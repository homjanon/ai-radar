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
# 一条），若统一用短窗会被整体过滤干净 —— 必须按车道区分。中文媒体实测日更节奏
# 差异大（36氪 30 条 / 雷锋网 1 条），36h 会把慢的那几家压缩到只能靠保底，故放宽到 72h。
LANE_MAX_AGE = {"model": 504, "official": 168, "paper": 72, "community": 48,
                "media": 96, "cn": 72}
DEFAULT_MAX_AGE = 48

ATOM = "{http://www.w3.org/2005/Atom}"
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}encoded"


def log(m):
    print(m, flush=True)


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def http_get(url, timeout=25, retry=1):
    """带一次重试的 GET。

    ⚠️ 永久性错误（403 无权限 / 404 不存在 / 410 已下线）**不重试** —— 重试不会自愈，
    只会白白拖延（借鉴 portfolio 的踩坑复盘：MiniMax-M3 EOL 时靠重试掩盖了 410）。
    瞬时错误（超时/断连/5xx）才值得重试。
    """
    last = None
    for attempt in range(retry + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 (ai-radar/1.0)", "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}")
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (403, 404, 410):
                raise
            last = e
        except Exception as e:
            last = e
        if attempt < retry:
            time.sleep(1.5)
    raise last


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
# 专用解析器（模型发布类源）：这些源的响应结构与 RSS 差得很远，需专门拍平。
# 统一产出 [{title, desc, url, dt}]，与 parse_feed 同构 —— 下游全部逻辑直接复用。
# --------------------------------------------------------------------------- #
def parse_leaderboard(body, cfg):
    """llm-leaderboard-data 的 latest.json → 「最近上架的模型」。

    这个源的核心价值是它有 listed_at_iso（上架时间）—— OpenRouter 官方 API
    没有这个字段，原方案要靠跨日快照做差集才能算出来，现在直接读即可。
    """
    j = json.loads(body)
    ms = j.get("models") or []
    days = int(cfg.get("recent_days", 21))
    cutoff = datetime.datetime.now(UTC) - datetime.timedelta(days=days)
    out = []
    for m in ms:
        iso = (m.get("listed_at_iso") or "").strip()
        dt = parse_dt(iso) if iso else None
        if dt and dt < cutoff:
            continue
        name = m.get("display_name") or m.get("id") or ""
        if not name:
            continue
        bits = [f"{m.get('org') or '未知厂商'} 出品"]
        if iso:
            bits.append(f"{iso[:10]} 上架")
        if m.get("arena_score"):
            rk = f"（LMArena 第 {m['arena_rank']}）" if m.get("arena_rank") else ""
            bits.append(f"LMArena Elo {m['arena_score']}{rk}")
        bits.append("开源权重" if m.get("open_weights") else "闭源")
        if m.get("context_length"):
            bits.append(f"上下文 {int(m['context_length']):,} tokens")
        if m.get("price_in") is not None:
            bits.append(f"定价 ${m['price_in']}/${m.get('price_out')} 每百万 tokens")
        out.append({
            "title": f"新模型上架：{name}",
            "desc": "；".join(bits),
            "url": f"https://openrouter.ai/{m.get('id', '')}",
            "dt": dt,
        })
    out.sort(key=lambda x: x["dt"] or datetime.datetime(1970, 1, 1, tzinfo=UTC),
             reverse=True)
    return out[: int(cfg.get("take", 12))]


def parse_openrouter(body, cfg):
    """OpenRouter /api/v1/models → 条目。

    filter=stealth 时只保留匿名公测模型 —— 实测这些条目 id 一律以 stealth/ 开头
    （如 stealth/space-bunny-alpha）。这是「有个模型正在匿名公测」的唯一结构化
    入口：规律是匿名免费预览 → 免费期结束揭晓身份（Union Alpha→Pareto 即此路径）。
    """
    j = json.loads(body)
    arr = j.get("data") or []
    stealth = cfg.get("filter") == "stealth"
    out = []
    for m in arr:
        mid = (m.get("id") or "").strip()
        name = (m.get("name") or mid).strip()
        if not mid:
            continue
        # 严格只认 stealth/ 前缀：名字里带 alpha 的普通模型（如 Mancer: Weaver (alpha)）
        # 不是匿名公测，按名字匹配会误捞。
        if stealth and not mid.lower().startswith("stealth/"):
            continue
        pr = m.get("pricing") or {}
        bits = []
        if m.get("context_length"):
            bits.append(f"上下文 {int(m['context_length']):,} tokens")
        if pr.get("prompt") not in (None, ""):
            bits.append(f"输入 ${pr.get('prompt')} / 输出 ${pr.get('completion')} 每百万 tokens")
        mods = (m.get("architecture") or {}).get("input_modalities") or []
        if mods:
            bits.append("输入模态 " + "/".join(str(x) for x in mods))
        if m.get("description"):
            bits.append(re.sub(r"\s+", " ", str(m["description"]))[:260])
        out.append({
            "title": (f"匿名公测模型：{name}" if stealth else f"模型上架：{name}"),
            "desc": "；".join(bits),
            "url": f"https://openrouter.ai/{mid}",
            "dt": None,          # OpenRouter 不提供上架时间，由 leaderboard 源补足
        })
    return out[: int(cfg.get("take", 10))]


