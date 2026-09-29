#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 前沿雷达 · 离线冒烟测试（不联网、不依赖 Secrets）。

为什么必须有这个：`py_compile` 只查语法，**查不出 NameError**。实测教训——因为同一
文件的多条并行编辑互相覆盖，`lib_items = []` 的初始化丢了而引用还在，本地「语法通过」
却让 Actions 上的正式抓取直接崩掉。凡是 main() 里新增的分支，都应该在这里被跑到。

覆盖点：
  · 五种专用 parser（leaderboard / openrouter / hf-models / hf-spaces / awesome-list）
  · 双通路与 desc 门槛、时间窗保底、跨源去重、跨日折叠
  · library_only 源只进案例库、不进简报
  · LLM 增强两条路径：① 无 Key 全降级 ② 正常返回（含 want_type 分组）
  · 案例库累积与缓存复用（第二次运行应命中缓存、不重复入库）
  · 产物字段无内部标记外泄（_lib / _want_type / _skip_llm）

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
    # —— text / awesome-list（library_only）——
    "https://raw.githubusercontent.com/Shubhamsaboo/awesome-llm-apps/main/README.md":
        ("# Awesome\n## 🙏 Thanks\n### 🌱 Starter AI Agents\n\n"
         "*   [🎙️ AI Blog to Podcast Agent](starter_ai_agents/blog/) - Turn a blog into a podcast\n"
         "*   [🩻 AI Medical Imaging Agent](starter_ai_agents/med/) - X-ray analysis with Gemini\n\n"
         "### ♾️ MCP AI Agents\n\n"
         "*   [MCP Filesystem Agent](mcp_ai_agents/fs/) - Let an agent read files safely\n"
         "*   [MCP Slack Agent](mcp_ai_agents/slack/) - Post to Slack from an agent\n").encode(),
}

TEST_IDS = ["openai-news", "v2ex-create", "hn-show", "openrouter-stealth",
            "llm-leaderboard", "hf-new-models", "hf-spaces-trend", "awesome-llm-apps"]


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
                it["score"] = {"rel": 9 if i == 0 else 6, "info": 8 if i == 0 else 6,
                               "fresh": 9 if i == 0 else 5}
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
        check(any(c.get("type") for c in cl), "案例库存在带 type 的条目", problems)
        check(all(c.get("type") for c in seed0) if (seed0 := [c for c in cl if c.get("block") == "案例库种子"])
              else False, "种子源条目都带 type（节名分类）", problems)
        seed = [c for c in cl if c.get("block") == "案例库种子"]
        check(len(seed) >= 4, f"library_only 源进了案例库（{len(seed)} 条）", problems)
        check(all("awesome-llm-apps" not in (i.get("url") or "") for i in items),
              "library_only 条目未混入简报", problems)
        internal = {"_lib", "_want_type", "_skip_llm", "_translate_title", "dt"}
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
        check(len(F.PARSERS) == 5, f"解析器注册表 5 项（{sorted(F.PARSERS)}）", problems)
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
