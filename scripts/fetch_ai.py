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
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
TZ_CN = datetime.timezone(datetime.timedelta(hours=8))
UTC = datetime.timezone.utc

# 各车道的默认时间窗（小时）。官方源发布频率天然低（实测 OpenAI Research 12 天
# 一条），若统一用短窗会被整体过滤干净 —— 必须按车道区分。中文媒体实测日更节奏
# 差异大（36氪 30 条 / 雷锋网 1 条），36h 会把慢的那几家压缩到只能靠保底，故放宽到 72h。
LANE_MAX_AGE = {"model": 504, "official": 168, "paper": 72, "community": 48,
                "media": 96, "cn": 72,
                # hot = AIHOT 精选。它的 API 侧已经用 window=24h 筛过一遍
                # （实测 21 条的 publishedAt 距今 1.4–23.1h），这里给 72h 只是
                # **兜底**，不是二次过滤 —— 设成 24h 会把边界上的条目误杀成 stale。
                "hot": 72}
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


def _is_nav_fragment(seg):
    """判断一段文字是不是「目录/导航块」。

    判据（实测数据总结）：导航块的特征是**词组高度重复**。
    真实案例（Navier–Stokes 那篇的开头）：
      "Share The problem The problem The result How we found the proof Concurrent work
       Progress and responsibility The problem The result How we found the proof …"
    实测 n-gram 统计："The problem" 出现 3 次，4-gram "The problem The result" 出现 2 次
    —— 正常正文绝不会这样回环重复。

    为什么不能靠关键词判：正文里恰好出现一个 "Share" 或 "Loading" 完全正常
    （实测就有 "The page shows a Loading state"），按词删会误伤正文。
    所以只看**重复结构**，一个关键词都不匹配。
    """
    if not seg or len(seg) > 600:
        return False
    words = seg.split()
    if len(words) < 12:
        return False
    # 找 3~5 词的重复词组（导航的典型形态是「小标题循环」）
    for n in (3, 4, 5):
        seen = {}
        for i in range(len(words) - n + 1):
            g = tuple(words[i:i + n])
            seen[g] = seen.get(g, 0) + 1
        if any(v >= 2 for v in seen.values()):
            return True
    return False


def _cut_nav_head(s):
    """从 s 开头切掉「Share + 小标题回环」式的目录块，返回正文。

    实测形态（OpenAI/Anthropic 的发布页被抓下来时最常见）：
      "Share The problem The problem The result How we found the proof Concurrent work
       Progress and responsibility The problem The result How we found the proof
       Progress and responsibility We're sharing a solution to the Navier–Stokes ..."
                                                            ↑ 正文从这里开始

    特征很规整：`Share` 之后是**同一串小标题重复两遍**（第一遍短、第二遍全），
    重复一结束、出现新句子就是正文。

    关键实现约束（前两版都栽在这里）：
      ① **只在开头 80 词内找重复** —— 全篇扫描会把正文里的正常重复也算进来。
         实测反例：GPT-6 那篇正文里有 "7-Speed Bicycle" 重复 3 次，全篇扫描会把
         切点算到第 192 词（正文深处），一刀砍掉半篇正文。
      ② 取**第一次**重复结束的位置，不是最后一次。
      ③ `Share` 后直接就是正文时（实测第 4 条），一个重复词组都没有 ——
         此时必须原样返回，不能乱切。
    """
    words = s.split()
    if len(words) < 20:
        return s
    WIN = min(len(words), 80)                 # 只看开头窗口
    best = 0
    for n in (3, 4, 5):
        seen = {}
        for i in range(WIN - n + 1):
            g = tuple(words[i:i + n])
            if g in seen and seen[g] >= 1:
                best = max(best, i + n)       # 第一次重复的结束位置
            seen[g] = seen.get(g, 0) + 1
    if best < 8:                              # 没找到像样的回环 → 没有目录块
        return s
    if best >= len(words) - 8:                # 切完剩不下东西 → 不动
        return s
    # 最后一道保护：确认砍掉的确实是「目录小标题」。目录块的形态是**短片段堆叠**
    # —— 几乎没有标点、由若干 2~6 词的小标题连排而成。若切点前那段出现完整句子
    # （逗号/句号成句），说明命中的是正文内部的正常重复，此时不动更安全。
    head_txt = " ".join(words[:best])
    # 目录块没有句末标点；正文段落必然有
    if re.search(r'[.。!！?？]', head_txt):
        return s
    # 目录块平均片段极短：按每 4 词一段粗估，真正的正文段落不会这么碎
    if len(head_txt) / max(1, best) > 12:     # 平均词长 > 12 字符 = 像正文句子
        return s
    return " ".join(words[best:]).lstrip()


def _clean_web_junk(s):
    """清掉正文里的「网页残留」。

    实测来源：Jina Reader 抓回的 markdown 在正文前后夹着页面 UI 碎片，
    它们不含 HTML 标签、strip_tags() 拦不住，会一路流进 desc：
      · `:last-child]:mb-0"> `        —— 被截断的 Tailwind 类名
      · `Loading…`                     —— SPA 占位符
      · 目录/导航块（短语重复）          —— "Share The problem The problem The result …"
      · `\u2060(opens in a new window)` —— 行内嵌的无障碍链接提示（见下）
    后果不只是难看：这些垃圾会被送进 LLM 当正文，摘要和翻译都跟着走偏
    （实测 top 的 7 条译文里有 3 条开头带碎片）。

    ★ 2026-10-08 补第 4 类：行内 UI 注释。
      实测 4 条 openai.com 的 desc 里嵌着 `Problems \u2060(opens in a new window)`：
      一个 U+2060 WORD JOINER（不可见）拼上无障碍提示文字。前三条规则抓不到它 ——
      既不只出现在开头、也不是重复片段、更不含 CSS 特征。
      更麻烦的是它会**被模型忠实翻译**成「（在新窗口中打开）」混进中文译文，
      等于网页 UI 文案被翻译成了中文出现在正文里。
      这类是纯粹的 UI 附属文字，**行内删除不影响任何语义**，所以就地摘掉。

    四条硬约束（避免误伤正文 —— 误删正文比漏清垃圾严重得多）：
      ① 只动**开头**/**结尾**/**明确的行内 UI 注释**，正文中段文字一律不碰；
      ② 只有**明确像代码/占位符/重复导航/UI 注释**的才删，不做通用清洗；
      ③ 删除量超过原文 30% 就整体放弃 —— 宁可留垃圾，不可砍正文；
      ④ 规则 4 的删除量不计入安全阀统计（它删的是不可见字符与固定短语，
         长度极小但可能出现在长文中段，按原比例会被误判）。
    """
    if not s:
        return s
    orig = s
    # ── 0) 行内 UI 注释（先做，因为它会污染后续所有匹配）──
    #    0a) U+2060(WORD JOINER) / U+FEFF(BOM) / U+200B(ZWSP)：纯不可见，无任何语义。
    #        只删这三个 —— 不断然扩到全部 Cf 类，避免误伤（如 U+200D 在 emoji 里是有意义的）。
    s = re.sub(r'[\u2060\ufeff\u200b]', '', s)
    #    0b) 英文态无障碍提示：`(opens in a new window)` / `(opens in new tab)`
    #        允许前面有空格，括号可有可无（实测两种都出现过）。
    s = re.sub(r'\s*\(?\s*opens?\s+in\s+(?:a\s+)?new\s+(?:window|tab)\s*\)?',
               '', s, flags=re.I)
    #    0c) 中文态兜底：模型可能已把它翻成中文存进 descZh，或某些源本身给中文提示
    s = re.sub(r'\s*[（(]\s*在新窗口(?:中)?打开\s*[）)]', '', s)
    s = re.sub(r'\s*[（(]\s*在新标签页?中?打开\s*[）)]', '', s)
    orig_core, s_core = orig, s      # 供安全阀只统计"结构清洗"的删除量
    # 循环收敛：清掉一层碎片后可能露出下一层（实测 "Loading… Share <目录>" 要清两轮），
    # 直到不再变化为止。上限 4 轮，防病态输入空转。
    for _ in range(4):
        before = s
        # ── 1) 开头的 Tailwind/CSS 残片（`:last-child]:mb-0"> ` 这类）──
        s = re.sub(r'^[\s:;,.\[\]\w-]{0,60}(?:last-child|first-child|mb-\d|mt-\d|px-\d|py-\d|flex|grid|text-)[\s\S]{0,50}?">\s*', '', s)
        # ── 2) 开头的 Loading 占位（可能连来两次）──
        s = re.sub(r'^(?:Loading…?|Loading\.\.\.|加载中…?)\s*', '', s)
        # ── 3) 开头的目录块：Share/Contents 起头 → 按「小标题回环」定位正文起点 ──
        if re.match(r'^(?:Share|Contents|Table of contents|On this page)\b', s):
            s = _cut_nav_head(s)
        # ── 3b) 孤立社交按钮残留：`Share` 后面紧跟完整句子时，_cut_nav_head 会
        #        正确地选择不动（见其保护逻辑），但那个 "Share" 本身是按钮文字，
        #        不是正文 —— 单独摘掉它。只在后面确实接正文时才动，避免误删。 ──
        s = re.sub(r'^Share\s+(?=[A-Z\u4e00-\u9fff])', '', s)
        # ── 4) 首尾的孤立标点/管道符 ──
        s = re.sub(r'^[\s|·—–>]+', '', s)
        s = re.sub(r'[\s|·—–]+$', '', s)
        if s == before:
            break
    # ── 5) 结尾的导航尾巴 ──
    tail = re.search(r'\n\s*(?:Share|Related|Read more|Explore more|Next article)\b[\s\S]{0,200}$', s)
    if tail and _is_nav_fragment(tail.group(0)):
        s = s[:tail.start()].rstrip()
    # ── 安全阀：砍太多就整体放弃（宁可留垃圾，不可砍正文）──
    #    只统计**结构清洗**（规则1~5）的删除量，不含规则 0 的行内 UI 注释：
    #    后者是固定短语，在长文里可能命中多次，按原比例算会把好正文误判成"砍太狠"。
    if s_core and len(s) < len(s_core) * 0.7:
        # 结构清洗触发安全阀 → 退回"只做了行内注释处理"的版本
        return _strip_inline_ui(orig_core)
    return s.strip()