def parse_hf_models(body, cfg):
    """Hugging Face /api/models → 新上架的模型权重。

    开源模型的「无声发布」现场：传了权重、有模型卡，但没有一篇新闻稿。
    两道降噪（实测必需）：① exclude_tags 剔掉量化/微调衍生品（gguf/awq/lora…）；
    ② min_likes 门槛 —— `sort=createdAt` 返回的绝大多数是个人测试仓库
    （实测首批 8 条全是 downloads=0 / likes=0 的占位仓），不设门槛会直接刷屏。
    真正的机构发布在几小时内就会有点赞，所以这个门槛不会漏掉有价值的。
    """
    j = json.loads(body)
    arr = j if isinstance(j, list) else []
    excl = set(str(t).lower() for t in (cfg.get("exclude_tags") or []))
    min_likes = int(cfg.get("min_likes", 2))
    out = []
    for m in arr:
        mid = (m.get("modelId") or m.get("id") or "").strip()
        if not mid:
            continue
        if int(m.get("likes") or 0) < min_likes:
            continue
        tags = [str(t).lower() for t in (m.get("tags") or [])]
        if excl & set(tags):
            continue
        bits = []
        if m.get("createdAt"):
            bits.append(f"创建于 {str(m['createdAt'])[:10]}")
        if m.get("downloads") is not None:
            bits.append(f"下载 {int(m['downloads']):,}")
        if m.get("likes") is not None:
            bits.append(f"点赞 {int(m['likes'])}")
        keep = [t for t in tags if t not in excl][:6]
        if keep:
            bits.append("标签 " + ", ".join(keep))
        out.append({
            # 标题刻意不含"权重"二字 —— 否则会命中 TOP_KW 里的关键词，
            # 把一堆个人测试仓库硬提成"重磅"（实测踩过）
            "title": f"HF 新模型：{mid}",
            "desc": "；".join(bits),
            "url": f"https://huggingface.co/{mid}",
            "dt": parse_dt(str(m["createdAt"])) if m.get("createdAt") else None,
        })
    return out[: int(cfg.get("take", 8))]


def parse_hf_spaces(body, cfg):
    """Hugging Face /api/spaces → AI 应用（Spaces 是「AI 应用」的托管现场）。

    与 hf-models 的区别：那边是模型权重（谁训了模型），这边是**能直接用的应用**
    （谁做出了东西）。trendingScore 比下载量更早反映风向。
    """
    j = json.loads(body)
    arr = j if isinstance(j, list) else []
    out = []
    for s in arr:
        sid = (s.get("id") or "").strip()
        if not sid:
            continue
        bits = []
        if s.get("sdk"):
            bits.append(f"技术栈 {s['sdk']}")
        if s.get("likes") is not None:
            bits.append(f"点赞 {int(s['likes'])}")
        if s.get("createdAt"):
            bits.append(f"创建于 {str(s['createdAt'])[:10]}")
        if s.get("lastModified"):
            bits.append(f"最近更新 {str(s['lastModified'])[:10]}")
        out.append({
            "title": f"Spaces 应用：{sid}",
            "desc": "；".join(bits),
            "url": f"https://huggingface.co/spaces/{sid}",
            "dt": parse_dt(str(s["lastModified"])) if s.get("lastModified") else None,
        })
    return out[: int(cfg.get("take", 6))]


# awesome-llm-apps 的分节名 → 中文类型。节名自带分类信息，比让 LLM 猜准得多。
AWESOME_SECTION_CN = {
    "agent skills": "Agent 技能",
    "starter ai agents": "入门 Agent",
    "advanced ai agents": "进阶 Agent",
    "always-on agents": "常驻 Agent",
    "multi-agent teams": "多 Agent 协作",
    "voice ai agents": "语音 Agent",
    "generative ui and agentic frontends": "生成式 UI",
    "autonomous game-playing agents": "游戏 Agent",
    "mcp ai agents": "MCP Agent",
    "rag (retrieval augmented generation)": "RAG 检索增强",
    "ai browser tools": "浏览器工具",
    "llm apps with memory": "带记忆的应用",
    "chat with x": "数据问答",
    "llm optimization tools": "推理优化",
    "llm fine-tuning": "微调",
    "ai agent framework crash courses": "框架教程",
}


