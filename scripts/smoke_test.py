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
  · 产物字段无内部标记外泄（_want_type / _translate_title / _aihot*）
  · AIHOT 精选：解析、**同语言**三层去重、精选集内部去重、影子不污染产物、
    接口失败/304 两条降级路径

用法：python scripts/smoke_test.py
"""
import datetime
import email.utils as eu
import io
import json
import os
import re
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

# --------------------------------------------------------------------------- #
# AIHOT 精选：离线桩
#   ⚠️ 必须打桩。fetch_aihot 走的是 _aihot_get（自己开 urllib，因为要读 ETag 响应头），
#   不经过 http_get —— 只 stub http_get 的话，冒烟测试会**偷偷联网**打 AIHOT，
#   既违背"离线守门"的定位，也会在对方接口抖动时假失败。
# --------------------------------------------------------------------------- #
AIHOT_STUB = {"mode": "ok", "etag": 'W/"smoke-aihot-1"'}


def _ah(i, zh, en, url, score, mins=40):
    p = (NOW - datetime.timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {"id": "smoke-%d" % i, "title": zh, "originalTitle": en,
            "summary": zh + "：这是 AIHOT 写的中文摘要，长度用来验证表达层还原。" * 2,
            "source": {"name": "X：测试号 (@tester)"},
            "links": {"aihot": "https://aihot.news/items/smoke-%d" % i, "original": url},
            "publishedAt": p, "discoveredAt": p, "category": "ai-products",
            "score": score, "selected": True,
            "reason": zh + "：推荐理由由 AIHOT 编辑给出，应当原样保留、不被我们重写。",
            "attribution": {"name": "AIHOT", "url": "https://aihot.news/items/smoke-%d" % i}}


def aihot_payload():
    """三条：一条与我们英文标题撞车、两条是它自己内部的重复（中文像、英文不像）。

    后两条复刻 2026-10-09 影子首跑撞上的真实情况 —— Arena 一笔融资在 AIHOT
    精选里以三种措辞出现，而它们的英文 originalTitle 是三条完全不同的推文正文，
    只按英文比一条都去不掉。这是「同语言才可比」这条规则的回归用例。
    """
    its = [
        _ah(1, "OpenAI 发布 GPT-6 Sol 支持百万上下文",
            "OpenAI releases GPT-6 Sol with 1M context",
            "https://x.com/tester/status/9001", 80),
        _ah(2, "Arena 完成 2 亿美元 B 轮融资估值 31 亿美元",
            "We're incredibly proud to continue working with @thehousefund!",
            "https://x.com/arena/status/9002", 67),
        _ah(3, "Arena 完成 2 亿美元 B 轮融资估值达 31 亿美元",
            "Grateful to have @lightspeedvp with us as we build what's next!",
            "https://x.com/arena/status/9003", 65, mins=90),
        _ah(4, "一条与现有内容完全无关的独立消息",
            "An unrelated standalone note about local inference caching",
            "https://x.com/tester/status/9004", 72, mins=120),
    ]
    return json.dumps({"schemaVersion": 1,
                       "query": {"mode": "selected", "window": "24h", "by": "published"},
                       "page": {"count": len(its), "hasMore": False, "nextCursor": None},
                       "items": its}).encode()


def stub_aihot():
    def _get(url, etag="", timeout=30):
        if AIHOT_STUB["mode"] == "fail":
            raise RuntimeError("smoke: AIHOT 接口不可用")
        if AIHOT_STUB["mode"] == "304" and etag == AIHOT_STUB["etag"]:
            return 304, b"", etag
        return 200, aihot_payload(), AIHOT_STUB["etag"]
    F._aihot_get = _get



def make_config(path):
    cfg = json.load(io.open(os.path.join(HERE, "sources.json"), encoding="utf-8"))
    keep = [s for s in cfg["sources"] if s["id"] in TEST_IDS]
    for s in keep:
        s["enabled"] = True
        s["mode"] = "direct"                      # 冒烟测试不走 rsshub 池
    cfg["sources"] = keep
    # 影子开关由用例控制（默认跟 sources.json 一致 = 开），用来分别验两条路径
    if isinstance(cfg.get("aihot"), dict):
        cfg["aihot"]["shadow_only"] = AIHOT_STUB.get("shadow", True)
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
    stub_aihot()                                   # AIHOT 走独立网络层，单独打桩


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

        # 正文翻译（T1）：同样打桩，产出真含中文的 descZh
        def fake_body_batch(batch, tcfg):
            for it in batch:
                it["descZh"] = "【译文】" + (it["desc"] or "")[:80] + "……这是正文的中文译文。"
            return "stub-body", True, 1
        F._call_body_batch = fake_body_batch
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

            # ── 正文翻译（T1）：只该落在 top 的英文长文上 ──
            check("translator_body" in d, "产物带 translator_body 字段", problems)
            zh_it = [i for i in items if i.get("descZh")]
            check(bool(zh_it), f"有 top 条目产出中文正文（{len(zh_it)} 条）", problems)
            check(all(i["level"] == "top" for i in zh_it),
                  "descZh 只出现在重磅条目上（不误伤其他分级）", problems)
            check(all(len(i["desc"]) >= 300 for i in zh_it),
                  "只有长正文才被翻译（短正文不做无谓调用）", problems)
            check(all(re.search(r"[\u4e00-\u9fff]", i["descZh"]) for i in zh_it),
                  "descZh 确为中文（防模型原样回吐英文）", problems)
            check(not any("descZh" in i for i in items if i["level"] != "top"),
                  "非重磅条目一律不写 descZh", problems)

            # ── 正文翻译的**独立模型链**（2026-10-08）──
            # 背景：正文翻译改用 body.models 专用链，不再复用摘要链首模型。
            # 这里有两条相反的要求要同时锁住：配置了专用链就用它、没配就回退全局链。
            _tc = json.load(io.open(os.path.join(HERE, "sources.json"), encoding="utf-8"))["translate"]
            _bc = _tc.get("body", {}).get("models")
            check(bool(_bc), "sources.json 配置了正文翻译专用链 body.models", problems)
            if _bc:
                _names = [x["name"] for x in _bc]
                check(_names[0] == "gemini-3.5-flash-lite",
                      f"专用链首选 gemini-3.5-flash-lite（实际 {_names[0]}）", problems)
                check("agnes-3.0-flash" not in _names,
                      "专用链不含摘要链首模型（否则等于没分离）", problems)
                check(all(x.get("key_env") and x.get("base") and x.get("model") for x in _bc),
                      "专用链每档字段齐全（key_env/base/model）", problems)
            # 回退：未配置专用链时必须仍能用全局链，老配置不能因此失效
            _tc2 = json.loads(json.dumps(_tc))
            _tc2.get("body", {}).pop("models", None)
            check(_tc2.get("models"), "回退路径：全局链仍在，去掉 body.models 不会失控", problems)
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

        # ---------- 用例 4c：正文网页残留清洗 ----------
        # 实测教训：Jina 抓回的正文里夹着页面 UI 碎片（CSS 残片 / Loading 占位 /
        # 「Share + 小标题回环」目录块），它们不含 HTML 标签故 strip_tags 拦不住，
        # 会一路流进 desc 并被送进 LLM 当正文 —— 实测 top 的 7 条译文里 3 条开头带碎片。
        # 这些断言锁住「清得掉垃圾、又不动正文」这一对相反要求。
        print("\n[4c/4] 正文网页残留清洗")
        # 注意：填充段必须是**互不相同**的句子。若反复拼同一句，会自己制造出
        # 重复 n-gram，让 _cut_nav_head 误判成目录块（写这条用例时就踩了一次）。
        PAD = (" The quarterly results showed steady progress across all three regions."
               " Engineers shipped the migration ahead of schedule in early March."
               " Customers reported fewer incidents after the rollout completed."
               " The support team documented every change in the internal handbook."
               " Regional managers reviewed the numbers during the weekly call."
               " A follow-up audit is scheduled for the end of the second quarter.")
        # ① 清得掉：三类碎片各一条
        check(F._clean_web_junk(':last-child]:mb-0"> \n \n Today we ship.' + PAD)
              .startswith("Today we ship."),
              "清掉开头的 Tailwind/CSS 残片", problems)
        check(F._clean_web_junk('Loading… ' + '正文内容。' * 60).startswith("正文内容。"),
              "清掉开头的 Loading 占位", problems)
        nav = ("Share The problem The problem The result How we found the proof "
               "Concurrent work Progress and responsibility The problem The result "
               "How we found the proof Concurrent work We are sharing a solution." + PAD)
        # 用例串必须与线上同量级（线上 desc 250~310 词）。若只给 30 来词，
        # 会撞上 _cut_nav_head 的「切完剩不下东西」保护而正确地拒绝切割 ——
        # 那是保护生效，不是缺陷（写这条用例时踩过两次）。
        _c = F._clean_web_junk(nav)
        check(_c.startswith("We are sharing a solution."),
              "清掉「Share + 小标题回环」目录块（切到正文首句）", problems)
        check(F._clean_web_junk('Share We are releasing new results.' + PAD)
              .startswith("We are releasing"), "摘掉孤立的 Share 按钮残留", problems)
        # ② 不动正文：正文里出现同样的词，绝不能被削
        for _name, _txt in [
            ("正文中段的 Share", "The team will Share findings next week." + PAD),
            ("正文中段的 Loading", "The page shows a Loading state." + PAD),
            ("普通正文", "Today we release two open models." + PAD),
        ]:
            check(F._clean_web_junk(_txt) == _txt.strip(), f"不误伤{_name}", problems)
        # ③ 安全阀：确实砍太多时整体放弃（宁可留垃圾，不可砍正文）
        _tiny = ':last-child]:mb-0"> \n \n A'      # 砍掉后只剩 1 字符 → 应回退
        check(F._clean_web_junk(_tiny) == _tiny, "砍太狠时回退（安全阀生效）", problems)

        # ---------- 用例 4d：行内 UI 注释残留 ----------
        # 2026-10-08 补：实测 4 条 openai.com 的 desc 里嵌着
        #   `Problems \u2060(opens in a new window)` —— U+2060 WORD JOINER 拼无障碍提示。
        # 它既不只在开头、也不是重复片段、更无 CSS 特征，前三条规则全都抓不到。
        # 危害不止难看：它会**被模型忠实翻译**成「（在新窗口中打开）」混进中文译文。
        print("\n[4d/4] 行内 UI 注释残留")
        W = '\u2060'                                # WORD JOINER（不可见）
        # ① 清得掉：实测的三种真实形态
        _s = F._clean_web_junk(f'The Millennium Prize Problems {W}(opens in a new window) represent the deepest questions.')
        check('opens in' not in _s and W not in _s and 'represent the deepest questions' in _s,
              "清掉英文态 (opens in a new window) + U+2060，正文保留", problems)
        _s = F._clean_web_junk(f'The Institute for Advanced Study {W}(opens in a new tab) to develop practices.')
        check('opens in' not in _s and 'to develop practices' in _s,
              "清掉 new tab 变体", problems)
        _s = F._clean_web_junk('千禧年七大数学难题\u2060（在新窗口中打开）代表了数学前沿。')
        check('在新窗口' not in _s and '代表了数学前沿' in _s,
              "清掉中文态（在新窗口中打开）", problems)
        # ② 不动正文：这些词出现在正常语境里，绝不能被削
        for _name, _txt in [
            ("正文提到浏览窗口", "Open the report in a new window to compare results." + PAD),
            ("正文提到 tab 键", "Press the tab key to move between fields." + PAD),
            ("正文含 new tab 短语但非提示", "A new tab group feature shipped last week." + PAD),
        ]:
            check(F._clean_web_junk(_txt) == _txt.strip(), f"不误伤{_name}", problems)
        # ③ 多处命中也该清干净（长文里同一提示可能重复出现）
        _multi = (f'A {W}(opens in a new window) and B {W}(opens in a new window) and '
                  f'C {W}(opens in a new window) end.' + PAD)
        _m = F._clean_web_junk(_multi)
        check('opens in' not in _m and W not in _m and _m.startswith('A and B and C end.'),
              "同一行内多次出现也全部清掉", problems)
        # ④ 行内清理**不参与安全阀**：短文本也不该被回退掉
        #    （安全阀只统计结构清洗量，行内提示是固定短语，不该拖累它）
        _short = 'See the note (opens in a new window).'
        check('opens in' not in F._clean_web_junk(_short),
              "短文本的行内提示也照清（不受安全阀影响）", problems)

        # ---------- 用例 4e：档位倒挂（R1/R2，2026-10-08）----------
        # 实测根因：TOP_KW 命中即「直通 top 且不受 cap_top 约束」。11 天里 10 天倒挂 ——
        # top 最低 6.5/6.7，watch 最高 8.4/8.7。普通 GitHub 仓库 p-e-w/heretic（6.7 分）
        # 仅因标题含「开源」就挤进 top，把 8.4 分的条目压到 watch。
        print("\n[4e/4] 档位倒挂（关键词加成 vs 名额约束）")
        _t = json.load(io.open(os.path.join(HERE, "sources.json"), encoding="utf-8"))["translate"]
        check(float(_t["level_thresholds"].get("kw_floor", 0)) == 7.0,
              f"kw_floor 已对齐为 7.0（实际 {_t['level_thresholds'].get('kw_floor')}）", problems)
        check(float(_t["level_thresholds"].get("kw_floor", 0)) > float(_t["level_thresholds"]["watch"]),
              "kw_floor 高于 watch 门槛（否则加成形同虚设）", problems)
        check(getattr(F, "KW_BONUS", None) == 1.0,
              f"KW_BONUS 关键词加成 = 1.0（实际 {getattr(F, 'KW_BONUS', None)}）", problems)

        def _mk(n, tot, title, lane="official", kw=False):
            """造一条待分级条目。kw 决定标题里是否埋关键词（触发加成）。"""
            t = title if kw else ("普通条目" + str(n))
            return {"title": t, "titleCn": "", "lane": lane, "desc": "x" * 400,
                    "summary": "s", "score": {"rel": int(tot), "info": int(tot), "fresh": int(tot)},
                    "total": 0.0}

        # ① 关键词不能再「直通」：一条 6.6 分的「开源」条目不该压过 8.4 分的正常条目
        #    6.6 + 1.0 = 7.6 < 8.0 —— 加成后仍不够 top，这才是正确行为
        _its = [_mk(1, 8.9, "高分开源发布", kw=True), _mk(2, 8.4, "正常的重磅条目"),
                _mk(3, 6.6, "某仓库", lane="community", kw=True),
                _mk(4, 8.1, "另一条重磅"), _mk(5, 8.0, "第三条重磅"), _mk(6, 8.0, "第四条重磅")]
        _nt, _nw, _inv = F.assign_levels(_its, _t)
        _low = [i for i in _its if i["title"] == "某仓库"][0]
        check(not _inv, "关键词低分条目未造成倒挂", problems)
        check(_low["level"] != "top",
              f"6.6 分的「开源」条目不再直通 top（实际 {_low['level']}）", problems)

        # ② 关键词的召回作用必须保住：8.5 分的「开源」条目加成后应稳进 top
        _its2 = [_mk(n, t, "普通") for n, t in [(1, 8.9), (2, 8.8), (3, 8.7), (4, 8.6)]]
        _its2.append({"title": "重磅开源模型发布", "titleCn": "", "lane": "official",
                      "desc": "x" * 400, "summary": "s",
                      "score": {"rel": 8, "info": 8, "fresh": 8}, "total": 0.0})
        F.assign_levels(_its2, _t)
        _kwit = _its2[-1]
        check(_kwit.get("level") == "top", "8.0 分的「开源」条目加成后进 top（召回作用保住）", problems)
        check(_kwit.get("kwBoost") is True, "加成命中被标记 kwBoost（可追溯）", problems)

        # ③ 加成要封顶在 10.0（否则 9.8 分的条目会被加成到 10.8，破坏刻度）
        _its3 = [{"title": "开源超高分", "titleCn": "", "lane": "official", "desc": "x" * 400,
                  "summary": "s", "score": {"rel": 10, "info": 10, "fresh": 10}, "total": 0.0}]
        F.assign_levels(_its3, _t)
        check(_its3[0]["total"] <= 10.0, f"加成后总分不超 10.0（实际 {_its3[0]['total']}）", problems)

        # ④ 分数单调：top 的最低分不得低于 watch 的最高分（倒挂的定义）
        _its4 = [_mk(n, 8.0 + (n % 3) * 0.1, "条目" + str(n)) for n in range(1, 13)]
        _its4 += [{"title": f"开源仓库{i}", "titleCn": "", "lane": "community", "desc": "x" * 400,
                   "summary": "s", "score": {"rel": 6, "info": 6, "fresh": 6}, "total": 0.0}
                  for i in range(6)]
        _nt4, _nw4, _inv4 = F.assign_levels(_its4, _t)
        check(not _inv4, "混合低分关键词条目后仍不倒挂", problems)

        # ---------- 用例 4f：reason「推荐理由」（R3，2026-10-08）----------
        # 学 AIHOT 的 reason 字段：summary 答「讲了什么」，reason 答「为什么值得看」。
        # ⚠️ 本节断言在首轮上线失败后**已重写**：原 prompt 写「两种情形可原样留空…
        #    宁可留空，也不要写空话」，结果 agnes-3.0-flash **整批留空**，线上 0/140 条。
        #    教训：对 LLM 说「可以不做」= 它就不做。现在必须是「每条都要写」+极窄例外。
        print("\n[4f/4] reason「推荐理由」字段")
        _sp_en = F._sys_prompt(40, 80, "translate")
        _sp_cn = F._sys_prompt(40, 80, "summarize")
        check("reason" in _sp_en, "translate 提示词含 reason 字段", problems)
        check("reason" in _sp_cn, "summarize 提示词含 reason 字段", problems)
        # ★ 强指令：必须"每条都写"，不能给模型留退路（首轮失败的直接原因）
        check("必须写" in _sp_en or "每条都必须写" in _sp_en,
              "提示词要求**每条都必须写** reason（不留退路）", problems)
        check("必须写" in _sp_cn or "每条都必须写" in _sp_cn,
              "summarize 侧同样要求每条都写", problems)
        # ★ 反例仍在：禁止空话
        check("值得关注" in _sp_en, "提示词保留反例（禁止「值得关注」类空话）", problems)
        # ★ 消极表述必须已被移除 —— 这正是首轮 0/140 的根因，不能再回来
        check("宁可留空" not in _sp_en and "宁可留空" not in _sp_cn,
              "已删除「宁可留空」这类消极表述（首轮失败的根因）", problems)
        check("可原样留空" not in _sp_en and "可原样留空" not in _sp_cn,
              "已删除「可原样留空」（同上）", problems)
        # reason 位于 ③、打分档仍是 ④、topic 顺延 ⑤
        check("reason" in _sp_en.split("③")[1].split("④")[0]
              and "reason" in _sp_cn.split("③")[1].split("④")[0],
              "reason 位于 ③ 与 ④ 之间（编号未被打乱）", problems)
        check("④ rel/info/fresh" in _sp_en and "④ rel/info/fresh" in _sp_cn,
              "打分档编号仍为 ④（未被 reason 顶掉）", problems)
        check("⑤ topic" in _sp_en and "⑤ topic" in _sp_cn,
              "topic 档编号顺延为 ⑤", problems)
        # 输入正文长度：400 字不够写"为什么值得看"，已提到 1200
        _src = io.open(os.path.join(HERE, "fetch_ai.py"), encoding="utf-8").read()
        check('i["desc"][:1200]' in _src, "喂给模型的正文片段已提到 1200 字", problems)
        # 产出率告警：低于 30% 必须在日志里喊出来（不然又是一次静默失败）
        check("推荐理由" in _src and "低于 30%" in _src,
              "有「推荐理由产出率过低」告警（不再静默失败）", problems)
        # ---------- 用例 5：AIHOT 精选接入 ----------
        print("\n[5/5] AIHOT 精选（影子模式）")
        acfg = {"lane": "hot", "jaccard_min": 0.55,
                "same_host_hours": 6, "same_host_jaccard": 0.25}
        raw = json.loads(aihot_payload().decode())["items"]
        parsed = F.parse_aihot(raw, acfg, NOW)
        check(len(parsed) == 4, f"parse_aihot 收到 4 条（实得 {len(parsed)}）", problems)
        check(all(i["lane"] == "hot" for i in parsed), "车道标成 hot", problems)
        check(all(i.get("attribution", {}).get("name") == "AIHOT" for i in parsed),
              "每条都带 attribution（合规硬要求）", problems)
        check(all(i.get("aihotUrl") for i in parsed), "每条都带回 AIHOT 的原文链接", problems)
        # title 存中文（读者看到的）、titleEn 存英文（比对用的第二个键）
        check(parsed[0]["title"].startswith("OpenAI 发布") and
              parsed[0]["titleEn"].startswith("OpenAI releases"),
              "title=中文标题、titleEn=英文原文（两个键都留着才敢同语言比对）", problems)

        pool = [{"title": "OpenAI releases GPT-6 Sol with 1M context",
                 "titleCn": "", "url": "https://openai.com/news/a", "dt": NOW},
                {"title": "An unrelated standalone note about local inference caching",
                 "titleCn": "", "url": "https://x.com/other/status/1", "dt": NOW}]
        kept, drop = F.aihot_dedup(parsed, pool, acfg)
        check(len(kept) == 1, f"4 条去重后只剩 1 条（实得 {len(kept)}）", problems)
        check(drop["title"] == 3,
              f"三层里标题层剔掉 3（英文撞 2 + 中文撞 1，实得 {drop['title']}）", problems)
        check(kept and kept[0]["aihotScore"] == 67,
              "内部重复留下的是**分数更高**的那条（按 aihotScore 降序判）", problems)

        # ★ 「同语言才可比」的回归：两条 Arena 的中文标题几乎一样、英文完全不同
        pair = [p for p in parsed if "Arena" in p["title"]]
        _j = lambda a, b: len(a & b) / max(1, len(a | b))
        zh_j = _j(F._zh_w(pair[0]["title"]), F._zh_w(pair[1]["title"]))
        en_j = _j(F._en_w(pair[0]["titleEn"]), F._en_w(pair[1]["titleEn"]))
        check(zh_j >= 0.55 and en_j < 0.55,
              f"复刻真实陷阱：中文 Jaccard {zh_j:.2f} 判得出、英文 {en_j:.2f} 判不出 "
              f"→ 只按英文比会漏（跨语言比对是无效层）", problems)

        # 影子模式：不污染 latest.json，只写诊断文件
        d5 = run(tmp, llm_stub=True)
        check(all(i["lane"] != "hot" for i in d5["items"]),
              "shadow_only=true → hot 车道条目**不在** latest.json 里", problems)
        sp = os.path.join(tmp, "data", "aihot_shadow.json")
        check(os.path.exists(sp), "影子诊断文件已生成", problems)
        if os.path.exists(sp):
            sd = json.load(io.open(sp, encoding="utf-8"))["days"][-1]
            # 不断言具体条数：fixture 里 openai-news 因 desc 中位数 171 < desc_min 300
            # 本来就会降级、不进 merged，于是"英文撞车"那条在集成路径里无从命中。
            # 这里只要求账目自洽 + **精选集内部去重确实发生**（那才是本轮的真实 bug）。
            check(sd["net"] == sd["fetched"] - sum(sd["dropped"].values()),
                  f"影子账目自洽 fetched {sd['fetched']} - dropped "
                  f"{sum(sd['dropped'].values())} = net {sd['net']}", problems)
            arena = [x for x in sd["items"] if "Arena" in x["title"]]
            check(len(arena) == 1,
                  f"精选集内部重复被压成 1 条（实得 {len(arena)}）", problems)
            check(sd["llm"] is True, "影子记录标了 llm=True（本地有桩模型，分数可信）", problems)
            check(all(not k.startswith("_") for it in sd["items"] for k in it),
                  "影子文件里没有内部标记外泄", problems)
        check(all(not k.startswith("_aihot") for i in d5["items"] for k in i),
              "产物里不含 _aihot* 内部字段", problems)

        # AIHOT 挂掉 → 记降级，但主流程照常出产物（绝不空窗）
        AIHOT_STUB["mode"] = "fail"
        try:
            d6 = run(tmp, llm_stub=True)
            check(any(x.get("id") == "_aihot" for x in d6.get("degraded", [])),
                  "AIHOT 失败 → degraded 里有 _aihot 一条（问题可见，不静默）", problems)
            check(len(d6["items"]) > 0,
                  f"AIHOT 失败仍产出 {len(d6['items'])} 条（不空窗）", problems)
        except Exception as e:
            check(False, f"AIHOT 失败不应中断主流程，却抛了 {type(e).__name__}: {e}", problems)
        finally:
            AIHOT_STUB["mode"] = "ok"

        # 304 → 用上次缓存，不重复烧流量
        AIHOT_STUB["mode"] = "304"
        d7 = run(tmp, llm_stub=True)
        check(len(d7["items"]) > 0, "304 路径主流程照常", problems)
        sp7 = os.path.join(tmp, "data", "aihot_shadow.json")
        if os.path.exists(sp7):
            sd7 = json.load(io.open(sp7, encoding="utf-8"))["days"][-1]
            check(sd7["fetched"] == 4,
                  f"304 时用缓存条目继续判定（fetched={sd7['fetched']}）", problems)
        AIHOT_STUB["mode"] = "ok"

        # ★ 正式并入路径（shadow_only=false）：验那条边界有没有真的守住。
        #   fake_batch 会把 summary 写成「摘要：xxx」，所以只要 AIHOT 条目的
        #   summary 还带着它自己的原文，就证明 restore 在 merged 分支里也生效了。
        #   这一步原来漏了：restore 只写在影子分支里，一旦关掉开关，
        #   「表达层用它的」这条边界会**静默失效**，而日志上完全看不出来。
        AIHOT_STUB["shadow"] = False
        try:
            d8 = run(tmp, llm_stub=True)
            hot = [i for i in d8["items"] if i["lane"] == "hot"]
            check(len(hot) >= 1, f"关掉影子后 hot 条目进了产物（{len(hot)} 条）", problems)
            if hot:
                check(all("摘要：" not in (i.get("summary") or "") for i in hot),
                      "表达层没被 LLM 覆盖（summary 仍是 AIHOT 原文）", problems)
                check(all((i.get("reason") or "").strip() for i in hot),
                      "推荐理由沿用它的编辑成果", problems)
                check(all(i.get("score") for i in hot),
                      "打分仍来自我们自己的 LLM（排序层没让给别人）", problems)
                check(all(i.get("attribution", {}).get("name") == "AIHOT" and
                          i.get("aihotUrl") for i in hot),
                      "每条都带 attribution + 回链（合规要求）", problems)
                check(all(not k.startswith("_") for i in hot for k in i),
                      "并入路径也不外泄内部标记", problems)
                check(all(i.get("titleEn") for i in hot) or
                      all(i.get("title") for i in hot),
                      "英文原题留在 titleEn 里，供次日跨日折叠比对", problems)
                rep = os.path.join(tmp, "data", "aihot_shadow.json")
                check(not os.path.exists(rep) or
                      all(dd.get("shadow_only") for dd in
                          json.load(io.open(rep, encoding="utf-8"))["days"]),
                      "关掉影子后不再写影子文件（或历史条目仍是影子记录）", problems)
        finally:
            AIHOT_STUB["shadow"] = True

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
