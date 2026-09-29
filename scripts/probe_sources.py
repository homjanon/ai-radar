#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 前沿雷达 · 源可用性探测（P0，只读）。

用途：在 GitHub Actions（美国 IP、无代理）上建立源基线 —— 本机经代理的实测
      结果不能代表 Actions（实测教训：大量源在本机报 Tunnel 502 而生产可用）。

与 probe-models.yml 同一定位：**本 workflow 不写任何文件、不提交、不碰 docs/**，
跑完看日志即可。

观测项（每源每实例）：
  - HTTP 状态 / 条目数 / 耗时
  - **desc 中位长度** —— 各 rsshub 实例返回的 desc 深浅不一（导语版 vs 全文版），
    直接决定 LLM 摘要质量；探测出「desc 最深的实例」才能写进配置的实例顺序。
  - 最新一条标题（人工核对内容是否对得上）

用法：
  python scripts/probe_sources.py                    # 测全部源
  python scripts/probe_sources.py --only official    # 只测某条车道
  python scripts/probe_sources.py --id hf-blog       # 只测某个源
"""
import argparse
import concurrent.futures as cf
import datetime
import json
import os
import re
import ssl
import statistics
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TZ_CN = datetime.timezone(datetime.timedelta(hours=8))

# 复用生产管线的解析器与统计口径 —— 探测结论必须与生产一致。
# 踩过的坑：probe 曾自带一套 parse_json（只数数组长度），于是对 openrouter / hf-models
# 这类源报出「460 条 / 30 条」的乐观数字，而生产要过 exclude_tags / min_likes / stealth
# 前缀等过滤，实际产出可能只有个位数。探测与生产用两套口径＝持续误导。
sys.path.insert(0, HERE)
import fetch_ai as F          # noqa: E402

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def log(m):
    print(m, flush=True)


def norm(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", s).strip()


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.status, r.read()


def parse_feed(body):
    """返回 [(title, desc, pub)]；兼容 RSS 与 Atom。"""
    root = ET.fromstring(body)
    els = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    out = []
    for it in els:
        ti = it.findtext("title") or it.findtext("{http://www.w3.org/2005/Atom}title") or ""
        de = (it.findtext("description")
              or it.findtext("{http://www.w3.org/2005/Atom}summary")
              or it.findtext("{http://www.w3.org/2005/Atom}content")
              or it.findtext("content:encoded") or "")
        pu = (it.findtext("pubDate") or it.findtext("published")
              or it.findtext("{http://www.w3.org/2005/Atom}updated") or "")
        out.append((norm(ti), norm(de), pu.strip()))
    return out


def parse_json(body):
    j = json.loads(body)
    if isinstance(j, list):
        return j
    for k in ("items", "models", "data", "papers"):
        if isinstance(j.get(k), list):
            return j[k]
    return []


def med_desc(items):
    L = [len(d) for _, d, _ in items if d]
    return int(statistics.median(L)) if L else 0


def oldest_age_h(items):
    """最早一条距今多少小时（判断源是否还在更新）。解析失败返回 -1。"""
    import email.utils as eu
    best = None
    for _, _, p in items:
        try:
            d = eu.parsedate_to_datetime(p)
            if d is None:
                continue
            if d.tzinfo is None:
                d = d.replace(tzinfo=datetime.timezone.utc)
            age = (datetime.datetime.now(datetime.timezone.utc) - d).total_seconds() / 3600
            if best is None or age < best:
                best = age
        except Exception:
            continue
    return int(best) if best is not None else -1


def probe_url(url, kind="rss", src=None):
    """测一个 URL，返回 dict。

    解析走 fetch_ai.try_once —— 与生产完全同一套代码（含专用 parser 与过滤），
    这样「探测说几条、生产就产出几条」。专用 parser 允许返回空列表（过滤后无合格
    条目属正常），故 0 条不算失败、只在 err 里注明。
    """
    t0 = time.time()
    try:
        items = F.try_once(url, kind, src or {})
        n = len(items)
        desc = F.desc_median(items)
        fh = -1
        for it in items:
            d = it.get("dt")
            if d:
                h = int((datetime.datetime.now(datetime.timezone.utc)
                         - d).total_seconds() // 3600)
                if fh < 0 or h < fh:
                    fh = h
        title = norm(str(items[0].get("title") or "")) if items else ""
        err = "" if n else "0 条（过滤后无合格条目，非抓取失败）"
        return {"ok": True, "status": 200, "n": n, "desc": desc, "title": title[:60],
                "cost": time.time() - t0, "fresh_h": fh, "err": err}
    except Exception as e:
        return {"ok": False, "status": 0, "n": 0, "desc": 0, "title": "",
                "cost": time.time() - t0, "err": f"{type(e).__name__}: {str(e)[:70]}"}


def config_summary(cfg):
    """打印配置全貌（车道 / 源清单 / 模型链 / 案例库）。

    合到这里而不是写在 workflow 里：配置摘要必须和探测结果出自同一处，
    否则「配置里有什么」和「实测到什么」会各说各话。
    """
    from collections import Counter
    s = cfg["sources"]
    on = [x for x in s if x.get("enabled")]
    log(f"配置版本 {cfg.get('version')} ｜ 源 {len(s)} 个，启用 {len(on)} 个（其余为候选）")
    cn_on, cn_all = Counter(x["lane"] for x in on), Counter(x["lane"] for x in s)
    log("车道（声明顺序 = 展示与跨源去重优先级）：")
    for l in cfg["lanes"]:
        log(f"   {l['id']:10} {l['name']:8} 启用 {cn_on.get(l['id'], 0):>2} / 共 "
            f"{cn_all.get(l['id'], 0):>2}   weight={l.get('weight')}")
    log("源清单：")
    for x in s:
        log(f"   [{'ON ' if x.get('enabled') else 'cand'}] {x['id']:22} {x['lane']:10} "
            f"{x.get('mode', 'auto'):7} {x.get('kind', 'rss'):5} "
            f"{x.get('parser', '-'):13} {x.get('block', '')}")
    t = cfg.get("translate", {})
    log("模型链：" + " → ".join(m["name"] for m in t.get("models", [])))
    cases = cfg.get("cases", {})
    log(f"案例库：enabled={cases.get('enabled')} file={cases.get('file')} "
        f"max={cases.get('max')}")
    log("=" * 108 + "\n")


def probe_source(src, pool):
    """测一个源的所有通路，返回结果列表。"""
    sid = src["id"]
    kind = src.get("kind", "rss")
    rows = []

    # 通路 1：rsshub 实例池（有 route 就测，兼容 auto / rsshub 两种模式）
    if src.get("route"):
        for host in pool:
            url = f"https://{host}{src['route']}"
            r = probe_url(url, kind, src)
            r.update({"id": sid, "via": host, "mode": "rsshub"})
            rows.append(r)
        # 中文源额外试 .cn 专属实例（portfolio 经验：对中文路由更稳）
        if src.get("lane") == "cn":
            url = f"https://rss.injahow.cn{src['route']}"
            r = probe_url(url, kind, src)
            r.update({"id": sid, "via": "rss.injahow.cn", "mode": "rsshub"})
            rows.append(r)

    # 通路 2：直连兜底
    for url in src.get("urls", []) or []:
        r = probe_url(url, kind, src)
        host = re.sub(r"^https?://", "", url).split("/")[0]
        r.update({"id": sid, "via": host, "mode": "direct"})
        rows.append(r)

    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "sources.json"))
    ap.add_argument("--only", default="", help="只测某条车道")
    ap.add_argument("--id", default="", help="只测某个源 id")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()

    cfg = json.load(open(a.config, encoding="utf-8"))
    pool = cfg["rsshub_pool"]
    srcs = [s for s in cfg["sources"] if s.get("mode") != "off"]
    if a.only:
        srcs = [s for s in srcs if s.get("lane") == a.only]
    if a.id:
        srcs = [s for s in srcs if s.get("id") == a.id]
    if not srcs:
        log("没有匹配的源，退出")
        sys.exit(1)

    now = datetime.datetime.now(TZ_CN)
    log("=" * 108)
    log(f"AI 前沿雷达 · 源探测（Actions 侧基线）  {now.strftime('%Y-%m-%d %H:%M')} 北京")
    log(f"环境：{os.environ.get('RUNNER_OS', 'local')}  源数：{len(srcs)}  实例池：{len(pool)}")
    log("=" * 108)

    tasks = []
    for s in srcs:
        n = (len(pool) if s.get("route") else 0) + len(s.get("urls", []) or [])
        if s.get("route") and s.get("lane") == "cn":
            n += 1
        tasks.append((s, n))
    total = sum(n for _, n in tasks)
    log(f"共 {total} 个通路待测（workers={a.workers}）\n")

    all_rows = []
    done = 0
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(probe_source, s, pool): s for s, _ in tasks}
        for f in cf.as_completed(futs):
            src = futs[f]
            try:
                rows = f.result()
            except Exception as e:
                log(f"  [!] {src['id']} 探测异常：{e}")
                continue
            done += 1
            all_rows.extend(rows)
            ok = [r for r in rows if r["ok"]]
            best = max(ok, key=lambda r: (r["desc"], r["n"])) if ok else None
            if best:
                log(f"[{done}/{len(tasks)}] {src['id']:22} OK  共{len(ok)}/{len(rows)}通路可用  "
                    f"最优 @{best['via']} ({best['n']}条 desc{best['desc']})")
            else:
                errs = " ; ".join(f"{r['via']}={r['err'][:28]}" for r in rows[:2])
                log(f"[{done}/{len(tasks)}] {src['id']:22} XX  全部不可用：{errs}")

    # ---------- 汇总 ----------
    log("\n" + "=" * 108)
    log("汇总：每个源的最优通路（可直接写进 sources.json 的实例顺序）")
    log("=" * 108)
    lines = []
    header = f"{'源':22} {'车道':9} {'最优实例':26} {'条数':>5} {'desc中位':>9} {'最新h':>6}  标题"
    log(header)
    lines.append("| 源 | 车道 | 最优通路 | 条数 | desc中位 | 最新(小时前) | 首条标题 |")
    lines.append("|---|---|---|---:|---:|---:|---|")
    for s in srcs:
        rows = [r for r in all_rows if r["id"] == s["id"] and r["ok"]]
        if not rows:
            log(f"{s['id']:22} {s.get('lane',''):9} {'—— 全部失败 ——':26}")
            lines.append(f"| `{s['id']}` | {s.get('lane','')} | 全部失败 | 0 | 0 | - | - |")
            continue
        best = max(rows, key=lambda r: (r["desc"], r["n"]))
        fh = best.get("fresh_h", -1)
        # 僵尸源识别：源"活着"（能取到条目）但久不更新 —— 比彻底失败更隐蔽，
        # 会让面板长期显示同一批旧内容而不报错。
        stale = ""
        if best["n"] == 0:
            stale += "  ⚠️过滤后无合格条目（该源当日产出 0 条）"
        if isinstance(fh, int):
            if fh > 240:
                stale = "  ⚠️僵尸源(超10天未更新)"
            elif fh > 72:
                stale = "  ⚠️低频"
        log(f"{s['id']:22} {s.get('lane',''):9} {best['via']:26} {best['n']:>5} "
            f"{best['desc']:>9} {fh:>6}{stale}  {best['title'][:40]}")
        lines.append(f"| `{s['id']}` | {s.get('lane','')} | {best['via']} | {best['n']} "
                     f"| {best['desc']} | {fh}{stale} | {best['title'][:40]} |")

    # 失败通路明细
    bad = [r for r in all_rows if not r["ok"]]
    if bad:
        log("\n" + "-" * 108)
        log(f"失败通路明细（{len(bad)} 条）")
        log("-" * 108)
        for r in bad[:60]:
            log(f"  {r['id']:22} @{r['via']:26} {r['err'][:60]}")

    # ---------- 写 Actions 摘要 ----------
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a", encoding="utf-8") as f:
            f.write(f"# AI 前沿雷达 · 源探测基线\n\n")
            f.write(f"运行时间：{now.strftime('%Y-%m-%d %H:%M')} 北京 ｜ "
                    f"环境：{os.environ.get('RUNNER_OS','local')} ｜ 源数：{len(srcs)} ｜ 通路：{total}\n\n")
            f.write("\n".join(lines))
            f.write(f"\n\n失败通路 {len(bad)} 条（完整明细见日志）\n")
        log(f"\n✅ 已写入 GITHUB_STEP_SUMMARY")

    log("\n探测完成（只读，未写入任何文件）")


if __name__ == "__main__":
    main()