def parse_awesome_list(body, cfg):
    """解析 awesome 类 README（markdown）→ 案例条目。

    格式实测为：
        ### 🌱 Starter AI Agents
        *   [🎙️ AI Blog to Podcast Agent](starter_ai_agents/xxx/) - Turn any blog URL into...
    节名即分类（直接拿来当 type），条目即案例。这类源无日期、非新闻，故在配置里标
    library_only=true —— 只进案例库、不进每日简报。
    """
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
    repo = (cfg.get("repo") or "Shubhamsaboo/awesome-llm-apps")
    branch = cfg.get("branch") or "main"
    section, out = "", []
    for line in text.splitlines():
        h = re.match(r"^#{2,3}\s+(.+?)\s*$", line)
        if h:
            section = re.sub(r"[^\w\s()\-/]", "", h.group(1)).strip()   # 去 emoji
            continue
        m = re.match(r"^\s*[-*]\s+\[(.+?)\]\(([^)]+)\)\s*(?:[-–—]\s*(.*))?$", line)
        if not m:
            continue
        title = re.sub(r"[^\w\s()\-/+.&:'!?,]", "", m.group(1)).strip()
        rel = m.group(2).strip()
        desc = (m.group(3) or "").strip()
        if not title or rel.startswith("http"):
            continue
        rel = rel.rstrip("/")
        t = AWESOME_SECTION_CN.get(section.lower(), section or "其他")
        out.append({
            "title": title,
            "desc": desc or title,
            "url": f"https://github.com/{repo}/tree/{branch}/{rel}",
            "dt": None,
            "apptype": t,          # 节名即分类
        })
    # 按分类轮转取样：README 里各节长度悬殊（RAG 21 条 vs 微调 2 条），
    # 直接取前 N 条会让大节挤掉其它形态，案例库就失衡了。
    buckets = {}
    for it in out:
        buckets.setdefault(it["apptype"], []).append(it)
    keys, ordered = list(buckets), []
    while any(buckets[k] for k in keys):
        for k in keys:
            if buckets[k]:
                ordered.append(buckets[k].pop(0))
    return ordered[: int(cfg.get("take", 40))]


PARSERS = {
    "leaderboard": parse_leaderboard,
    "openrouter": parse_openrouter,
    "hf-models": parse_hf_models,
    "hf-spaces": parse_hf_spaces,
    "awesome-list": parse_awesome_list,
}


# --------------------------------------------------------------------------- #
# 抓取：双通路 + desc 门槛
# --------------------------------------------------------------------------- #
def try_once(url, kind, src=None):
    body = http_get(url)
    p = (src or {}).get("parser")
    if p in PARSERS:
        # 专用解析器允许返回空 —— "过滤后没有合格条目"是正常结果（如 HF 当天
        # 只有个人测试仓），不能等同于抓取失败
        return PARSERS[p](body, src)
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
            items = try_once(url, kind, src)
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
def jina_fetch(url, cap=1500):
    """补抓正文。cap 取 1500 字：足够 P2 生成 40–80 字摘要，
    又不至于让产物体积和后续 token 消耗失控（实测不设限时每条都顶到 4000）。"""
    clean = re.sub(r"^https?://", "", url)
    body = http_get(f"https://r.jina.ai/https://{clean}", timeout=30)
    text = body.decode("utf-8", "ignore")
    text = re.sub(r"^Title:.*$", "", text, flags=re.M)
    text = re.sub(r"^URL Source:.*$", "", text, flags=re.M)
    text = re.sub(r"^Markdown Content:.*$", "", text, flags=re.M)
    return strip_tags(text)[:cap]