def _strip_inline_ui(s):
    """只做行内 UI 注释清理，不动结构 —— 安全阀回退路径。"""
    s = re.sub(r'[\u2060\ufeff\u200b]', '', s or '')
    s = re.sub(r'\s*\(?\s*opens?\s+in\s+(?:a\s+)?new\s+(?:window|tab)\s*\)?', '', s, flags=re.I)
    s = re.sub(r'\s*[（(]\s*在新窗口(?:中)?打开\s*[）)]', '', s)
    s = re.sub(r'\s*[（(]\s*在新标签页?中?打开\s*[）)]', '', s)
    return s.strip()


def strip_tags(s):
    s = re.sub(r"<script.*?</script>", " ", s or "", flags=re.S | re.I)
    s = re.sub(r"<style.*?</style>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</p>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\u00a0]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return _clean_web_junk(s.strip())


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


PARSERS = {
    "leaderboard": parse_leaderboard,
    "openrouter": parse_openrouter,
    "hf-models": parse_hf_models,
    "hf-spaces": parse_hf_spaces,
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
# ── 正文翻译（只跑 top）───────────────────────────────────────────────────
# 背景：AIHOT 每条都有「AI 导读 + 正文 · AI 翻译」并可双语切换，我们只有 80 字摘要。
# 全量翻译不划算（140 条 × 上千字），但**该看的其实只有重磅那十来条**。
# 所以只对 level=top 的英文条目翻正文，产出 descZh；失败就留空、前端自动回退原文。
_ZH_RE = re.compile(r"[\u4e00-\u9fff]")


def _body_sys_prompt():
    return ("你是 AI 技术情报译者。输入是 JSON 数组 [{\"i\":序号,\"text\":英文正文}]。"
            "**逐段翻译成简体中文**，输出 JSON 数组 [{\"i\":序号,\"zh\":中文正文}]，规则："
            "① 完整翻译，不摘要、不改写、不增删信息；原文有几段就译几段，段间用 \\n 分隔。"
            "② 专有名词保留通用写法：GPT-6 / Claude / LangChain / Transformer / RAG 等**不要音译**；"
            "公司名、产品名、人名保留英文原文。"
            "③ 数字、百分比、版本号、日期、金额、URL **一字不改**照抄。"
            "④ 代码片段、命令行、文件路径保持原样，不翻译。"
            "⑤ 译文不写\"本文介绍了\"这类引子，直接就是正文。"
            "只输出 JSON 数组本身，不要解释、不要 markdown 代码块。")


def _call_body_batch(batch, tcfg):
    """翻一批正文。返回 (模型名, 是否成功, 实际请求数)。

    ★ 2026-10-08：改用**独立模型链**（body.models），与摘要/打分链物理分离。
      分工依据（学自 portfolio 的「导语由下一个模型生成」）：
        · 摘要链首模型 agnes-3.0-flash 是推理模型，长输入易 90s 超时；
          正文比摘要长一个量级（实测 5 条 × 1200 字），把它摘出去两头受益；
        · 两条链走不同的 Key 配额池（agnes 不限量 / gemini 约 20 RPD），
          一条限流不拖累另一条；
        · 首选 gemini-3.5-flash-lite（最轻），gemini-3-flash、agnes-2.5-flash 顺延备用。
      回退：body.models 未配置时仍用全局链，保证老配置不会因此失效。
    """
    bcfg = tcfg.get("body") or {}
    chain = bcfg.get("models") or tcfg.get("models", [])
    sys_prompt = _body_sys_prompt()
    tried = 0
    for m in chain:
        if m["name"] in _DEAD_MODELS:
            continue
        key = os.environ.get(m.get("key_env", ""))
        if not key:
            continue
        tried += 1
        payload = {
            "model": m["model"],
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": json.dumps(
                    [{"i": n, "text": i["desc"]} for n, i in enumerate(batch)],
                    ensure_ascii=False)},
            ],
            "temperature": 0.15,
            # 5 条 × 1200 字原文 → 中文约等量字符，加上思考预算，给足
            "max_tokens": 16000,
        }
        try:
            req = urllib.request.Request(
                m["base"].rstrip("/") + "/chat/completions",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=240) as r:
                data = json.loads(r.read().decode())
            msg = data["choices"][0]["message"]
            text = (msg.get("content") or msg.get("reasoning_content") or "").strip()
            text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
            arr = _loads_array(text)
            if len(arr) < 1:
                raise RuntimeError("返回条数为 0")
            if len(arr) < len(batch):
                log(f"  ℹ️ 正文翻译仅返回 {len(arr)}/{len(batch)} 条，其余保留原文")
            n_ok = 0
            for row in arr:
                k = int(row.get("i", -1))
                if not (0 <= k < len(batch)):
                    continue
                zh = str(row.get("zh") or "").strip()
                # 必须真含中文：防模型原样回吐英文（那样前端「中文」钮点了没变化）
                if zh and len(zh) >= 60 and _ZH_RE.search(zh):
                    batch[k]["descZh"] = zh
                    n_ok += 1
            # 一条都没译出来 → 判失败，交给下一个模型；否则整批白跑不被察觉
            if n_ok == 0:
                raise RuntimeError("整批无有效中文译文")
            log(f"  🀄 正文翻译完成（{m['name']}，{n_ok}/{len(batch)} 条）")
            return m["name"], True, tried
        except Exception as e:
            detail = _err_detail(e)
            if _is_quota_err(detail):
                _DEAD_MODELS.add(m["name"])
                log(f"  ⛔ {m['name']} 配额/限流 → 本轮跳过后续正文翻译：{detail[:110]}")
            else:
                log(f"  ⚠️ {m['name']} 正文翻译失败：{detail[:130]}")
    return None, False, tried


def translate_bodies(items, tcfg):
    """只对 top 的英文长文做正文翻译，写回 descZh。

    三条入选条件（缺一不可）：
      · level == "top"             —— 用户明确要求「先只翻重磅」
      · _translate_title            —— 英文源才需要翻译
      · len(desc) >= body_min_chars —— 太短的正文和摘要重复，翻译没有增量价值

    失败语义：写不出 descZh 就不写，前端 hasBody/bodyHTML 自动只显示原文 ——
    与「绝不空窗」一致，翻译是**增益**不是依赖。
    """
    bcfg = tcfg.get("body") or {}
    if not bcfg.get("enabled", False):
        return "off"
    # level 是在 assign_levels() 里算的，本函数必须排在其后调用
    min_chars = int(bcfg.get("body_min_chars", 300))
    cap = int(bcfg.get("body_max_chars", 1200))
    size = int(bcfg.get("batch_size", 5))
    max_items = int(bcfg.get("max_items", 20))

    targets = [i for i in items
               if i.get("level") == "top"
               and i.get("_translate_title")
               and len(i.get("desc") or "") >= min_chars]
    if not targets:
        log("🀄 正文翻译：本轮无符合条件的 top 英文长文")
        return "none"
    if len(targets) > max_items:                 # 兜底上限，防阈值异常时爆量
        targets = targets[:max_items]
        log(f"  ℹ️ 正文翻译目标超上限，截取前 {max_items} 条")

    saved = [(i, i["desc"]) for i in targets]
    for i in targets:
        i["desc"] = i["desc"][:cap]              # 临时截断控 token，翻完还原

    log(f"🀄 正文翻译：{len(targets)} 条 top 英文长文（批 {size}，单条上限 {cap} 字）")
    done, failed = {}, []
    try:
        for k in range(0, len(targets), size):
            part = targets[k:k + size]
            name, ok, tried = _call_body_batch(part, tcfg)
            if ok:
                done[name] = done.get(name, 0) + len(part)
            else:
                failed.append(len(part))
                if tried == 0:
                    log(f"  ⏭️ 正文翻译批（{len(part)} 条）无可用模型：Key 均未配置")
                    break                        # 后面批次同样没 Key，不必再试
    finally:
        for i, full in saved:                    # 无论成败都还原完整正文
            i["desc"] = full

    n_zh = sum(1 for i in targets if i.get("descZh"))
    if not done:
        log(f"  ⚠️ 正文翻译全部失败，{n_zh} 条有译文（前端将回退原文）")
        return "none(原文)"
    out = " + ".join(f"{n}({c}条)" for n, c in done.items())
    if failed:
        out += f" ⚠️降级{[len(f) for f in failed]}"
    log(f"  ✅ 正文翻译结果：{n_zh}/{len(targets)} 条")
    return out


