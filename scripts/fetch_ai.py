#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 前沿雷达 · 抓取与产物生成（P1：纯硬规则，LLM 不参与选条）。

设计原则（与 news-feed 同源）：
  1. **选条全是硬规则** —— 时间窗 + 标题归一化去重 + 跨源去重 + 跨日折叠。
     LLM 不参与选条（P2 才引入，且只做摘要与打分）；
  2. **双通路兜底** —— mode=auto：先试直连 urls[]，失败再走 rsshub 实例池。
     每条通路都要过 desc_min 门槛（沿用 portfolio 踩坑经验：不设门槛会锁死在
     导语版实例上，静默毁掉后续摘要质量）；
  3. **绝不空窗** —— 全部源失败则不写任何文件，保留上一份产物；
  4. **配置驱动** —— 加源/换路由只改 scripts/sources.json。

用法：
  python scripts/fetch_ai.py --outdir docs
  HTTPS_PROXY=http://127.0.0.1:7890 python scripts/fetch_ai.py --outdir docs   # 本机调试
"""
import argparse
import datetime
import email.utils as eu
import glob
import hashlib
import html
import json
import os
import re
import statistics
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
TZ_CN = datetime.timezone(datetime.timedelta(hours=8))
UTC = datetime.timezone.utc

# 各车道的默认时间窗（小时）。官方源发布频率天然低（实测 OpenAI Research 12 天
# 一条），若统一用短窗会被整体过滤干净 —— 必须按车道区分。
LANE_MAX_AGE = {"official": 168, "paper": 72, "community": 48, "media": 96, "cn": 36}
DEFAULT_MAX_AGE = 48

ATOM = "{http://www.w3.org/2005/Atom}"
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}encoded"


def log(m):
    print(m, flush=True)


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def http_get(url, timeout=25):
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (ai-radar/1.0)", "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}")
        return r.read()


def strip_tags(s):
    s = re.sub(r"<script.*?</script>", " ", s or "", flags=re.S | re.I)
    s = re.sub(r"<style.*?</style>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</p>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\u00a0]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def norm_title(t):
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", t or "").lower()


def parse_dt(s):
    """兼容 RSS pubDate（RFC822）与 Atom published（ISO8601）。"""
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = eu.parsedate_to_datetime(s)
        if d is not None:
            return d if d.tzinfo else d.replace(tzinfo=UTC)
    except Exception:
        pass
    try:
        d = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except Exception:
        return None


def bj_pub(d):
    """北京时间智能显示，贴合国内阅读习惯（今天→HH:MM / 昨天 / M月D日）。"""
    if not d:
        return ""
    t = d.astimezone(TZ_CN)
    now = datetime.datetime.now(TZ_CN)
    if t.date() == now.date():
        return t.strftime("%H:%M")
    if t.date() == now.date() - datetime.timedelta(days=1):
        return t.strftime("昨天 %H:%M")
    if t.year == now.year:
        return f"{t.month}月{t.day}日 {t.strftime('%H:%M')}"
    return t.strftime("%Y-%m-%d %H:%M")


def item_id(title):
    return hashlib.md5(norm_title(title).encode("utf-8")).hexdigest()[:10]


def desc_median(items):
    L = [len(i["desc"]) for i in items if i.get("desc")]
    return int(statistics.median(L)) if L else 0


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def parse_feed(body):
    """解析 RSS / Atom → [{title, desc, url, dt}]。"""
    root = ET.fromstring(body)
    out = []

    rss = root.findall(".//item")
    if rss:
        for it in rss:
            link = (it.findtext("link") or "").strip()
            if not link:                      # 少数 feed 用 guid 当链接
                g = (it.findtext("guid") or "").strip()
                link = g if g.startswith("http") else ""
            desc = (it.findtext(CONTENT_NS) or it.findtext("description") or "")
            out.append({
                "title": strip_tags(it.findtext("title") or ""),
                "desc": strip_tags(desc),
                "url": link,
                "dt": parse_dt(it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date")),
            })
        return out

    for it in root.findall(f".//{ATOM}entry"):
        link = ""
        for ln in it.findall(f"{ATOM}link"):
            if ln.get("rel") in (None, "alternate"):
                link = (ln.get("href") or "").strip()
                break
        if not link:
            for ln in it.findall(f"{ATOM}link"):
                if ln.get("href"):
                    link = ln.get("href").strip()
                    break
        desc = it.findtext(f"{ATOM}content") or it.findtext(f"{ATOM}summary") or ""
        out.append({
            "title": strip_tags(it.findtext(f"{ATOM}title") or ""),
            "desc": strip_tags(desc),
            "url": link,
            "dt": parse_dt(it.findtext(f"{ATOM}published") or it.findtext(f"{ATOM}updated")),
        })
    return out


def parse_json_feed(body):
    """HF daily_papers / OpenRouter 这类 JSON 端点的通用拍平。"""
    j = json.loads(body)
    arr = j if isinstance(j, list) else next(
        (j[k] for k in ("items", "models", "data", "papers") if isinstance(j.get(k), list)), [])
    out = []
    for e in arr:
        if not isinstance(e, dict):
            continue
        paper = e.get("paper") if isinstance(e.get("paper"), dict) else e
        title = paper.get("title") or e.get("title") or e.get("id") or ""
        desc = paper.get("summary") or paper.get("abstract") or e.get("description") or ""
        url = (paper.get("url") or e.get("url") or e.get("id") or "")
        if url and not str(url).startswith("http"):
            url = f"https://huggingface.co/papers/{url}"
        dt = None
        for k in ("publishedAt", "published_at", "date", "createdAt"):
            if e.get(k):
                dt = parse_dt(str(e[k]))
                break
        out.append({"title": strip_tags(str(title)), "desc": strip_tags(str(desc)),
                    "url": str(url), "dt": dt})
    return out


# --------------------------------------------------------------------------- #
# 抓取：双通路 + desc 门槛
# --------------------------------------------------------------------------- #
def try_once(url, kind):
    body = http_get(url)
    items = parse_json_feed(body) if kind == "json" else parse_feed(body)
    if not items:
        raise RuntimeError("200 但 0 条（疑似 HTML 错误页）")
    return items


def fetch_source(src, pool):
    """按 auto/direct/rsshub 抓一个源，逐通路尝试并过 desc_min 门槛。

    返回 (items, via, diag)；全失败返回 (None, None, diag)。
    """
    kind = src.get("kind", "rss")
    dmin = int(src.get("desc_min", 0))
    diag = []

    direct = []
    for u in src.get("urls", []) or []:
        direct.append((u, re.sub(r"^https?://", "", u).split("/")[0]))
    pooled = []
    if src.get("route"):
        pooled = [(f"https://{h}{src['route']}", h) for h in pool]

    mode = src.get("mode", "auto")
    if mode == "direct":
        candidates = direct
    elif mode == "rsshub":
        candidates = pooled
    else:                                  # auto：先直连，失败再走实例池
        candidates = direct + pooled

    for url, host in candidates:
        try:
            items = try_once(url, kind)
            dm = desc_median(items)
            # desc 门槛：不达标继续试下一通路（防锁死导语版实例）
            if dmin and dm < dmin:
                diag.append(f"{host}:desc中位{dm}<{dmin}跳过")
                continue
            diag.append(f"{host}:OK({len(items)}条/desc{dm})")
            return items, host, diag
        except Exception as e:
            diag.append(f"{host}:{type(e).__name__}{getattr(e, 'code', '')}")
            time.sleep(0.2)

    return None, None, diag


# --------------------------------------------------------------------------- #
# 正文补抓（Jina Reader）—— 治 desc 为 0 或过短的源
# --------------------------------------------------------------------------- #
def jina_fetch(url, cap=4000):
    clean = re.sub(r"^https?://", "", url)
    body = http_get(f"https://r.jina.ai/https://{clean}", timeout=30)
    text = body.decode("utf-8", "ignore")
    text = re.sub(r"^Title:.*$", "", text, flags=re.M)
    text = re.sub(r"^URL Source:.*$", "", text, flags=re.M)
    text = re.sub(r"^Markdown Content:.*$", "", text, flags=re.M)
    return strip_tags(text)[:cap]


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="docs")
    ap.add_argument("--config", default=os.path.join(HERE, "sources.json"))
    ap.add_argument("--keep-days", type=int, default=30)
    ap.add_argument("--no-jina", action="store_true", help="关闭正文补抓")
    ap.add_argument("--jina-max", type=int, default=8, help="单次最多补抓条数")
    ap.add_argument("--id", default="", help="只抓某个源（调试用，逗号分隔可多个）")
    ap.add_argument("--only", default="", help="只抓某条车道（调试用）")
    a = ap.parse_args()

    cfg = json.load(open(a.config, encoding="utf-8"))
    pool = cfg["rsshub_pool"]
    srcs = [s for s in cfg["sources"] if s.get("enabled")]
    if a.only:
        srcs = [s for s in srcs if s["lane"] == a.only]
    if a.id:
        want = {x.strip() for x in a.id.split(",") if x.strip()}
        srcs = [s for s in srcs if s["id"] in want]
    if not srcs:
        log("没有匹配的启用源，退出")
        sys.exit(1)
    lanes = {l["id"]: l for l in cfg["lanes"]}
    now = datetime.datetime.now(TZ_CN)

    log("=" * 100)
    log(f"AI 前沿雷达 · 抓取  {now.strftime('%Y-%m-%d %H:%M')} 北京   启用源 {len(srcs)}")
    log("=" * 100)

    all_items, degraded, reports = [], [], []

    for src in srcs:
        if src.get("take") == 0:          # 纯数据源（如 OpenRouter），不产出条目
            continue
        log(f"[{src['lane']:9}] {src['id']:20} {src['block']}")
        items, via, diag = fetch_source(src, pool)
        if not items:
            degraded.append({"id": src["id"], "lane": src["lane"], "reason": "; ".join(diag)[:200]})
            log(f"  ⛔ 全部通路失败：{'; '.join(diag)[:160]}")
            reports.append({"id": src["id"], "ok": False, "via": None, "n": 0, "diag": diag})
            continue

        max_age = src.get("max_age_h") or LANE_MAX_AGE.get(src["lane"], DEFAULT_MAX_AGE)
        # ① 标题去重（同源内）
        seen, dedup = set(), []
        for i in items:
            k = norm_title(i["title"])
            if not k or k in seen:
                continue
            seen.add(k)
            dedup.append(i)
        # ② 时间窗 + 保底
        #    「绝不空窗」原则的延伸：严格的窗口会把天然低频的优质源清空
        #    （实测 HF 每日论文 28→0、OpenAI Research 10→0），而这些正是权重最高的源。
        #    故窗口内不足 min_take 条时，用窗口外最新的条目补足，并标 stale（前端弱化显示）。
        cutoff = now - datetime.timedelta(hours=max_age)
        ep = datetime.datetime(1970, 1, 1, tzinfo=UTC)
        inw, outw = [], []
        for i in dedup:
            i["ageH"] = -1 if i["dt"] is None else int((now - i["dt"]).total_seconds() // 3600)
            if i["dt"] is None or i["dt"] >= cutoff:
                inw.append(i)
            else:
                i["stale"] = True
                outw.append(i)
        keyf = lambda x: x["dt"] or ep
        if src.get("sort_desc", True):
            inw.sort(key=keyf, reverse=True)
            outw.sort(key=keyf, reverse=True)
        take = int(src.get("take", 8))
        min_take = int(src.get("min_take", 3))
        kept = inw[:take]
        if len(kept) < min_take:
            kept += outw[: min_take - len(kept)]
        for i in kept:
            i.update({"lane": src["lane"], "block": src["block"],
                      "source": src.get("default_source") or src["block"], "via": via})
        all_items.extend(kept)
        n_stale = sum(1 for i in kept if i.get("stale"))
        extra = f"（含 {n_stale} 条保底旧文）" if n_stale else ""
        log(f"  ✅ @{via}  取 {len(items)} → 去重 {len(dedup)} → "
            f"窗口({max_age}h)内 {len(inw)} → 采纳 {len(kept)}{extra}")
        reports.append({"id": src["id"], "ok": True, "via": via, "n": len(kept),
                        "raw": len(items), "diag": diag})

    if not all_items:
        log("⛔ 全部源失败：不写任何文件（保留上一份产物，latest.json 不被覆盖）")
        sys.exit(1)

    # ④ 跨源去重：同一事件多源报道，只留车道权重最高的（lane 声明顺序即权重）；
    #    车道内按新鲜度升序（ageH 小 = 新，排前面），无时间的排最后。
    lane_order = [l["id"] for l in cfg["lanes"]]
    all_items.sort(key=lambda x: (lane_order.index(x["lane"]) if x["lane"] in lane_order else 99,
                                  x["ageH"] if x["ageH"] >= 0 else 9999))
    seen2, merged = {}, []
    for i in all_items:
        k = norm_title(i["title"])
        if k in seen2:
            merged[seen2[k]]["alsoIn"].append(i["block"])
            continue
        seen2[k] = len(merged)
        i["alsoIn"] = []
        merged.append(i)
    if len(merged) < len(all_items):
        log(f"🔁 跨源去重折叠 {len(all_items) - len(merged)} 条")

    # ⑤ 跨日折叠：与最近一份归档比对，重复的标记而非丢弃
    prev = os.path.join(a.outdir, "latest.json")
    prev_titles = set()
    if os.path.exists(prev):
        try:
            with open(prev, encoding="utf-8") as f:
                prev_titles = {norm_title(x["title"]) for x in json.load(f).get("items", [])}
        except Exception as e:
            log(f"  ⚠️ 读上一份产物失败：{e}")
    for i in merged:
        i["isRepeat"] = norm_title(i["title"]) in prev_titles
    n_rep = sum(1 for i in merged if i["isRepeat"])
    if n_rep:
        log(f"♻️ 跨日重复标记 {n_rep} 条（保留但降权展示）")

    # ⑥ 正文补抓（仅对 desc 过短且非重复的条目，限量）
    if not a.no_jina:
        need = [i for i in merged if len(i["desc"]) < 300 and not i["isRepeat"] and i["url"]][: a.jina_max]
        if need:
            log(f"📥 Jina 补正文：{len(need)} 条（desc<300）")
        ok_j = 0
        for i in need:
            try:
                body = jina_fetch(i["url"])
                if len(body) > len(i["desc"]):
                    i["desc"] = body
                    i["descFrom"] = "jina"
                    ok_j += 1
            except Exception as e:
                log(f"  ⚠️ Jina 失败 {i['url'][:60]}：{type(e).__name__}")
        if need:
            log(f"  ✅ 补抓成功 {ok_j}/{len(need)}")

    # ⑦ 产物（ageH 保留给前端做"x 小时前"显示；-1 表示源未提供时间）
    for i in merged:
        i["id"] = item_id(i["title"])
        i["pubTime"] = bj_pub(i["dt"])
        i["pubTs"] = i["dt"].astimezone(TZ_CN).isoformat() if i["dt"] else ""
        i["desc"] = i["desc"][:4000]
        i.pop("dt", None)

    doc = {
        "version": 1,
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "date": now.strftime("%Y-%m-%d"),
        "lanes": [{"id": l["id"], "name": l["name"],
                   "count": sum(1 for i in merged if i["lane"] == l["id"])} for l in cfg["lanes"]],
        "counts": {"total": len(merged), "repeat": n_rep},
        "degraded": degraded,
        "items": merged,
    }

    os.makedirs(a.outdir, exist_ok=True)
    with open(os.path.join(a.outdir, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)

    daily_dir = os.path.join(a.outdir, "daily")
    os.makedirs(daily_dir, exist_ok=True)
    with open(os.path.join(daily_dir, f"{doc['date']}.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)

    # 运行报告（借鉴 agent-pulse：每次运行留档，结论可回溯到当时证据）
    rep_dir = os.path.join(a.outdir, "data", "reports")
    os.makedirs(rep_dir, exist_ok=True)
    with open(os.path.join(rep_dir, f"{now.strftime('%Y%m%dT%H%M')}.json"),
              "w", encoding="utf-8") as f:
        json.dump({"generated_at": doc["generated_at"], "counts": doc["counts"],
                   "sources": reports, "degraded": degraded}, f,
                  ensure_ascii=False, indent=1)

    log("-" * 100)
    log(f"✅ 产出 {len(merged)} 条（跨日重复 {n_rep}）→ latest.json + "
        f"daily/{doc['date']}.json + 运行报告")
    for l in doc["lanes"]:
        log(f"   {l['name']:8} {l['count']:>3} 条")
    if degraded:
        log(f"⚠️ 降级源 {len(degraded)} 个：{', '.join(d['id'] for d in degraded)}")

    # 归档清理
    cutoff_ts = time.time() - a.keep_days * 86400
    removed = 0
    for d in (daily_dir, rep_dir):
        for f in glob.glob(os.path.join(d, "*.json")):
            if os.path.getmtime(f) < cutoff_ts:
                os.remove(f)
                removed += 1
    if removed:
        log(f"🧹 清理 {removed} 个超过 {a.keep_days} 天的归档")


if __name__ == "__main__":
    main()