# --------------------------------------------------------------------------- #
# LLM 增强（P2）：中文摘要 + 三维打分 + 主题标签
#   分工原则：LLM 只做「表达与评分」，不做选条 —— 选条仍是上一步的硬规则。
#   按语言拆批（学自 news-feed 的教训）：同一 prompt 里混"译标题"与"标题原样"
#   两条互斥指令，模型会整批统一处理，导致英文标题漏译。
# --------------------------------------------------------------------------- #
def _sys_prompt(smin, smax, mode, want_type=False):
    extra = ("⑤ type：该案例的**应用形态**，必须从以下固定词表里选一个（不要自创、不要组合）："
             "Web 应用 / 移动 App / 浏览器插件 / 桌面工具 / CLI 工具 / Agent 工作流 / "
             "模型与推理 / 数据分析 / 内容生成 / 效率工具 / 其他。")
    base = (
        f"② summary：{smin}~{smax} 字的中文摘要，讲清核心事实（谁做了什么 + 关键数字或结论）；"
        "信息完整优先于字数，不逐字照抄、不以半句截断。"
        "③ rel/info/fresh：三个 0-10 的**整数**评分。"
        "**必须严格区分档位，不要普遍给高分** —— 实测若评分集中在 8-9 分，筛选就失去意义。各档定义："
        "rel（与 AI 前沿的相关度）：直接涉及大模型 / Agent / Skill / 论文 / 开源生态的**实质进展** = 8-10；"
        "行业应用、商业案例、人物观点、活动与招聘 = 5-7；与 AI 关联很弱或纯营销 = 0-4。"
        "info（信息密度）：含具体数字、技术细节、可复现结论 = 8-10；有信息但较浅 = 5-7；"
        "纯观点、宣传、入门科普 = 0-4。"
        "fresh（时效性）：首次发布或刚发生的事件 = 8-10；一周内的持续讨论 = 5-7；"
        "回顾、长期有效内容 = 0-4。"
        "④ topic：4-8 字的中文主题标签（如 模型发布 / 开源权重 / Agent 框架 / 融资并购 / 政策监管 / 论文方法 / 工程实践）。"
        + (extra if want_type else "")
        + "只输出 JSON 数组本身，不要任何解释、不要 markdown 代码块。")
    tn = ",\"type\":形态" if want_type else ""
    if mode == "translate":
        return ("你是 AI 技术情报编辑。输入是 JSON 数组 [{\"i\":序号,\"title\":英文标题,\"desc\":正文片段}]。"
                "**本批全部条目均为英文。**"
                "输出 JSON 数组 [{\"i\":序号,\"title\":中文标题,\"summary\":中文摘要,"
                "\"rel\":整数,\"info\":整数,\"fresh\":整数,\"topic\":中文标签" + tn + "}]，规则："
                "① title：**必须译成简洁中文**（专有名词保留通用写法，如 GPT-6、Claude、LangChain），"
                "不得原样保留英文、不得留英文残句。" + base)
    return ("你是 AI 技术情报编辑。输入是 JSON 数组 [{\"i\":序号,\"title\":中文标题,\"desc\":正文片段}]。"
            "**本批全部条目均为中文。**"
            "输出 JSON 数组 [{\"i\":序号,\"title\":标题,\"summary\":中文摘要,"
            "\"rel\":整数,\"info\":整数,\"fresh\":整数,\"topic\":中文标签" + tn + "}]，规则："
            "① title：**必须一字不改原样返回输入标题**，不要改写、不要润色、不要增删字词。" + base)


def _loads_array(text):
    """宽松解析模型返回的 JSON 数组。

    实测踩坑：agnes-3.0-flash 在条数较多时会返回**被 max_tokens 截断**的 JSON
    （`JSONDecodeError: Expecting ':' delimiter`），整批因此判失败、白跑一趟。
    这里回退到「从后往前找最后一个完整的 }，截断后补 ]」，把前面完整的条目救回来。
    """
    try:
        v = json.loads(text)
        if isinstance(v, list):
            return v
    except Exception:
        pass
    tried = 0
    for cut in range(len(text) - 1, -1, -1):
        if text[cut] != "}":
            continue
        tried += 1
        if tried > 40:                    # 限制回退次数，避免大文本上反复解析
            break
        cand = text[:cut + 1].rstrip().rstrip(",") + "]"
        try:
            v = json.loads(cand)
            if isinstance(v, list) and v:
                return v
        except Exception:
            continue
    raise RuntimeError("无法解析模型返回的 JSON 数组")