def _sys_prompt(smin, smax, mode, want_type=False):
    extra = ("⑤ type：该案例的**应用形态**，必须从以下固定词表里选一个（不要自创、不要组合）："
             "Web 应用 / 移动 App / 浏览器插件 / 桌面工具 / CLI 工具 / Agent 工作流 / "
             "模型与推理 / 数据分析 / 内容生成 / 效率工具 / 其他。")
    base = (
        f"② summary：{smin}~{smax} 字的中文摘要，讲清核心事实（谁做了什么 + 关键数字或结论）；"
        "信息完整优先于字数，不逐字照抄、不以半句截断。"
        "③ reason：**每条都必须写** 40~60 字的中文推荐理由，回答「这条为什么值得我看」——"
        "**不是复述内容**（那是 summary 的职责），而是点出它的**独特性**："
        "给出了什么别处没有的信息 / 改变了什么既有认知 / 对什么人有直接用处。"
        "写法参照："
        "「原文用三个月随机田野实验区分了 AI 辅助产出与独立能力变化，给出初级与资深律师分化的具体证据。」"
        "「官方公告说明了全球开放后订阅者能获得的实际能力变化，便于对比现有套餐选择。」"
        "**唯一可以不写的情形**：材料残缺到无法判断这条在说什么（如正文与摘要都为空）。"
        "除此之外一律要写 —— 即使内容平庸，也要如实点出它「平庸在哪」，"
        "例如「仅是一份版本更新日志，未含实质功能变化」。"
        "**禁止**写「值得关注」「内容有价值」「信息量丰富」这类不含信息的空话。"
        "④ rel/info/fresh：三个 0-10 的**整数**评分。"
        "**必须严格区分档位，不要普遍给高分** —— 实测若评分集中在 8-9 分，筛选就失去意义。各档定义："
        "rel（与 AI 前沿的相关度）：直接涉及大模型 / Agent / Skill / 论文 / 开源生态的**实质进展** = 8-10；"
        "行业应用、商业案例、人物观点、活动与招聘 = 5-7；与 AI 关联很弱或纯营销 = 0-4。"
        "info（信息密度）：含具体数字、技术细节、可复现结论 = 8-10；有信息但较浅 = 5-7；"
        "纯观点、宣传、入门科普 = 0-4。"
        "fresh（时效性）：首次发布或刚发生的事件 = 8-10；一周内的持续讨论 = 5-7；"
        "回顾、长期有效内容 = 0-4。"
        "⑤ topic：4-8 字的中文主题标签（如 模型发布 / 开源权重 / Agent 框架 / 融资并购 / 政策监管 / 论文方法 / 工程实践）。"
        + (extra if want_type else "")
        + "只输出 JSON 数组本身，不要任何解释、不要 markdown 代码块。")
    tn = ",\"type\":形态" if want_type else ""
    if mode == "translate":
        return ("你是 AI 技术情报编辑。输入是 JSON 数组 [{\"i\":序号,\"title\":英文标题,\"desc\":正文片段}]。"
                "**本批全部条目均为英文。**"
                "输出 JSON 数组 [{\"i\":序号,\"title\":中文标题,\"summary\":中文摘要,"
                "\"reason\":中文推荐理由,\"rel\":整数,\"info\":整数,\"fresh\":整数,\"topic\":中文标签" + tn + "}]，规则："
                "① title：**必须译成简洁中文**（专有名词保留通用写法，如 GPT-6、Claude、LangChain），"
                "不得原样保留英文、不得留英文残句。" + base)
    return ("你是 AI 技术情报编辑。输入是 JSON 数组 [{\"i\":序号,\"title\":中文标题,\"desc\":正文片段}]。"
            "**本批全部条目均为中文。**"
            "输出 JSON 数组 [{\"i\":序号,\"title\":标题,\"summary\":中文摘要,"
            "\"reason\":中文推荐理由,\"rel\":整数,\"info\":整数,\"fresh\":整数,\"topic\":中文标签" + tn + "}]，规则："
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


# ── 模型配额熔断 ──────────────────────────────────────────────────────────
# 实测教训：gemini-3-flash 的免费额度在一天多跑几轮后会被耗尽，此后**每一批都会再去撞
# 一次 429** —— 一次运行 7 批就是 7 次无效请求加 7 行噪声日志，还挤掉了真正有用的信息。
# 配额类错误在当轮内不会自愈，所以一旦命中就在本轮跳过该模型（新进程运行时自动复位）。
_DEAD_MODELS = set()
_QUOTA_HINTS = ("429", "too many requests", "quota", "rate limit", "resource_exhausted",
                "capacity", "overloaded")


def _is_quota_err(msg):
    m = str(msg).lower()
    return any(k in m for k in _QUOTA_HINTS)


def _err_detail(e):
    """把 HTTPError 的**响应体**带出来。

    `str(HTTPError)` 只有 "HTTP Error 429: Too Many Requests"，把 body 丢掉了 ——
    而 body 里才写着配额指标与重试建议。不打出来就只能靠猜（实测正是这样绕了一圈才
    确认是配额而非配置错误）。
    """
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read().decode("utf-8", "replace")[:2000]
        except Exception:
            body = ""
        m = re.search(r'"message"\s*:\s*"([^"]{0,220})"', body)
        extra = m.group(1) if m else (body.strip()[:160] or str(getattr(e, "reason", "")))
        return f"HTTP {e.code}: {extra}"
    return f"{type(e).__name__}: {str(e)[:140]}"


def _call_llm_batch(batch, tcfg, mode, want_type=False):
    """对一批条目依次尝试模型链。返回 (模型名, 是否成功, 实际发起请求的模型数)。"""
    smin = int(tcfg.get("summary_min", 40))
    smax = int(tcfg.get("summary_max", 80))
    sys_prompt = _sys_prompt(smin, smax, mode, want_type)
    tried = 0
    for m in tcfg.get("models", []):
        if m["name"] in _DEAD_MODELS:
            continue                      # 本轮已判定配额耗尽，不重复去撞
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
                    [{"i": n, "title": i["title"], "desc": i["desc"][:1200]}
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
            n_reason = 0
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
                # reason「推荐理由」（40~60 字）：学 AIHOT 的 reason 字段。
                # summary 回答"讲了什么"，reason 回答"为什么值得看" —— 两者不同。
                # ⚠️ 实测教训（2026-10-08，第一次上线就踩到）：prompt 里如果写"可以留空"，
                #    agnes-3.0-flash 会**整批留空** —— 线上 first-run 产出 0/140 条 reason。
                #    所以 ① prompt 已改为"每条都必须写"（把留空收窄成极窄例外）；
                #         ② 这里加整批计数，低于 30% 就在日志里喊出来，不再静默。
                rs = str(row.get("reason") or "").strip()
                if rs:
                    it["reason"] = rs[:150]
                    n_reason += 1
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
                if want_type:
                    # 模型对字段名的服从度不一，多收几个别名；全拿不到还有关键词兜底
                    for _k in ("type", "apptype", "app_type", "kind", "形态", "应用形态"):
                        _ty = str(row.get(_k) or "").strip()
                        if _ty:
                            it["apptype"] = _ty[:14]
                            break
            # 整批译出率过低才判失败（防模型整体原样回吐英文而未被察觉）
            if mode == "translate":
                need = sum(1 for x in batch if x["_translate_title"])
                if need and n_title < max(1, int(need * 0.3)):
                    raise RuntimeError(f"整批仅 {n_title}/{need} 条译出（低于 30%），判定失败")
            log(f"  🌐 AI 增强完成（{m['name']}，{len(batch)} 条，译标题 {n_title} 条，"
                f"推荐理由 {n_reason}/{len(batch)} 条）")
            # reason 产出率过低就当**告警**（不判失败，不影响已得的 summary/打分）。
            # 为什么要有这条：第一次上线时 prompt 写了"可以留空"，模型整批留空、
            # 线上 0/140 条，而日志只显示「AI 增强完成」一片祥和 —— 静默失败最难发现。
            if n_reason < max(1, int(len(batch) * 0.3)):
                log(f"  ⚠️ {m['name']} 推荐理由仅 {n_reason}/{len(batch)} 条（低于 30%）"
                    f"—— 检查 prompt 里 reason 是否被模型整体忽略")
            return m["name"], True, tried
        except Exception as e:
            detail = _err_detail(e)
            if _is_quota_err(detail):
                _DEAD_MODELS.add(m["name"])
                log(f"  ⛔ {m['name']} 配额/限流 → 本轮跳过后续批次：{detail[:110]}")
            else:
                log(f"  ⚠️ {m['name']} 失败：{detail[:130]}")
    return None, False, tried


def llm_enhance(items, tcfg):
    """按 (语言 × 是否应用案例) 分组 → 分批 → 逐批走模型链。

    为什么还要按「是否应用案例」再拆一次：只有 apps 车道需要多输出一个 type
    （应用形态），而输出 schema 一旦混批，模型会对整批都套用同一套字段解释。
    宁可多切几组，也不要把两种 schema 混在一批（与「按语言拆批」同源的理由）。
    """
    if not tcfg.get("enabled", True):
        return "off", []
    _DEAD_MODELS.clear()          # 每轮抓取复位（进程内只跑一轮，这里是双保险）
    size = int(tcfg.get("batch_size", 25))
    groups = []
    for want_cn, mode in ((True, "translate"), (False, "summarize")):
        grp = [i for i in items if bool(i["_translate_title"]) == want_cn]
        for wt in (True, False):
            sub = [i for i in grp if bool(i.get("_want_type")) == wt]
            if sub:
                groups.append((sub, mode, wt))
    n_en = sum(1 for i in items if i["_translate_title"])
    n_app = sum(1 for i in items if i.get("_want_type"))
    log(f"🧠 LLM 增强：待处理 {len(items)} 条（英文 {n_en} / 应用案例 {n_app}），批大小 {size}")

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


# 粗分类（案例库的筛选轴）。两套分类曾经并存 —— 种子源用 README 节名（"进阶 Agent"、
# "RAG 检索增强"…），LLM 返回的是应用形态词表，结果案例库冒出 25 个筛选项，手机上要
# 滚三四行才看完，等于没法筛。改法：apptype 统一走这张粗表（筛选轴上），原节名保留到
# topic 字段（展示用），信息不丢、筛选可用。
AWESOME_SECTION_COARSE = {
    "agent skills": "Agent 技能",
    "starter ai agents": "Agent 工作流",
    "advanced ai agents": "Agent 工作流",
    "always-on agents": "Agent 工作流",
    "multi-agent teams": "Agent 工作流",
    "voice ai agents": "Agent 工作流",
    "autonomous game-playing agents": "Agent 工作流",
    "mcp ai agents": "Agent 工作流",
    "generative ui and agentic frontends": "Web 应用",
    "rag (retrieval augmented generation)": "RAG 检索增强",
    "chat with x": "RAG 检索增强",
    "llm apps with memory": "RAG 检索增强",
    "ai browser tools": "浏览器插件",
    "llm optimization tools": "模型与推理",
    "llm fine-tuning": "模型与推理",
    "ai agent framework crash courses": "教程与学习",
}


# 细分类名 → 粗分类的反查表。用途：库里可能有「URL 已不在源里、但已入库」的历史条目，
# 它们存的是旧口径的细分类名（如「进阶 Agent」）。写库时统一折算，否则筛选轴里会长期
# 混着这些孤儿值，每个都只占 1 条却各占一个筛选项。
COARSE_BY_FINE = {v: AWESOME_SECTION_COARSE.get(k, "效率工具")
                  for k, v in AWESOME_SECTION_CN.items()}


# 应用形态的兜底判定。为什么必须有：LLM 对「额外输出一个字段」的服从度不稳定 ——
# 实测首轮 6 个含 type 指令的批次**全部没返回** type，38 条 apps 条目全是空的。
# 而「按形态筛选」正是案例库的核心用途，不能押在模型身上。词表与给 LLM 的完全一致，
# 保证两条来源的分类能混在一起筛选。顺序即优先级（从具体到宽泛）。
APP_TYPE_RULES = (
    ("浏览器插件", ("浏览器插件", "浏览器扩展", "extension", "userscript", "油猴",
                    "tampermonkey", "chrome 插件", "插件")),
    ("移动 App", ("小程序", "安卓", "android", "ios", "手机 app", "手机端", "移动端",
                  "app store", "鸿蒙")),
    ("CLI 工具", ("cli", "命令行", "终端工具", "shell 脚本", "npm i", "pip install",
                  "安装即用", "一行命令")),
    ("Agent 工作流", ("agent", "智能体", "工作流", "workflow", "mcp", "multi-agent",
                      "自动化流程", "编排")),
    ("桌面工具", ("桌面应用", "desktop", "mac app", "windows app", "客户端", "macos")),
    ("数据分析", ("看板", "dashboard", "可视化", "图表", "数据分析", "报表", "统计",
                  "csv", "excel")),
    ("模型与推理", ("微调", "量化", "推理", "训练", "fine-tun", "gguf", "权重",
                    "benchmark", "评测", "模型权重")),
    ("内容生成", ("写作", "摘要", "翻译", "生成器", "作图", "视频生成", "播客", "漫画",
                  "语音合成", "图像生成", "绘图")),
    ("Web 应用", ("网站", "web 应用", "网页", "在线工具", "saas", "landing page")),
)


def guess_apptype(it):
    """按关键词猜应用形态（LLM 缺失时的兜底）。标题 + 摘要 + 来源 + 链接一起判。"""
    hay = " ".join([
        it.get("title") or "", it.get("titleCn") or "", it.get("summary") or "",
        (it.get("desc") or "")[:400], it.get("block") or "", it.get("url") or "",
    ]).lower()
    for label, kws in APP_TYPE_RULES:
        if any(k in hay for k in kws):
            return label
    return "效率工具"


def _keep_case(c, min_total, min_rel):
    """存量条目的留存判定。

    为什么必须有这一步：入库门槛只挡「新增」是不够的 —— 库里已有的低质条目躺在
    缓存里，不显式剔除就会永久留着（实测首轮不过滤，38 条全进、含 20 条 total<5）。
    老记录若没写 score，无法判断 ⇒ 保留（宁可不误杀）。
    """
    s = c.get("score")
    if not isinstance(s, (int, float)):
        return True
    if s < min_total:
        return False
    r = c.get("rel")
    return not (isinstance(r, (int, float)) and r < min_rel)


# 重磅关键词（加分规则，2026-10-08 改）：「我关心的那类事」优先浮上来，但**必须靠分数**
# 而不是靠特权。
#
# ⚠️ 为什么改（实测根因，别再退回旧写法）：原实现是「命中即判 top，且不受名额 cap_top 约束」。
#    实测 11 天里 **10 天出现档位倒挂** —— top 最低分 6.5/6.7，而 watch 最高分 8.4/8.7，
#    根因就是这两条：
#      · `p-e-w/heretic`（6.7 分）、`LLM-OpenAI-Decisions 0.1a0`（6.5 分）—— 普通 GitHub 仓库，
#        仅因标题含「开源」就被直通进 top，把真正 8.4 分的条目挤到 watch；
#      · `kw_floor` 形同虚设：恰好卡在 6.5 线上，既没过滤掉低质，又比注释主张的 5.0 更严。
#    改成「加成 +1.0 参与正常排序」后，用 11 份历史快照回放：**倒挂 10 天 → 0 天**，
#    且 top 数量恒为 15 条（不减少）—— 关键词的召回作用保住了，特权没有了。
TOP_KW = ("开源", "融资", "收购", "并购", "ipo", "反垄断", "监管",
          "open-source", "open source", "open weights", "open-weight",
          "release", "benchmark", "state-of-the-art", "sota", "breakthrough",
          "acquisition", "acquires", "raises", "funding round", "general availability")

# 关键词加成分值。为什么是 1.0：实测 0.5 时两条边缘条目仍能压过 watch 头部；
# 1.0 既能把这 14 天里真正重要的关键词条目抬进 top，又不足以让 6.5 分的仓促项目越级。
KW_BONUS = 1.0


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
    # 兜底分（LLM 全挂时）。hot=6.4 是**故意压在 watch 门槛 6.5 之下**的：
    # AIHOT 的精选虽然是别人判过的，但兜底路径意味着我们自己的打分不可用，
    # 此时把外部来源的条目送进「重磅」区等于用别人的判断冒充我们的判断。
    base = {"model": 9.0, "official": 7.6, "apps": 6.6, "paper": 6.2, "cn": 6.2,
            "community": 5.4, "media": 5.4, "hot": 6.4}.get(it.get("lane"), 5.4)
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
    # 关键词加成的**参与下限**：低于此分的关键词条目不享受加成（视为不相关）。
    # 定 7.0 的实测依据：watch 门槛是 6.5，加成 1.0 后为 7.5 —— 即"关键词必须先进到
    # 值得关注级，加成才有意义"。旧值 6.5 与 watch 线重合，等于没设。
    kw_floor = float(th.get("kw_floor", 7.0))
    n = len(items)
    cap_top = min(int(q.get("top_max", 15)),
                  max(int(q.get("top_min", 4)), round(n * float(q.get("top_ratio", 0.12)))))
    cap_watch = max(0, round(n * float(q.get("watch_ratio", 0.35))))

    for i in items:
        i["total"] = _total_of(i, tcfg)
        i["_kw"] = _hits_kw(i)
        # 关键词加成：**加在参与排序的分数上，不改原始 total 语义**。
        # 这样分数仍单调（不会出现低分排在高分前面），倒挂从机制上消失。
        if i["_kw"] and i["total"] >= kw_floor:
            i["total"] = round(min(10.0, i["total"] + KW_BONUS), 1)
            i["kwBoost"] = True

    ranked = sorted(items, key=lambda x: -x["total"])
    n_top = n_watch = 0
    # 统一按「分数 + 名额」分配 —— 不再有绕过 cap_top 的直通分支（倒挂根因）
    for i in ranked:
        if n_top < cap_top and i["total"] >= floor_top:
            i["level"] = "top"
            n_top += 1
        elif n_top + n_watch < cap_top + cap_watch and i["total"] >= floor_watch:
            i["level"] = "watch"
            n_watch += 1
        else:
            i["level"] = "normal"
    # 倒挂自检：top 的最低分**不该低于** watch 的最高分。越界说明分级逻辑又被绕过了。
    # 之所以要这条：倒挂持续了 10 天没人发现，因为没有一处会喊出来。
    tops = [x["total"] for x in items if x.get("level") == "top"]
    wats = [x["total"] for x in items if x.get("level") == "watch"]
    inv = (min(tops) < max(wats)) if (tops and wats) else False
    if inv:
        log(f"  ⚠️ 档位倒挂：top 最低 {min(tops):.1f} < watch 最高 {max(wats):.1f}"
            f"（top {len(tops)} 条 / watch {len(wats)} 条）—— 请检查分级逻辑")
    for i in items:
        i.pop("_kw", None)
    return n_top, n_watch, inv


# --------------------------------------------------------------------------- #
# AIHOT 精选接入（影子模式）
#   为什么只接「精选」而不接全量（2026-10-08 实测）：
#     mode=all 24h 有 366 条（x.com 占 68%），mode=selected 24h 只有 17–21 条。
#     接全量等于把"哪些值得看"这件事重做一遍，而 AIHOT 的编辑判断已经做完了 ——
#     项目所有者的指示是「站在巨人肩膀上，不要重复做工」。
#   边界（重要）：**表达层用它的，排序层用我们的。**
#     标题/摘要/推荐理由直接取它的中文成果；但 rel/info/fresh 打分仍走我们自己的
#     LLM。因为加权总分是整页唯一的排序主键，两套分数不可通约（它 71 分 ≠ 我们 7.1
#     分），混用会让「分数下限 + 名额」的分级失控。
#   合规：其条款允许个人非商业使用，但要求保留 attribution 与回链。
#     影子阶段数据不落线上产物、不构成再分发；正式并 UI 需要单独一次确认。
# --------------------------------------------------------------------------- #
_AIHOT_UA = "ai-radar/1.0 (+https://github.com/homjanon/ai-radar)"


def _aihot_get(url, etag="", timeout=30):
    """带 If-None-Match 的 GET。返回 (status, body_bytes, etag)。

    为什么不走 http_get()：它只返回 body，拿不到响应头，而 ETag 就在校验头里。
    304 时按 HTTP 语义没有响应体，urllib 会直接返回 304 而不抛异常。
    """
    req = urllib.request.Request(url, headers={"User-Agent": _AIHOT_UA,
                                               "Accept": "application/json",
                                               "If-None-Match": etag})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), (r.headers.get("ETag") or "")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, b"", etag
        raise


def _aihot_block(name):
    """source.name → 卡片上那个短的 block 标签。

    实测形态有「X：Arena (@arena)」「The Decoder：AI News（RSS）」「公众号：数字生命卡兹克」。
    尾部的（RSS）/（网页）是他们标注抓取通路用的，对读者是噪音，去掉；其余原样保留 ——
    前缀（X / 公众号）本身就是有价值的信息，它告诉读者这条来自大V还是来自号。
    """
    s = re.sub(r"（(?:RSS|网页|Atom|feed)[^）]*）", "", name or "", flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:34]


def _wordset(s):
    """词集：英文按空格分词，中文按 2-gram。用于跨语言无关的同源判定。"""
    s = re.sub(r"[^\w一-鿿\s]", " ", (s or "").lower())
    out = set()
    for t in s.split():
        if re.fullmatch(r"[\u4e00-\u9fff]+", t):
            out |= {t[k:k + 2] for k in range(max(1, len(t) - 1))}
        else:
            out.add(t)
    return out


def _host(u):
    try:
        h = urllib.parse.urlparse(u or "").netloc.lower()
    except Exception:
        return ""
    return h[4:] if h.startswith("www.") else h


def fetch_aihot(acfg, outdir):
    """拉 AIHOT 精选。返回 (raw_items, diag, err)。

    ETag 能跨运行留住，是因为 fetch.yml 是 `git add docs` —— docs/data/ 下的
    小 json 会随产物一起提交，下次 checkout 就带回来了。
    """
    api = acfg.get("api", "https://aihot.news/api/v1/items")
    params = dict(acfg.get("params", {}))
    max_pages = int(acfg.get("max_pages", 3))
    etag_file = os.path.join(outdir, acfg.get("etag_file", "data/aihot_etag.json"))
    etag = ""
    try:
        if os.path.exists(etag_file):
            etag = json.load(open(etag_file, encoding="utf-8")).get("etag", "")
    except Exception:
        pass

    qs = urllib.parse.urlencode(params)
    raw, diag, pages = [], [], 0
    cur = None
    while pages < max_pages:
        url = f"{api}?{qs}" + (f"&cursor={urllib.parse.quote(cur, safe='')}" if cur else "")
        # ⚠️ 整个网络调用必须包在 try 里。冒烟测试抓到过这个 bug：
        #    原来只判 `if aerr` 返回值，而 _aihot_get 抛的是 URLError / 超时 /
        #    ConnectionReset —— 异常直接穿到 main() 外面，**整轮抓取崩掉、当天没产物**。
        #    "外部接口挂了不影响主流程"是注释里的承诺，不写 try 就是空话。
        try:
            st, body, new_etag = _aihot_get(url, etag if pages == 0 else "")
        except Exception as e:
            detail = getattr(e, "reason", None) or getattr(e, "strerror", None) or str(e)
            return None, diag + [f"{type(e).__name__}:{str(detail)[:60]}"], \
                f"网络异常 {type(e).__name__}"
        diag.append(f"HTTP{st}")
        if st == 304:
            log("  ♻️ AIHOT 返回 304（ETag 命中，0 流量）—— 与上次抓取内容相同")
            try:
                cached = json.load(open(etag_file, encoding="utf-8")).get("last_items") or []
            except Exception:
                cached = []
            if cached:
                diag.append(f"304→用缓存{len(cached)}条")
                return cached, diag, None
            # 缓存里没有条目（老版本只存了 etag）：清掉 etag 强制重拉一次，
            # 否则会静默地"今天没有 AIHOT 数据"而日志上完全看不出来
            log("  ⚠️ 304 但本地无缓存条目，清掉 ETag 重拉")
            try:
                os.remove(etag_file)
            except OSError:
                pass
            try:
                st, body, new_etag = _aihot_get(url, "")
            except Exception as e:
                return None, diag + [f"重拉{type(e).__name__}"], "重拉失败"
            diag.append("重拉HTTP%d" % st)
        if st != 200:
            return None, diag, f"HTTP {st}"
        try:
            j = json.loads(body.decode("utf-8"))
        except Exception as e:
            return None, diag, f"JSON 解析失败 {type(e).__name__}"
        its = j.get("items") or []
        raw += its
        pages += 1
        cur = (j.get("page") or {}).get("nextCursor")
        if not cur:
            break
    try:
        os.makedirs(os.path.dirname(etag_file), exist_ok=True)
        # last_items 只存回读 304 时需要的最小字段集，别把整包塞进去撑大仓库
        slim = [{"id": i.get("id"), "title": i.get("title"),
                 "originalTitle": i.get("originalTitle"), "summary": i.get("summary"),
                 "source": {"name": (i.get("source") or {}).get("name")},
                 "links": i.get("links"), "publishedAt": i.get("publishedAt"),
                 "discoveredAt": i.get("discoveredAt"), "category": i.get("category"),
                 "score": i.get("score"), "selected": i.get("selected"),
                 "reason": i.get("reason"), "attribution": i.get("attribution")}
                for i in raw]
        json.dump({"etag": new_etag, "saved_at": datetime.datetime.now(TZ_CN).isoformat(),
                   "n": len(slim), "last_items": slim},
                  open(etag_file, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception as e:
        diag.append(f"etag落盘失败:{type(e).__name__}")
    return raw, diag, None


def parse_aihot(raw, acfg, now):
    """AIHOT item → 与现有条目同构的 dict。

    ⚠️ 比对键的教训（2026-10-09 影子首跑实测）：第一版把 `title` 存成它的
    `originalTitle`（英文），理由是"两边都是英文，跨日折叠天然判得出"。
    结果漏掉了 AIHOT 精选集内部的重复 —— Arena 那笔 2 亿美元融资三条推文，
    originalTitle 分别是 "We're incredibly proud to continue working with
    @thehousefund!" / "Grateful to have @lightspeedvp with us as we build
    what's next!" / "We're so proud to have worked alongside @felicis…"，
    互相 Jaccard 只有 0.00–0.22；而它的**中文标题**之间是 0.58–0.68。
    原因：X 条目的 originalTitle 是**推文正文**不是标题，真正的"这条讲了什么"
    只存在于 AIHOT 编辑写的中文标题里。
    所以现在的取向是：
      · title    = 它的中文标题（读者看到的、也是同语言比对用的键）
      · titleEn  = originalTitle（英文，留着跟**我们自己的英文标题**比）
      · 比对一律「英文对英文、中文对中文」，跨语言不比 —— 见 _fold_keys / aihot_dedup
    """
    out = []
    for e in raw or []:
        if not isinstance(e, dict) or not e.get("selected"):
            continue                      # 精选接口理论上全 true，但按契约要容错
        zh = (e.get("title") or "").strip()
        en = (e.get("originalTitle") or "").strip()
        if not zh:
            continue
        links = e.get("links") or {}
        orig = (links.get("original") or "").strip()
        if not orig:
            continue                      # 没有原文链接就不收 —— 无法溯源的内容不要
        dt = parse_dt(e.get("publishedAt") or e.get("discoveredAt"))
        summ = re.sub(r"\s+", " ", (e.get("summary") or "").strip())
        blk = _aihot_block((e.get("source") or {}).get("name"))
        item = {
            "title": zh[:150],
            "titleCn": "",                # 空着：前端 titleCn||title 会直接用中文标题，
                                          # 且不会渲染出「英文原标题」那行（X 的推文碎片
                                          # 当副标题只会误导）
            "titleEn": en[:150],
            "desc": summ[:1200],
            "url": orig,
            "dt": dt,
            "ageH": -1 if dt is None else int((now - dt).total_seconds() // 3600),
            "lane": acfg.get("lane", "hot"),
            "block": blk,
            "source": blk,
            "aihotUrl": links.get("aihot") or "",
            "aihotScore": e.get("score"),
            "aihotCategory": e.get("category") or "",
            "attribution": e.get("attribution") or {"name": "AIHOT",
                                                    "url": "https://aihot.news/"},
            # 内部暂存：LLM 会覆盖 summary/reason，⑦ 之后按这两个值还原成"它的成果"
            "_aihotSummary": summ,
            "_aihotReason": (e.get("reason") or "").strip(),
            "_aihot": True,
            "alsoIn": [],
        }
        out.append(item)
    return out


def _fold_keys(i):
    """一条条目用来判重的所有标题形态。同语言才互相可比，所以中英文都收进来，
    由比对方各自按语言取用。"""
    ks = set()
    for k in ("title", "titleCn", "titleEn"):
        v = norm_title(i.get(k))
        if v:
            ks.add(v)
    return ks


def _zh_w(s):
    """含 CJK 才给词集，否则返回空集 —— 强制「中文只跟中文比」。"""
    return _wordset(s) if re.search(r"[\u4e00-\u9fff]", s or "") else set()


def _en_w(s):
    """拉丁词集：含 CJK 的串不当英文比（词集切法不同，比出来是噪音）。"""
    if not s or re.search(r"[\u4e00-\u9fff]", s):
        return set()
    return _wordset(s)


def aihot_dedup(items, merged, acfg):
    """三层去重。返回 (kept, dropped_dict)。

    实测才定的层次（第四轮交接里写的「标题 Jaccard>0.8」那层是空转的 ——
    它的中文标题和我们的英文标题词集根本不重叠）：
      ① URL 归一化                 实测命中 4/500
      ② 标题词集 Jaccard≥0.55      **只在同语言之间比**（中文对中文、英文对英文）
      ③ 同域名 + 6h 内 + 标题弱重叠  兜住「同一篇文章、标题写法略有出入」

    ⚠️ ② 的「同语言」这条是硬要求，不是优化。实测两边都见过误判：
      · 拿 AIHOT 中文标题 对 我们的英文标题 → Jaccard 恒≈0，一层形同虚设；
      · 拿 X 条目的英文 originalTitle 互相比 → 那是推文正文，同一事件的三条
        推文 Jaccard 只有 0.00–0.22，重复全漏。
      同语言比对才有效：那三条中文标题互相 0.58–0.68，一发就中。

    ⚠️ ③ 的设计改过一次。最初写成「同域名 + 发布时间差 < 6h 就判重复」，
    那是**错的**：高产源（openai.com、github.com）在 6 小时内发两条完全不同的内容
    很常见，按这个判据会把真新闻当重复丢掉；而不同媒体报道同一事件时域名本来就不同，
    这条又帮不上忙 —— 它既误杀又漏杀。现在加上「标题弱相似」这个前置条件，
    只在同域名（同一篇文章的先验本来就高）且标题确实有相似度时才生效。

    ★ 比对池**边判边吸收**已留下的条目，等于顺带做了「精选集内部去重」。
      2026-10-09 影子首跑撞上：AIHOT 自己的精选集里 Arena 那笔融资重复了三条。
      进来的列表先按它的 score 降序排，保证同组里分数最高的那条留下。
    """
    dmin = float(acfg.get("jaccard_min", 0.55))
    dweak = float(acfg.get("same_host_jaccard", 0.25))
    tmax = float(acfg.get("same_host_hours", 6))
    pool = []          # (url, zh词集, en词集, dt, host)
    for m in merged:
        u = (m.get("url") or "").split("?")[0].rstrip("/")
        pool.append((u, _zh_w(m.get("title")) | _zh_w(m.get("titleCn")),
                     _en_w(m.get("title")) | _en_w(m.get("titleEn")),
                     m.get("dt"), _host(m.get("url") or "")))

    def _jac(a, b):
        return len(a & b) / max(1, len(a | b))

    def _sim(x, y):
        """同语言取高分者。跨语言一律 0，不参与判断。"""
        return max(_jac(x[1], y[1]) if x[1] and y[1] else 0.0,
                   _jac(x[2], y[2]) if x[2] and y[2] else 0.0)

    drop = {"url": 0, "title": 0, "host_time": 0}
    kept = []
    for i in sorted(items, key=lambda x: -(x.get("aihotScore") or 0)):
        u = (i["url"] or "").split("?")[0].rstrip("/")
        if u and any(p[0] and p[0] == u for p in pool):
            drop["url"] += 1
            continue
        me = (u, _zh_w(i["title"]) | _zh_w(i.get("titleCn")),
              _en_w(i.get("titleEn")) or _en_w(i["title"]), i.get("dt"), _host(i["url"]))
        if max((_sim(me, p) for p in pool), default=0) >= dmin:
            drop["title"] += 1
            continue
        if me[3]:
            same = any(p[4] and p[4] == me[4] and p[3]
                       and abs((me[3] - p[3]).total_seconds()) < tmax * 3600
                       and _sim(me, p) >= dweak for p in pool)
            if same:
                drop["host_time"] += 1
                continue
        kept.append(i)
        pool.append(me)
    return kept, drop


def restore_aihot_expr(items):
    """把表达层还原成 AIHOT 的成果（LLM 只留下 score / topic）。

    这是「不重复做工」的落点：它已经写好的中文标题、118 字摘要、编辑推荐理由，
    我们没必要让模型再产一遍。
    """
    for i in items:
        if not i.get("_aihot"):
            continue
        if i.get("_aihotSummary"):
            i["summary"] = i["_aihotSummary"][:300]
        if i.get("_aihotReason"):
            i["reason"] = i["_aihotReason"][:300]
        # 正文与摘要相同时前端 hasBody() 会判 False → 不显示「展开全文」。
        # X/公众号这类条目 API 本来就不给全文，这是**如实降级**，不是漏功能。
        i["desc"] = i.get("_aihotSummary") or i.get("desc") or ""
    return items


def write_aihot_shadow(outdir, day_stat):
    """影子产物：按日期累积成 days 数组，保留最近 14 天，跑 3 天就有决策数据。"""
    p = os.path.join(outdir, "data", "aihot_shadow.json")
    doc = {"updated": "", "days": []}
    if os.path.exists(p):
        try:
            doc = json.load(open(p, encoding="utf-8"))
        except Exception:
            doc = {"updated": "", "days": []}
    if not isinstance(doc.get("days"), list):
        doc["days"] = []
    doc["days"] = [d for d in doc["days"] if d.get("date") != day_stat["date"]] + [day_stat]
    doc["days"] = doc["days"][-14:]
    doc["updated"] = day_stat.get("generated_at", "")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(doc, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return p


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
        if items is None:
            degraded.append({"id": src["id"], "lane": src["lane"], "reason": "; ".join(diag)[:200]})
            log(f"  ⛔ 全部通路失败：{'; '.join(diag)[:160]}")
            reports.append({"id": src["id"], "ok": False, "via": None, "n": 0, "diag": diag})
            continue
        if not items:
            # 通路是通的，只是过完过滤后没有合格条目（如 HF 当天只有个人测试仓）。
            # 这是**正常结果**，不能计入「降级源」——否则告警天天误报、信号就废了。
            log(f"  ○ @{via} 通路可用，但过滤后 0 条（不计入降级）")
            reports.append({"id": src["id"], "ok": True, "via": via, "n": 0, "diag": diag})
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

    # 语言标记：标题含 CJK 的按「中文条目」处理（标题原样 + 摘要 + 打分），
    # 纯英文的走 translate（译标题 + 摘要 + 打分）。两种指令绝不能混在同一批，
    # 否则模型会整批统一处理、造成英文标题漏译（news-feed 的实测教训）。
    for i in all_items:
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

    # ④c AIHOT 精选接入。放在跨源去重**之后**，因为第三层去重要拿 merged 的
    #    域名+时间做比对；放在 LLM 增强之前，这样正式并入时不用改后面的流程。
    acfg = cfg.get("aihot", {}) or {}
    aihot_items, aihot_stat = [], None
    if acfg.get("enabled", False):
        log("[hot      ] aihot-selected        AIHOT 精选")
        raw, adiag, aerr = fetch_aihot(acfg, a.outdir)
        if aerr or raw is None:
            # 外部接口挂了绝不影响主流程：记一条降级，继续往下走（绝不空窗）
            degraded.append({"id": "_aihot", "lane": "hot",
                             "reason": ("AIHOT 接口不可用：" + (aerr or "; ".join(adiag)))[:170]})
            log(f"  ⛔ AIHOT 抓取失败，主流程照常：{(aerr or '; '.join(adiag))[:120]}")
            reports.append({"id": "_aihot", "ok": False, "via": "aihot.news",
                            "n": 0, "diag": adiag})
        else:
            parsed = parse_aihot(raw, acfg, now)
            # 时间窗：AIHOT 条目是在源循环**之后**直接进 merged 的，绕过了 ② 那段
            # 每源窗口过滤。既然 LANE_MAX_AGE 给 hot 配了 72h，就得在这里真的执行它，
            # 否则那条配置是谎话。超窗的不丢弃、只标 stale（与主流程的保底语义一致）。
            win = int(acfg.get("max_age_h") or LANE_MAX_AGE.get(acfg.get("lane", "hot"), 72))
            n_stale = 0
            for i in parsed:
                if i["ageH"] > win:
                    i["stale"] = True
                    n_stale += 1
            for i in parsed:
                i["_translate_title"] = False     # 它的标题已是中文，别再进翻译链
                i["_want_type"] = False
            kept, dropped = aihot_dedup(parsed, merged, acfg)
            shadow = bool(acfg.get("shadow_only", True))
            log(f"  ✅ 拉到 {len(raw)} → 可用 {len(parsed)}（超 {win}h 窗标 stale {n_stale}）→ "
                f"去重剔 {sum(dropped.values())}（URL {dropped['url']} / 标题 {dropped['title']} / "
                f"同域同时段 {dropped['host_time']}）→ 净增 {len(kept)}"
                + ("（影子模式，未并入产物）" if shadow else "（已并入）"))
            reports.append({"id": "_aihot", "ok": True, "via": "aihot.news",
                            "n": len(kept), "raw": len(raw), "diag": adiag})
            if shadow:
                aihot_items = kept
                aihot_stat = {"date": now.strftime("%Y-%m-%d"),
                              "generated_at": now.strftime("%Y-%m-%d %H:%M"),
                              "shadow_only": True, "fetched": len(raw),
                              "usable": len(parsed), "dropped": dropped,
                              "net": len(kept), "wouldBe": {}, "bumped": 0, "items": []}
            else:
                merged.extend(kept)

    # ④b 读案例库既有记录：入库时按 url 比对，用于跨日累积（命中即更新 lastSeen）。
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
                # 收齐每条目的**所有标题形态**（title / titleCn / titleEn）。
                # 原来只收 norm_title(title)，于是 AIHOT 条目（title 是中文、
                # titleEn 才是英文）永远撞不上昨天那条英文标题 —— 同一条新闻
                # 会连着两天各出现一次。
                for x in json.load(f).get("items", []):
                    prev_titles |= _fold_keys(x)
        except Exception as e:
            log(f"  ⚠️ 读上一份归档失败（{prev_name}）：{e}")
    for i in merged:
        i["isRepeat"] = bool(_fold_keys(i) & prev_titles) if prev_titles else False
    n_rep = sum(1 for i in merged if i["isRepeat"])
    if prev_name:
        log(f"♻️ 跨日比对 {prev_name}：重复 {n_rep} 条（保留但降权展示）")
    else:
        log("♻️ 无历史归档可比（首次运行），跳过跨日折叠")

    # ⑥ 正文补抓（仅对 desc 过短且非重复的条目，限量）
    #    ⚠️ 排除 _aihot 条目：它的 desc 就是 118 字的摘要，按长度判会被选中去补抓，
    #    而 links.original 大量是 x.com / 公众号 —— 那些页面要 JS 渲染或有登录墙，
    #    Jina 抓不动，白烧配额还刷一堆失败日志。API 本来就不给全文，这是已知边界。
    if not a.no_jina:
        need = [i for i in merged if len(i["desc"]) < 300 and not i["isRepeat"]
                and i["url"] and not i.get("_aihot")][: a.jina_max]
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
    translator, failed_batches = llm_enhance(merged, tcfg)

    # ⑦a′ AIHOT 条目的表达层还原。**必须无条件做一次，不能只在影子分支里做** ——
    #     正式并入（shadow_only=false）时它们是直接混在 merged 里过 ⑦ 的，
    #     LLM 会把它们已有的中文标题/摘要/推荐理由一并覆盖掉，
    #     那等于把「表达层用它的、排序层用我们的」这条边界悄悄抹成了"两层都用我们的"，
    #     而这条边界正是这次接入的全部理由。
    restore_aihot_expr(merged)

    # ⑦a 应用形态兜底：LLM 没给就按关键词判，保证「按形态筛选」永远可用。
    for i in merged:
        if i.get("lane") == "apps" and not i.get("apptype"):
            i["apptype"] = guess_apptype(i)
    n_typed = sum(1 for i in merged if i.get("lane") == "apps" and i.get("apptype"))
    log(f"🏷️ 应用形态：{n_typed} 条已标注"
        f"（{sum(1 for i in merged if i.get('lane') == 'apps')} 条 apps 条目）")

    # ⑧ 分级与排序
    for i in merged:
        if not i.get("summary"):
            i["summary"] = i["desc"][:200]        # LLM 失败时降级用正文开头，绝不空窗
        if not i.get("titleCn") and not i["_translate_title"]:
            i["titleCn"] = ""
    n_top_chk, n_watch_chk, inverted = assign_levels(merged, tcfg)
    if inverted:
        # 记进产物（degraded 数组，与"降级源"同结构），让倒挂**可见**而不是静默。
        # 实测倒挂持续 10 天没被发现，就是因为没有任何一处会喊出来。
        degraded.append({"id": "_levels", "lane": "-",
                         "reason": "档位倒挂：top 最低分低于 watch 最高分（分级逻辑异常）"})

    # ⑧b AIHOT 影子评估：必须在 assign_levels **之后**，否则拿不到"它会落在哪个档"。
    #     影子模式的价值不在于"能不能拉到数据"，而在于回答那个真正要拍板的问题：
    #     并进来之后，它会不会把我们自己判出来的重磅挤掉？
    #     做法是把 aihot 条目和当日 merged 放进同一个池子重跑一遍分级（用副本，
    #     绝不污染真实产物），看两边的档位各变成什么。
    if aihot_items and aihot_stat is not None:
        _, _af = llm_enhance(aihot_items, tcfg)
        restore_aihot_expr(aihot_items)          # 表达层还原成它的成果，只留我们的分数
        # ⚠️ 必须把"这一轮 LLM 到底有没有跑成"记进影子文件。
        #    本地没配 Secret 时打分全走兜底分（车道基线+新鲜度），算出来的
        #    「重磅 0 / 关注 7」是**兜底分的分布**，不是我们真实判分的结果，
        #    拿它做并入决策会得出错误结论。有这个字段，读的人一眼就知道能不能信。
        aihot_stat["llm"] = bool(aihot_items and
                                 any(i.get("score") for i in aihot_items))
        aihot_stat["llmFailedBatches"] = len(_af or [])
        probe = [dict(x) for x in merged] + [dict(x) for x in aihot_items]
        for x in probe:
            x.pop("level", None)
        assign_levels(probe, tcfg)               # 只改副本的 level/total，真实 merged 不动
        ours, mine = probe[:len(merged)], probe[len(merged):]
        dist = {}
        for m, p in zip(aihot_items, mine):
            dist[p["level"]] = dist.get(p["level"], 0) + 1
            aihot_stat["items"].append({
                "title": (m.get("titleCn") or m.get("title") or "")[:80],
                "block": m.get("block", ""), "url": m.get("url", ""),
                "aihotUrl": m.get("aihotUrl", ""),
                "aihotScore": m.get("aihotScore"), "category": m.get("aihotCategory"),
                "ageH": m.get("ageH"), "ourTotal": p.get("total"),
                "ourLevel": p.get("level"), "ourScore": p.get("score"),
                "kwBoost": bool(p.get("kwBoost")),
                "reason": (m.get("reason") or "")[:180]})
        moved = sum(1 for o, orig in zip(ours, merged) if o["level"] != orig.get("level"))
        lost_top = [(orig.get("titleCn") or orig.get("title") or "")[:40]
                    for o, orig in zip(ours, merged)
                    if orig.get("level") == "top" and o["level"] != "top"]
        aihot_stat["wouldBe"] = dist
        aihot_stat["bumped"] = moved
        aihot_stat["lostTop"] = lost_top
        log(f"🛰 AIHOT 影子：净增 {len(aihot_items)} 条 → 若并入落在 "
            f"重磅 {dist.get('top', 0)} / 关注 {dist.get('watch', 0)} / 常规 {dist.get('normal', 0)}"
            f"；现有条目档位被改动 {moved} 条"
            + (f"，其中跌出重磅：{'、'.join(lost_top[:4])}" if lost_top else ""))
        if not aihot_stat["llm"]:
            log("   ⚠️ 本轮 LLM 打分不可用，上面是**兜底分**的分布，不能作为并入依据"
                "（兜底分只按车道基线+新鲜度算，hot 车道压到 6.4 就是为了让它进不了重磅）")
        if _af:
            log(f"   ⚠️ AIHOT 批次有降级：{_af}")
        sp = write_aihot_shadow(a.outdir, aihot_stat)
        log(f"   → 影子数据已写入 {os.path.relpath(sp, a.outdir)}（保留最近 14 天）")

    # ⑧a 正文翻译（只跑 top，见方案 §5）：必须在 assign_levels 之后 —— 它按 level 选条
    tr_body = translate_bodies(merged, tcfg)

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
        if i.get("descZh"):                     # 译文同样限长，产物别被撑爆
            i["descZh"] = i["descZh"][:2400]
        i.pop("dt", None)
        for _k in ("_translate_title", "_want_type", "_aihot",
                   "_aihotSummary", "_aihotReason"):
            i.pop(_k, None)                 # 内部标记一律不外泄（漏一个就会写进产物）

    doc = {
        "version": 2,
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "date": now.strftime("%Y-%m-%d"),
        "translator": translator,
        "translator_body": tr_body,
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

    # ⑩ 案例库累积：把 apps 车道的当日条目按 url 去重后累积落盘。
    #     与简报的分工：简报回答「今天有什么新东西」（会滚走，要宽不漏），
    #     案例库回答「攒下了哪些作品」（可反复翻，要严不滥）。
    if cases_cfg.get("enabled", True):
        by_url = dict(lib_cache)
        # 入库门槛：**简报要宽（不漏），案例库要严（宁缺毋滥）**。
        #   apps 车道的条目必须过分数门槛。实测不过滤时 38 条里有 20 条 total<5，
        #     把「耳机剁手清单 / 手机评测 / 社媒营销策略」这类与 AI 无关的推荐流内容
        #     也收了进来 —— 少数派是「效率工具+数码生活」版块，本身不是 AI 源。
        min_total = float(cases_cfg.get("min_total", 5.0))
        min_rel = float(cases_cfg.get("min_rel", 3))

        def _lib_ok(it):
            """够格入库吗？分数门槛 + 相关度门槛。"""
            if (it.get("total") or 0) < min_total:
                return False
            sc = it.get("score")
            if not sc:
                # LLM 不可用（降级路径）：没有 rel 可判 —— 只按兜底总分把关。
                # 否则 LLM 一挂，案例库就会静默停止增长（不报错、只是不再有新条目）。
                return True
            return sc.get("rel", 0) >= min_rel

        fresh = [i for i in merged if i["lane"] == "apps" and _lib_ok(i)]
        n_cut = sum(1 for i in merged if i["lane"] == "apps" and not _lib_ok(i))
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
                "type": COARSE_BY_FINE.get(i.get("apptype") or "",
                                           i.get("apptype") or "")[:16],
                "topic": (i.get("topic") or "")[:16],
                "block": i.get("block", ""),
                # 分数一并入库：淘汰要按质量排。老实现只按 lastSeen —— 那是「新的留
                # 老的扔」，与作品集的用途相反（老的优质条目被新的平庸条目挤掉，且
                # 被挤掉后若源里不再出现就永远回不来）。
                "src": "apps",
                "lastSeen": doc["date"],
            }
            if i.get("total") is not None:
                rec["score"] = round(float(i["total"]), 1)
            _rel = (i.get("score") or {}).get("rel")
            if _rel is not None:
                rec["rel"] = int(_rel)
            prev = by_url.get(u)
            if prev:
                prev.update({k: v for k, v in rec.items() if v})
            else:
                rec["firstSeen"] = doc["date"]
                by_url[u] = rec
                added += 1

        # 补分：换口径前入库的老记录**没有 score**，就无从判质量 —— 它们会永久躺在库里。
        # 实测：本轮 38 条里 10 条因「今天又被抓到且不达标」被剔除，但余下 21 条没再被任何
        # 源抓到，永远判不了。每次运行补一小批（上限 rescore_max）、补完立刻按门槛判，
        # 自然收敛：既不用人工做一次性迁移，也不会一次烧掉全部额度。
        rescore_max = int(cases_cfg.get("rescore_max", 30))
        todo = [c for c in by_url.values()
                if not isinstance(c.get("score"), (int, float))][:rescore_max]
        n_scored = 0
        if todo and tcfg.get("enabled", True):
            pseudo = []
            for c in todo:
                ps = {"title": c.get("titleEn") or c.get("title") or "",
                      "desc": (c.get("summary") or "")[:1200],
                      "lane": "apps", "block": c.get("block") or "",
                      "url": c.get("url") or "", "source": c.get("block") or ""}
                ps["_translate_title"] = not re.search(r"[\u4e00-\u9fff]", ps["title"])
                ps["_c"] = c
                pseudo.append(ps)
            log(f"🩹 案例库补分：{len(pseudo)} 条老记录无分数（换口径前入库），走 LLM 补一次")
            _bsz = int(tcfg.get("batch_size", 25))
            for _k in range(0, len(pseudo), _bsz):
                _call_llm_batch(pseudo[_k:_k + _bsz], tcfg, mode="summarize")
            for ps in pseudo:
                c = ps.pop("_c")
                if not ps.get("score"):
                    continue
                c["score"] = _total_of(ps, tcfg)
                c["rel"] = int(ps["score"].get("rel", 0))
                if ps.get("summary") and len(ps["summary"]) > len(c.get("summary") or ""):
                    c["summary"] = ps["summary"][:300]
                n_scored += 1
            log(f"   补分成功 {n_scored}/{len(pseudo)}（随后按同一门槛判去留）")
        # 存量清理：库里**已存在**但不达标的条目必须显式剔掉 —— 它们在 lib_cache 里，
        # 只做「不新增」不删就会永久留存（实测首轮 38 条全进，含 20 条低分）。
        cut = {i["url"] for i in merged
               if i.get("url") and i["lane"] == "apps" and not _lib_ok(i)}
        before = len(by_url)
        by_url = {u: c for u, c in by_url.items()
                  if u not in cut and _keep_case(c, min_total, min_rel)}
        n_purge = before - len(by_url)
        if n_purge:
            log(f"🧹 案例库剔除 {n_purge} 条不达标条目（门槛 total≥{min_total} 且 rel≥{min_rel}）")
        def _show_key(c):
            # 有分的（过了质量门槛的）在前、按分降序，无分的排最后。
            # 按 lastSeen 排是不行的 —— 无分老条目的 lastSeen 往往也很新，会一直霸着首屏，
            # 而案例库的用途是「看别人做出了什么」，不是「翻旧账」。
            s = c.get("score")
            return (1 if isinstance(s, (int, float)) else 0,
                    float(s) if isinstance(s, (int, float)) else 0.0,
                    c.get("lastSeen") or "")
        lib = sorted(by_url.values(), key=_show_key, reverse=True)
        # 整库归一化：换过分类口径后，URL 已失效的老条目不会再被上面的循环碰到，
        # 只靠「命中即更新」会留下永久孤儿（实测：「进阶 Agent」这种旧细分类名长期占着
        # 一个筛选位、底下只有 1 条）。落盘前统一折算。
        for _c in lib:
            _old = _c.get("type") or ""
            _c["type"] = COARSE_BY_FINE.get(_old, _old)
        maxn = int(cases_cfg.get("max", 600))
        trimmed = 0
        if len(lib) > maxn:
            # 淘汰按质量：分数降序，同分老的先走。展示顺序由 _show_key 决定，
            # 淘汰策略不该改变浏览顺序。
            def _rank(c):
                s = c.get("score")
                return float(s) if isinstance(s, (int, float)) else 5.5
            keep = sorted(lib, key=lambda c: (_rank(c), c.get("lastSeen") or ""),
                          reverse=True)[:maxn]
            trimmed = len(lib) - len(keep)
            lib = sorted(keep, key=_show_key, reverse=True)
        os.makedirs(os.path.dirname(lib_path), exist_ok=True)
        with open(lib_path, "w", encoding="utf-8") as f:
            json.dump({"updated": doc["generated_at"], "count": len(lib),
                       "types": sorted({c["type"] for c in lib if c.get("type")}),
                       "cases": lib}, f, ensure_ascii=False, indent=1)
        log(f"📚 案例库：共 {len(lib)} 条（新增 {added} · 当日过滤 {n_cut}"
            + (f" · 补分 {n_scored}" if n_scored else "")
            + (f" · 裁剪 {trimmed}" if trimmed else "") + "）→ data/cases.json")

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
