#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 前沿雷达 · 离线冒烟测试（不联网、不依赖 Secrets）。

为什么必须有这个：`py_compile` 只查语法，**查不出 NameError**。实测教训——因为同一
文件的多条并行编辑互相覆盖，`lib_items = []` 的初始化丢了而引用还在，本地「语法通过」
却让 Actions 上的正式抓取直接崩掉。凡是 main() 里新增的分支，都应该在这里被跑到。

覆盖点：
  · 四种专用 parser（leaderboard / openrouter / hf-models / hf-spaces）
  · 双通路与 desc 门槛、时间窗保底、跨源去重、跨日折叠
  · LLM 增强两条路径：① 无 Key 全降级 ② 正常返回（含 want_type 分组）
  · 案例库入库门槛（低质 apps 条目只进简报、不进库）
  · 案例库按 url 累积（第二次运行只更新 lastSeen、不重复入库）
  · 产物字段无内部标记外泄（_want_type / _translate_title）

用法：python scripts/smoke_test.py
"""
import datetime
import email.utils as eu
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fetch_ai as F          # noqa: E402

# ⚠️ run() 会把 F._call_llm_batch 换成桩（各用例共用），所以想测**真实**的批调用逻辑
# （如配额熔断）必须提前抓住原始引用 —— 否则测到的是桩，断言会假通过。
_REAL_BATCH = F._call_llm_batch

NOW = datetime.datetime.now(F.UTC)


def rss(items):
    """items: [(标题, 描述, 几小时前)] → RSS 文本。"""
    parts = []
    for ti, de, h in items:
        d = eu.format_datetime(NOW - datetime.timedelta(hours=h))
        parts.append(f"<item><title>{ti}</title><description>{de}</description>"
                     f"<link>https://example.com/{abs(hash(ti)) % 99999}</link>"
                     f"<pubDate>{d}</pubDate></item>")
    return ("<?xml version='1.0'?><rss version='2.0'><channel>"
            + "".join(parts) + "</channel></rss>").encode()


FIX = {
    # —— RSS（英文车道）——
    "https://openai.com/news/rss.xml": rss([
        ("OpenAI releases GPT-6 Sol with 1M context",
         "OpenAI today released GPT-6 Sol. " + "detail " * 40, 2),
        ("A quiet research note", "short " * 5, 100),
    ]),
    # —— RSS（apps 车道，中文）——
    "https://www.v2ex.com/feed/tab/creative.xml": rss([
        ("我用 AI 做了个记账小程序", "把流水截图丢进去自动分类。" * 20, 1),
        ("分享一个自用的论文摘要插件", "在 arXiv 页面就地叠加中文摘要。" * 20, 5),
        # 故意混入一条与 AI 无关的数码推荐（复刻线上实测：少数派的「耳机剁手清单」）。
        # 它应该**进简报、不进案例库** —— 用来守门案例库的入库门槛。
        ("派早报 | 蓝牙耳机剁手清单", "与 AI 无关的消费电子推荐。" * 20, 2),
    ]),
    "https://hnrss.org/show": rss([
        ("Show HN: I built a local-first notes app",
         "Built it with Claude Code over two weekends. " * 20, 1),
    ]),
    # —— json / openrouter：一个 stealth，一个普通（应被过滤）——
    "https://openrouter.ai/api/v1/models": json.dumps({"data": [
        {"id": "stealth/space-bunny-alpha", "name": "Space Bunny Alpha",
         "context_length": 1000000, "pricing": {"prompt": "0", "completion": "0"},
         "architecture": {"input_modalities": ["text", "image"]},
         "description": "Stealth model for testing."},
        {"id": "mancer/weaver-alpha", "name": "Mancer: Weaver (alpha)",
         "context_length": 8000, "pricing": {"prompt": "0.1", "completion": "0.2"},
         "architecture": {"input_modalities": ["text"]}, "description": "not stealth"},
    ]}).encode(),
    # —— json / leaderboard：两条在上架窗口内、一条很早 ——
    "https://raw.githubusercontent.com/AmigaMeow/llm-leaderboard-data/main/data/latest.json":
        json.dumps({"sources": {}, "models": [
            {"id": "anthropic/claude-opus-5.5", "display_name": "Claude Opus 5.5",
             "org": "Anthropic", "listed_at_iso": (NOW - datetime.timedelta(days=2)).strftime("%Y-%m-%d"),
             "arena_score": 1517.8, "arena_rank": 1, "open_weights": False,
             "context_length": 1000000, "price_in": 4, "price_out": 20},
            {"id": "old/model", "display_name": "Old Model", "org": "Old",
             "listed_at_iso": (NOW - datetime.timedelta(days=400)).strftime("%Y-%m-%d"),
             "arena_score": 1, "open_weights": True},
        ]}).encode(),
    # —— json / hf-models：三个仓库，只有「机构发布」那条能过 min_likes ——
    "https://huggingface.co/api/models?sort=createdAt&direction=-1&limit=30": json.dumps([
        {"modelId": "acme/Llama-4-8B", "createdAt": (NOW - datetime.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
         "likes": 42, "downloads": 1200, "tags": ["text-generation", "transformers"]},
        {"modelId": "someone/Llama-4-8B-gguf", "createdAt": (NOW - datetime.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
         "likes": 99, "downloads": 9999, "tags": ["gguf"]},                     # 应被 exclude_tags 剔除
        {"modelId": "test/MyAwesomeModel-TestRepo", "createdAt": (NOW - datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
         "likes": 0, "downloads": 0, "tags": ["bert"]},                          # 应被 min_likes 剔除
    ]).encode(),
    # —— json / hf-spaces ——
    "https://huggingface.co/api/spaces?sort=trendingScore&direction=-1&limit=25": json.dumps([
        {"id": "someone/cool-app", "sdk": "gradio", "likes": 30,
         "createdAt": "2026-09-01T00:00:00.000Z",
         "lastModified": (NOW - datetime.timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")},
    ]).encode(),
}

TEST_IDS = ["openai-news", "v2ex-create", "hn-show", "openrouter-stealth",
            "llm-leaderboard", "hf-new-models", "hf-spaces-trend"]


def make_config(path):
    cfg = json.load(io.open(os.path.join(HERE, "sources.json"), encoding="utf-8"))
    keep = [s for s in cfg["sources"] if s["id"] in TEST_IDS]
    for s in keep:
        s["enabled"] = True
        s["mode"] = "direct"                      # 冒烟测试不走 rsshub 池
    cfg["sources"] = keep
    io.open(path, "w", encoding="utf-8").write(json.dumps(cfg, ensure_ascii=False))
    return cfg


def stub_http():
    """替换网络层：命中 FIX 返回对应 body，未命中抛错（暴露漏配的用例）。"""
    def _get(url, timeout=25, retry=1):
        for k, v in FIX.items():
            if url.startswith(k) or k in url:
                return v
        raise RuntimeError(f"smoke: 未预置数据的 URL {url}")
    F.http_get = _get
    F.jina_fetch = lambda url, cap=1500: ""       # 关闭正文补抓


def run(tmp, llm_stub):
    cfg_path = os.path.join(tmp, "sources.json")
    make_config(cfg_path)
    stub_http()
    if llm_stub:
        def fake_batch(batch, tcfg, mode, want_type=False):
            for i, it in enumerate(batch):
                if it["_translate_title"]:
                    it["titleCn"] = "【中】" + it["title"][:20]
                it["summary"] = "摘要：" + it["title"][:30]
                # 与 AI 无关的条目给 rel=0（复刻线上实测的低质 apps 条目）
                junk = "耳机" in it["title"]
                it["score"] = {"rel": 0 if junk else (9 if i == 0 else 6),
                               "info": 1 if junk else (8 if i == 0 else 6),
                               "fresh": 2 if junk else (9 if i == 0 else 5)}
                it["topic"] = "测试主题"
                if want_type:
                    it["apptype"] = "Web 应用"
            return "stub-model", True, 1
        F._call_llm_batch = fake_batch
    else:
        for k in ("GEMINI_API_KEY", "AGNES_API_KEY"):
            os.environ.pop(k, None)
    sys.argv = ["fetch_ai.py", "--config", cfg_path, "--outdir", tmp]
    F.main()
    return json.load(io.open(os.path.join(tmp, "latest.json"), encoding="utf-8"))


def check(cond, msg, problems):
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        problems.append(msg)


def main():
    problems = []
    tmp = tempfile.mkdtemp(prefix="airadar-smoke-")
    try:
        # ---------- 用例 1：LLM 全降级（无 Key） ----------
        print("\n[1/4] LLM 无 Key → 全降级路径")
        d = run(tmp, llm_stub=False)
        items = d["items"]
        lanes = {l["id"]: l["count"] for l in d["lanes"]}
        cases_path = os.path.join(tmp, "data", "cases.json")
        cases = json.load(io.open(cases_path, encoding="utf-8")) if os.path.exists(cases_path) else {}
        cl = cases.get("cases", [])
        check(len(items) > 0, f"简报有产出（{len(items)} 条）", problems)
        check(lanes.get("apps", 0) > 0, f"apps 车道有条目（{lanes.get('apps', 0)} 条）", problems)
        check(all(i.get("summary") for i in items), "每条都有 summary（降级用正文开头）", problems)
        check(all(i.get("level") in ("top", "watch", "normal") for i in items),
              "每条都有 level", problems)
        check(len(cl) >= 4, f"案例库有种子条目（{len(cl)} 条）", problems)
        check(all(c.get("type") for c in cl),
              f"案例库条目全部带 type（LLM 或关键词兜底，{len(cl)} 条）", problems)
        apps_it = [i for i in items if i["lane"] == "apps"]
        check(apps_it and all(i.get("apptype") for i in apps_it),
              f"apps 简报条目全部带应用形态（兜底，{len(apps_it)} 条）", problems)
        # 兜底分类要可解释：至少出现两种形态，而不是全被丢进同一个桶
        n_kind = len({i["apptype"] for i in apps_it})
        check(n_kind >= 2, f"兜底分类有区分度（{n_kind} 种形态）", problems)
        check(all(c.get("src") == "apps" for c in cl),
              "案例库记录都带 src=apps", problems)
        check(all("score" in c for c in cl),
              "案例记录带 score（淘汰按分数排，不按 lastSeen）", problems)
        internal = {"_want_type", "_translate_title", "dt"}
        leaked = sorted({k for i in items for k in i if k in internal})
        check(not leaked, f"产物无内部标记外泄（发现 {leaked}）", problems)
        # hf-models 的两道降噪
        hf = [i["title"] for i in items if i.get("block") == "HF 新模型"]
        check(any("acme/Llama-4-8B" in t for t in hf), "HF 新模型：机构发布被保留", problems)
        check(not any("gguf" in t for t in hf), "HF 新模型：exclude_tags 剔除了 gguf", problems)
        check(not any("TestRepo" in t for t in hf), "HF 新模型：min_likes 剔除了测试仓", problems)
        # stealth 过滤
        st = [i for i in items if i.get("block") == "匿名公测"]
        check(len(st) == 1 and "Space Bunny" in (st[0].get("titleCn") or st[0]["title"]),
              "匿名公测：只保留 stealth/ 前缀", problems)
        # leaderboard 时间窗
        lb = [i for i in items if i.get("block") == "模型上架"]
        check(all("Old Model" not in (i.get("titleCn") or i["title"]) for i in lb),
              "模型上架：超出 recent_days 的旧模型被过滤", problems)
        ids = [i["id"] for i in items]
        check(len(ids) == len(set(ids)), "条目 id 无重复", problems)
        # 跨日折叠需要一份「日期不是今天」的归档：同日重跑本来就不自我比对（设计如此），
        # 不造历史的话这条断言永远测不到东西。
        daily_dir = os.path.join(tmp, "daily")
        os.makedirs(daily_dir, exist_ok=True)
        prev = dict(d)
        prev["date"] = "2026-01-01"
        io.open(os.path.join(daily_dir, "2026-01-01.json"), "w", encoding="utf-8").write(
            json.dumps(prev, ensure_ascii=False))
        # 案例库缓存：再跑一次不应新增
        n_before = len(cl)
        d2 = run(tmp, llm_stub=False)
        cases2 = json.load(io.open(cases_path, encoding="utf-8"))
        check(len(cases2["cases"]) == n_before,
              f"二次运行案例库命中缓存、不新增（{n_before} → {len(cases2['cases'])}）", problems)
        check(any(i.get("isRepeat") for i in d2["items"]), "跨日折叠：二次运行识别出重复", problems)

        # ---------- 用例 1b：老记录补分（换口径前入库的条目没有 score） ----------
        # 没有这一步，老记录就永远判不了质量、永久留在库里（实测漏了 21 条，含一条
        # 与 AI 无关的「社媒营销策略」）。
        print("\n[1b/4] 案例库补分：无分老记录 → 补分后按门槛判")
        tmp3 = tempfile.mkdtemp(prefix="airadar-smoke3-")
        try:
            run(tmp3, llm_stub=False)                      # 先用无 LLM 造一个库
            cp3 = os.path.join(tmp3, "data", "cases.json")
            j3 = json.load(io.open(cp3, encoding="utf-8"))
            j3["cases"].append({                           # 注入一条"换口径前"的老记录
                "url": "https://example.com/legacy-junk", "title": "派早报 | 蓝牙耳机剁手清单",
                "titleEn": "", "summary": "与 AI 无关的消费电子推荐。", "type": "其他",
                "topic": "", "block": "少数派",
                "firstSeen": "2026-01-01", "lastSeen": "2026-01-01",
            })
            io.open(cp3, "w", encoding="utf-8").write(json.dumps(j3, ensure_ascii=False))
            run(tmp3, llm_stub=True)                       # 第二次跑到 LLM → 触发补分
            c3 = json.load(io.open(cp3, encoding="utf-8"))["cases"]
            check(not any("耳机" in (r.get("title") or "") for r in c3),
                  "无分老记录补分后被门槛剔除（不再永久留存）", problems)
            check(any(isinstance(r.get("score"), (int, float)) for r in c3),
                  "补分结果写回了 score 字段", problems)
        finally:
            shutil.rmtree(tmp3, ignore_errors=True)

        # ---------- 用例 2：LLM 正常返回 ----------
        print("\n[2/4] LLM 正常返回（含 want_type 分组）")
        tmp2 = tempfile.mkdtemp(prefix="airadar-smoke2-")
        try:
            d = run(tmp2, llm_stub=True)
            items = d["items"]
            apps = [i for i in items if i["lane"] == "apps"]
            check(all(i.get("summary") for i in items), "每条都有 LLM 摘要", problems)
            check(any(i.get("titleCn") for i in items), "英文标题被中文化", problems)
            check(all(i.get("score") for i in items), "每条都有三维打分", problems)
            check(all(i.get("total") is not None for i in items), "每条都有加权总分", problems)
            check(apps and all(i.get("apptype") for i in apps),
                  f"apps 条目的应用形态标签（{len(apps)} 条）", problems)
            check(all(i.get("apptype") is None for i in items if i["lane"] != "apps"),
                  "非 apps 条目没有被塞应用形态", problems)
            lv = d["counts"]["byLevel"]
            check(lv["top"] <= 15, f"重磅受名额上限约束（top={lv['top']}）", problems)
            check(lv["top"] >= 1, "至少有一条重磅", problems)
            # 排序：分级优先（首屏第一条必须是 top）
            check(items[0]["level"] == "top", "首屏第一条是重磅", problems)
            # ── 案例库入库门槛：与 AI 无关的低质条目必须「进简报、不进库」──
            # 简报要宽（不漏信息），案例库要严（宁缺毋滥），这是两者唯一的规则差异。
            cl2 = json.load(io.open(os.path.join(tmp2, "data", "cases.json"),
                                    encoding="utf-8"))["cases"]
            junk = [i for i in items if "耳机" in i["title"]]
            check(len(junk) == 1, "低质 apps 条目仍留在简报里（简报要宽）", problems)
            check(not any("耳机" in (c.get("title") or "") for c in cl2),
                  "低质 apps 条目被案例库挡掉（案例库要严）", problems)
            apps_c = [c for c in cl2 if c.get("src") == "apps"]
            check(bool(apps_c) and all(c.get("score", 0) >= 5.0 for c in apps_c),
                  f"库里 apps 条目分数均达门槛（{len(apps_c)} 条）", problems)
            check(all(c.get("rel", 99) >= 3 for c in apps_c),
                  "库里 apps 条目 rel 均达门槛", problems)
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)

        # ---------- 用例 3：交叉校验 ----------
        print("\n[3/4] 产物一致性")
        d = json.load(io.open(os.path.join(tmp, "latest.json"), encoding="utf-8"))
        total = sum(l["count"] for l in d["lanes"])
        check(total == d["counts"]["total"],
              f"车道计数之和 = 总数（{total} = {d['counts']['total']}）", problems)
        check(sum(d["counts"]["byLevel"].values()) == d["counts"]["total"],
              "分级计数之和 = 总数", problems)
        rep_dir = os.path.join(tmp, "data", "reports")
        check(os.path.isdir(rep_dir) and os.listdir(rep_dir), "运行报告已落盘", problems)

        # ---------- 用例 4：probe 与生产共用解析器 ----------
        print("\n[4/4] 探测脚本与生产共用一个解析口径")
        import probe_sources as P
        check(P.F is F or P.F.PARSERS is F.PARSERS,
              "probe_sources 复用 fetch_ai 的 PARSERS", problems)
        check(len(F.PARSERS) == 4, f"解析器注册表 4 项（{sorted(F.PARSERS)}）", problems)

        # ---------- 用例 4b：模型配额熔断与 429 诊断 ----------
        # 实测教训：gemini-3-flash 额度耗尽后若不熔断，每批都会再去撞一次 429
        # （一次运行白撞 7 次）。这条断言保证「已熔断的模型不再发起任何请求」。
        print("\n[4b/4] 模型配额熔断与 429 诊断")
        check(_REAL_BATCH is not F._call_llm_batch,
              "本用例用的是**原始**批调用（run() 已把模块属性换成桩）", problems)
        check(F._is_quota_err("HTTP Error 429: Too Many Requests"), "识别 429 为配额类", problems)
        check(F._is_quota_err("Resource exhausted: quota exceeded"), "识别 quota 字样", problems)
        check(not F._is_quota_err("HTTP Error 500: Internal Server Error"),
              "500 不算配额类（应继续尝试其它模型）", problems)
        import io as _io
        import urllib.error as _ue
        _e = _ue.HTTPError("https://x/v1/chat/completions", 429, "Too Many Requests", {},
                           _io.BytesIO(b'{"error":{"message":"Quota exceeded for model gemini-3-flash"}}'))
        check("Quota exceeded" in F._err_detail(_e),
              "429 的响应体被带进日志（否则只能看到 Too Many Requests）", problems)
        _fake = {"models": [{"name": "smoke-dead", "model": "x",
                             "base": "https://invalid.example", "key_env": "SMOKE_KEY"}]}
        os.environ["SMOKE_KEY"] = "k"
        F._DEAD_MODELS.add("smoke-dead")
        _n, _ok, _tried = _REAL_BATCH(
            [{"title": "t", "desc": "d", "_translate_title": False}], _fake, mode="summarize")
        check((not _ok) and _tried == 0,
              f"已熔断的模型不再发起请求（tried={_tried}，应为 0）", problems)
        F._DEAD_MODELS.discard("smoke-dead")
        os.environ.pop("SMOKE_KEY", None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 70)
    if problems:
        print(f"❌ 冒烟测试失败 {len(problems)} 项：")
        for p in problems:
            print("   - " + p)
        sys.exit(1)
    print("✅ 冒烟测试全部通过")


if __name__ == "__main__":
    main()