def _call_llm_batch(batch, tcfg, mode, want_type=False):
    """对一批条目依次尝试模型链。返回 (模型名, 是否成功, 实际发起请求的模型数)。"""
    smin = int(tcfg.get("summary_min", 40))
    smax = int(tcfg.get("summary_max", 80))
    sys_prompt = _sys_prompt(smin, smax, mode, want_type)
    tried = 0
    for m in tcfg.get("models", []):
        key = os.environ.get(m.get("key_env", ""))
        if not key:
            log(f"  ⏭️ 跳过 {m['name']}：环境变量 {m.get('key_env')} 未设置（Secret 未配置时属预期）")
            continue
        tried += 1
        payload = {
            "model": m["model"],
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": json.dumps(
                    [{"i": n, "title": i["title"], "desc": i["desc"][:400]}
                     for n, i in enumerate(batch)], ensure_ascii=False)},
            ],
            "temperature": 0.2,
            "max_tokens": 12000,          # 给足：思考链 + 25 条的 JSON 会吃不少预算
        }
        try:
            req = urllib.request.Request(
                m["base"].rstrip("/") + "/chat/completions",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.loads(r.read().decode())
            msg = data["choices"][0]["message"]
            # 商汤系答案在 reasoning_content（见实测记录），一并兼容
            text = (msg.get("content") or msg.get("reasoning_content") or "").strip()
            text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
            arr = _loads_array(text)
            # 允许部分成功：模型偶发只返回部分条目（或 JSON 被截断），
            # 只要过半就采纳，缺失的条目保留原文 —— 比整批作废划算得多。
            if len(arr) < max(1, len(batch) // 2):
                raise RuntimeError(f"返回条数过少（期望 {len(batch)}，得 {len(arr)}）")
            if len(arr) < len(batch):
                log(f"  ℹ️ {m['name']} 仅返回 {len(arr)}/{len(batch)} 条，其余保留原文")
            n_title = 0
            for row in arr:
                i = int(row.get("i", -1))
                if not (0 <= i < len(batch)):
                    continue
                it = batch[i]
                ti = str(row.get("title", "")).strip()
                s = str(row.get("summary", "")).strip()
                if it["_translate_title"] and ti:
                    # 含中文才采纳。纯专有名词标题（如 "Microsoft Data Formulator"）
                    # 本就不该硬译 —— 跳过该条、保留英文原标题，不影响整批。
                    # （原先「单条未译即抛异常」会让整批白跑并降级到下一个模型，
                    #  实测因此损失了一整批 43 条。）
                    if re.search(r"[\u4e00-\u9fff]", ti):
                        it["titleCn"] = ti
                        n_title += 1
                if s:
                    it["summary"] = s
                try:
                    it["score"] = {
                        "rel": max(0, min(10, int(row.get("rel", 0)))),
                        "info": max(0, min(10, int(row.get("info", 0)))),
                        "fresh": max(0, min(10, int(row.get("fresh", 0)))),
                    }
                except Exception:
                    pass
                tp = str(row.get("topic", "")).strip()
                if tp:
                    it["topic"] = tp[:12]
            # 整批译出率过低才判失败（防模型整体原样回吐英文而未被察觉）
            if mode == "translate":
                need = sum(1 for x in batch if x["_translate_title"])
                if need and n_title < max(1, int(need * 0.3)):
                    raise RuntimeError(f"整批仅 {n_title}/{need} 条译出（低于 30%），判定失败")
            log(f"  🌐 AI 增强完成（{m['name']}，{len(batch)} 条，译标题 {n_title} 条）")
            return m["name"], True, tried
        except Exception as e:
            log(f"  ⚠️ {m['name']} 失败：{type(e).__name__}: {str(e)[:120]}")
    return None, False, tried


def llm_enhance(items, tcfg):
    """按 (语言 × 是否应用案例) 分组 → 分批 → 逐批走模型链。

    为什么还要按「是否应用案例」再拆一次：只有 apps 车道需要多输出一个 type
    （应用形态），而输出 schema 一旦混批，模型会对整批都套用同一套字段解释。
    宁可多切几组，也不要把两种 schema 混在一批（与「按语言拆批」同源的理由）。
    _skip_llm 的条目（案例库中已处理过的）直接跳过，避免每天重译上百条。
    """
    if not tcfg.get("enabled", True):
        return "off", []
    size = int(tcfg.get("batch_size", 25))
    pend = [i for i in items if not i.get("_skip_llm")]
    groups = []
    for want_cn, mode in ((True, "translate"), (False, "summarize")):
        grp = [i for i in pend if bool(i["_translate_title"]) == want_cn]
        for wt in (True, False):
            sub = [i for i in grp if bool(i.get("_want_type")) == wt]
            if sub:
                groups.append((sub, mode, wt))
    n_en = sum(1 for i in pend if i["_translate_title"])
    n_app = sum(1 for i in pend if i.get("_want_type"))
    log(f"🧠 LLM 增强：待处理 {len(pend)} 条（英文 {n_en} / 应用案例 {n_app} / "
        f"已缓存跳过 {len(items) - len(pend)}），批大小 {size}")

    done, failed = {}, []
    for grp, mode, wt in groups:
        for k in range(0, len(grp), size):
            part = grp[k:k + size]
            tag = f"{mode}{'+type' if wt else ''}"
            name, ok, tried = _call_llm_batch(part, tcfg, mode=mode, want_type=wt)
            if ok:
                done[name] = done.get(name, 0) + len(part)
            else:
                failed.append(f"{tag}:{len(part)}")
                if tried == 0:
                    log(f"  ⏭️ {tag} 批（{len(part)} 条）无可用模型：Key 均未配置")

    if not done:
        return "none(原文)", failed
    out = " + ".join(f"{n}({c}条)" for n, c in done.items())
    if failed:
        out += f" ⚠️降级[{'、'.join(failed)}]"
    return out, failed


# 重磅关键词（硬规则提级）：只保留**高精度**的词 —— 「发布 / 推出 / 政策」这类太常见，
# 会把"发布一个活动计划"也提成重磅，那类判断交给 LLM 分数。命中即判 top，
# 保证「我关心的那类事」一定浮到首屏，不受模型某次给分偏低的影响。
TOP_KW = ("开源", "融资", "收购", "并购", "ipo", "反垄断", "监管",
          "open-source", "open source", "open weights", "open-weight",
          "release", "benchmark", "state-of-the-art", "sota", "breakthrough",
          "acquisition", "acquires", "raises", "funding round", "general availability")


def _total_of(it, tcfg):
    """加权总分 rel .45 / info .30 / fresh .25。无 LLM 分时用「车道基线 + 新鲜度」兜底。

    ⚠️ 降级路径必须也有分级 —— 若 LLM 未配置/全失败则没有 score，此时若一律判
    normal，首屏就只剩一个折叠条、页面等于空白。
    """
    sc = it.get("score") or {}
    if sc:
        total = sc.get("rel", 0) * 0.45 + sc.get("info", 0) * 0.30 + sc.get("fresh", 0) * 0.25
        # 「模型发布」车道加权：新模型上架 / 匿名公测是"技术领先"最直接的信息，
        # 应当优先浮到首屏（受名额上限约束，不会失控）。
        if it.get("lane") == "model":
            total += 0.5
        return round(min(10.0, total), 1)
    base = {"model": 9.0, "official": 7.6, "apps": 6.6, "paper": 6.2, "cn": 6.2,
            "community": 5.4, "media": 5.4}.get(it.get("lane"), 5.4)
    age = it.get("ageH", -1)
    bonus = 1.2 if 0 <= age <= 12 else (0.6 if 0 <= age <= 36 else 0.0)
    if it.get("alsoIn"):
        bonus += 0.5                          # 多源同时报道 = 重要性信号
    it["fallbackScore"] = True
    return round(min(9.5, base + bonus), 1)


def _hits_kw(it):
    hay = (it.get("title", "") + " " + it.get("titleCn", "")).lower()
    return any(k in hay for k in TOP_KW)


def assign_levels(items, tcfg):
    """分级 = 分数下限 + 名额上限。

    只用绝对阈值会在模型整体给分偏高时失控 —— 实测首轮 top 达 50/90（占 56%），
    因为打分集中在 8-9 分。加名额上限后，每天首屏条数恒定，不受打分漂移影响。
    返回 (top 数, watch 数)。
    """
    th = tcfg.get("level_thresholds", {}) or {}
    q = tcfg.get("quota", {}) or {}
    floor_top = float(th.get("top", 8.0))
    floor_watch = float(th.get("watch", 6.5))
    # 硬规则提级的分数下限：关键词能保证"不遗漏"，但也会误伤
    # （实测「AI Engineering from Scratch 开源教程」仅 4.7 分却因含"开源"进了重磅）。
    # 用宽松下限 5.0 兜住 —— 只挡明显低质/不相关的，不影响关键词的召回作用。
    kw_floor = float(th.get("kw_floor", 5.0))
    n = len(items)
    cap_top = min(int(q.get("top_max", 15)),
                  max(int(q.get("top_min", 4)), round(n * float(q.get("top_ratio", 0.12)))))
    cap_watch = max(0, round(n * float(q.get("watch_ratio", 0.35))))

    for i in items:
        i["total"] = _total_of(i, tcfg)
        i["_kw"] = _hits_kw(i)

    ranked = sorted(items, key=lambda x: -x["total"])
    n_top = n_watch = 0
    # ① 硬规则命中的先占 top（不受名额限制 —— 这类"我关心的事"必须浮上来），
    #    但仍要过 kw_floor，防低质内容靠关键词上位
    for i in ranked:
        if i["_kw"] and i["total"] >= kw_floor:
            i["level"] = "top"
            n_top += 1
    # ② 其余按分数 + 名额分配
    for i in ranked:
        if i.get("level"):
            continue
        if n_top < cap_top and i["total"] >= floor_top:
            i["level"] = "top"
            n_top += 1
        elif n_top + n_watch < cap_top + cap_watch and i["total"] >= floor_watch:
            i["level"] = "watch"
            n_watch += 1
        else:
            i["level"] = "normal"
    for i in items:
        i.pop("_kw", None)
    return n_top, n_watch


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

    all_items, degraded, reports, lib_items = [], [], [], []

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
        if src.get("library_only"):
            # 只进案例库、不进每日简报：这类源是无日期的一次性沉淀（如 awesome-llm-apps
            # 的 100+ 模板），塞进日常流会天天重复占版面。
            lib_items.extend(kept)
        else:
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

    # 语言标记：标题含 CJK 的按「中文条目」处理（标题原样 + 摘要 + 打分），
    # 纯英文的走 translate（译标题 + 摘要 + 打分）。两种指令绝不能混在同一批，
    # 否则模型会整批统一处理、造成英文标题漏译（news-feed 的实测教训）。
    for i in all_items + lib_items:
        i["_translate_title"] = not re.search(r"[\u4e00-\u9fff]", i["title"])
        # 应用案例车道额外要一个「应用形态」标签（案例库要靠它分类检索）
        i["_want_type"] = (i["lane"] == "apps")

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

    # ④b 案例库条目并入（library_only 源）—— 与既有库比对，命中的直接复用中文标题
    #     与摘要并跳过 LLM，否则每天重译上百条会白烧免费额度。
    cases_cfg = cfg.get("cases", {}) or {}
    lib_path = os.path.join(a.outdir, cases_cfg.get("file", "data/cases.json"))
    lib_cache = {}
    if cases_cfg.get("enabled", True) and os.path.exists(lib_path):
        try:
            with open(lib_path, encoding="utf-8") as f:
                for c in json.load(f).get("cases", []):
                    if c.get("url"):
                        lib_cache[c["url"]] = c
        except Exception as e:
            log(f"  ⚠️ 读案例库失败（将重建）：{e}")
    lib_new, seen_lib, n_cached = [], set(), 0
    for i in lib_items:
        u = i.get("url") or ""
        if not u or u in seen_lib:
            continue
        seen_lib.add(u)
        i["_lib"] = True
        prev = lib_cache.get(u)
        if prev:
            i["_skip_llm"] = True
            n_cached += 1
            if prev.get("title"):
                i["titleCn"] = prev["title"]
            if prev.get("summary"):
                i["summary"] = prev["summary"]
            if prev.get("type"):
                i["apptype"] = prev["type"]
        lib_new.append(i)
    if lib_new:
        log(f"📚 案例库：候选 {len(lib_new)} 条（{len(lib_new) - n_cached} 条新增待 AI 处理、"
            f"{n_cached} 条命中缓存复用）")
    merged_all = merged + lib_new

    # ⑤ 跨日折叠：与最近一份归档比对，重复的标记而非丢弃
    #    比对对象必须是「日期不是今天」的 daily 归档 —— 不能拿 latest.json 充当，
    #    它会被同日多次运行覆盖，导致同一天的内容互相标记为重复（实测踩过：首次运行
    #    误标 59/87 条重复，全是本地测试产物造成的假信号）。
    today = now.strftime("%Y-%m-%d")
    prev_titles, prev_name = set(), None
    cands = sorted(glob.glob(os.path.join(a.outdir, "daily", "*.json")), reverse=True)
    prev_path = next((f for f in cands if not os.path.basename(f).startswith(today)), None)
    if prev_path:
        prev_name = os.path.basename(prev_path)
        try:
            with open(prev_path, encoding="utf-8") as f:
                prev_titles = {norm_title(x["title"]) for x in json.load(f).get("items", [])}
        except Exception as e:
            log(f"  ⚠️ 读上一份归档失败（{prev_name}）：{e}")
    for i in merged:
        i["isRepeat"] = norm_title(i["title"]) in prev_titles
    n_rep = sum(1 for i in merged if i["isRepeat"])
    if prev_name:
        log(f"♻️ 跨日比对 {prev_name}：重复 {n_rep} 条（保留但降权展示）")
    else:
        log("♻️ 无历史归档可比（首次运行），跳过跨日折叠")

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

    # ⑦ LLM 增强（P2）：中文摘要 / 英文标题中文化 / 三维打分 / 主题标签 / 应用形态
    tcfg = cfg.get("translate", {})
    translator, failed_batches = llm_enhance(merged_all, tcfg)

    # ⑦b 简报只收非 library_only 的条目；案例库条目（含缓存复用）留待第 ⑩ 步入库
    merged = [i for i in merged_all if not i.get("_lib")]
    if len(merged) != len(merged_all):
        log(f"📚 简报 {len(merged)} 条 ｜ 案例库条目 {len(merged_all) - len(merged)} 条另行入库")

    # ⑧ 分级与排序
    for i in merged:
        if not i.get("summary"):
            i["summary"] = i["desc"][:200]        # LLM 失败时降级用正文开头，绝不空窗
        if not i.get("titleCn") and not i["_translate_title"]:
            i["titleCn"] = ""
    assign_levels(merged, tcfg)
    lvl_rank = {"top": 0, "watch": 1, "normal": 2}
    # 全局按「分级 → 总分 → 新鲜度」排。原来以车道为主键，结果是首屏「重磅」被
    # 单条车道的条目按车道聚成一堆（实测：前 7 条全是模型发布）。首屏最上面
    # 应该是当下最该看的那几条，与来自哪条车道无关；车道筛选交给前端 chip。
    merged.sort(key=lambda x: (lvl_rank.get(x["level"], 9), -x.get("total", 0),
                               x.get("ageH") if (x.get("ageH") or -1) >= 0 else 9999))
    n_lvl = {k: sum(1 for i in merged if i["level"] == k) for k in ("top", "watch", "normal")}

    # ⑨ 产物（ageH 保留给前端做"x 小时前"显示；-1 表示源未提供时间）
    for i in merged:
        i["id"] = item_id(i["title"])
        i["pubTime"] = bj_pub(i["dt"])
        i["pubTs"] = i["dt"].astimezone(TZ_CN).isoformat() if i["dt"] else ""
        i["desc"] = i["desc"][:2000]
        i["summary"] = (i.get("summary") or "")[:300]
        i.pop("dt", None)
        for _k in ("_translate_title", "_want_type", "_lib", "_skip_llm"):
            i.pop(_k, None)                 # 内部标记一律不外泄（漏一个就会写进产物）

    doc = {
        "version": 2,
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "date": now.strftime("%Y-%m-%d"),
        "translator": translator,
        "lanes": [{"id": l["id"], "name": l["name"],
                   "count": sum(1 for i in merged if i["lane"] == l["id"])} for l in cfg["lanes"]],
        "counts": {"total": len(merged), "repeat": n_rep, "byLevel": n_lvl},
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

    # ⑩ 案例库累积：apps 车道的当日条目 + library_only 源的条目，按 url 去重后落盘
    #     与简报的分工：简报回答「今天有什么新东西」（会滚走），
    #     案例库回答「这段时间攒下了哪些作品」（可反复翻、可按形态筛选）。
    if cases_cfg.get("enabled", True):
        by_url = dict(lib_cache)
        fresh = [i for i in merged_all if i.get("_lib") or i["lane"] == "apps"]
        added = 0
        for i in fresh:
            u = i.get("url") or ""
            if not u:
                continue
            rec = {
                "url": u,
                "title": (i.get("titleCn") or i["title"])[:120],
                "titleEn": i["title"][:150] if i.get("titleCn") else "",
                "summary": re.sub(r"\s+", " ", i.get("summary") or i.get("desc") or "")[:300],
                "type": (i.get("apptype") or "")[:16],
                "topic": (i.get("topic") or "")[:16],
                "block": i.get("block", ""),
                "lastSeen": doc["date"],
            }
            prev = by_url.get(u)
            if prev:
                prev.update({k: v for k, v in rec.items() if v})
            else:
                rec["firstSeen"] = doc["date"]
                by_url[u] = rec
                added += 1
        lib = sorted(by_url.values(),
                     key=lambda c: (c.get("lastSeen") or "", c.get("title") or ""), reverse=True)
        maxn = int(cases_cfg.get("max", 400))
        trimmed = max(0, len(lib) - maxn)
        lib = lib[:maxn]
        os.makedirs(os.path.dirname(lib_path), exist_ok=True)
        with open(lib_path, "w", encoding="utf-8") as f:
            json.dump({"updated": doc["generated_at"], "count": len(lib),
                       "types": sorted({c["type"] for c in lib if c.get("type")}),
                       "cases": lib}, f, ensure_ascii=False, indent=1)
        log(f"📚 案例库：共 {len(lib)} 条（新增 {added}"
            + (f"，裁剪 {trimmed}" if trimmed else "") + "）→ data/cases.json")

    log("-" * 100)
    log(f"✅ 产出 {len(merged)} 条 → latest.json + daily/{doc['date']}.json + 运行报告")
    log(f"   分级：重磅 {n_lvl['top']} · 关注 {n_lvl['watch']} · 常规 {n_lvl['normal']}"
        f" ｜ 跨日重复 {n_rep} ｜ AI 增强：{translator}")
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
