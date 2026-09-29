# AI 前沿雷达（ai-radar）

持续追踪大模型 / Agent / Skill 生态的自动化聚合站。**当前 P2：抓取 → 硬规则筛选 → LLM 增强（中文摘要 / 三维评分 / 分级）→ JSON → 网页面板，全链路已跑通。**

线上面板：https://homjanon.github.io/ai-radar/

> **首次部署需配两个 Secret**（未配置时自动降级为原文，不影响抓取）：
> ```bash
> gh secret set GEMINI_API_KEY --repo homjanon/ai-radar   # 交互式输入，不落 shell 历史
> gh secret set AGNES_API_KEY  --repo homjanon/ai-radar
> ```

## 这个仓要做什么

把国内外 AI 前沿动态（官方发布、论文、开源社区、中文资讯）自动汇总成一份「该看的少数几条」，
而不是又一堆信息流。设计原则照搬 `homjanon/news-feed` 已验证的三条：

1. **能用硬规则就不用 LLM** —— 选条、去重、时间窗全是确定性逻辑，结果可复现；
2. **每一层都必须能降级** —— 源挂了换实例，模型挂了换下一档，绝不空窗；
3. **配置驱动** —— 加源、换路由只改 `scripts/sources.json`，不改代码。

## 目录

```
scripts/sources.json          源配置（单一数据源）· 五车道 · desc_min 门槛 · LLM 模型链
scripts/probe_sources.py      源可用性探测（只读，P0）
scripts/fetch_ai.py           抓取与产物生成（硬规则选条 + LLM 增强）
docs/index.html               网页面板（读 latest.json 渲染，支持车道筛选与分级折叠）
docs/latest.json              最新一期产物
docs/daily/{date}.json        当日归档（保留 30 天）
docs/data/reports/{ts}.json   运行报告（每源通路/条数/降级原因，可回溯）
.github/workflows/probe-sources.yml   源探测（手动 + 每月自动）
.github/workflows/fetch.yml           抓取发布（每日 08:10 北京）
```

## 三层处理链路

| 层 | 职责 | 谁来做 |
|---|---|---|
| **选条** | 时间窗（含保底）→ 标题去重 → 跨源折叠 → 跨日标记 | **纯硬规则**，确定性、可复现 |
| **分级** | 重磅关键词硬规则 > 加权总分（rel .45 / info .30 / fresh .25） | 硬规则 + LLM 分数；无 LLM 时用「车道基线 + 新鲜度」兜底打分 |
| **表达** | 中文摘要（40–80 字）· 英文标题中文化 · 主题标签 · 应用形态（仅 apps 车道） | **只由 LLM 做**，失败即降级为原文 |

LLM 按**语言 × 是否应用案例**四组拆批（英文 `translate` / 中文 `summarize`，各自再分
「要 type」与「不要 type」）——指令一旦混批，模型会整批统一处理：要么英文标题漏译，
要么给新闻也硬塞一个「应用形态」。模型链逐档降级，全失败也不空窗。

## 七条车道

| 车道 | 覆盖 | 代表源 |
|---|---|---|
| `model` 模型发布 | **新模型上架与匿名公测**（技术领先最直接的信息） | llm-leaderboard（含上架时间）、OpenRouter stealth、HF 新模型 |
| `official` 官方一手 | 模型/产品发布，权重最高 | OpenAI、Anthropic、DeepMind、Google AI、Hugging Face |
| `apps` 应用案例 | **别人用 AI 做出来的东西**（找灵感 + 学实现） | Show HN、V2EX 分享创造、Product Hunt、r/SideProject、HF Spaces、awesome-llm-apps |
| `paper` 论文与基准 | 论文精选与用量趋势 | HF Daily Papers、arXiv cs.CL/cs.AI |
| `community` 社区与开源 | 开发者真实热度 | Hacker News、GitHub Trending、GitHub Releases |
| `media` 媒体与分析 | 深度解读与观点 | Simon Willison、TLDR AI |
| `cn` 中文动态 | 国内产业与工程视角 | 雷锋网 AI、36氪 AI、InfoQ、Solidot |

`model` 车道的存在理由：新模型的信息有明显**提前量**，而官方 blog 只覆盖 T−0 之后。

| 阶段 | 在哪能最早看到 | 对应源 |
|---|---|---|
| T−21d 匿名压力测试 | OpenRouter 的 `stealth/` 模型 | `openrouter-stealth` |
| T−7d 权重上架（无声发布） | Hugging Face（有模型卡、无新闻稿） | `hf-new-models` |
| T−0 正式发布 | 厂商官方 blog / 媒体 | `official` / `media` 车道 |

上架时间字段（`listed_at_iso`）来自第三方仓 `AmigaMeow/llm-leaderboard-data`（MIT）——
OpenRouter 官方 API 没有这个字段，该仓已代抓 LMArena Elo 与上架时间，
省掉了「自己抓 JS 渲染页 + 自己做跨日快照 diff」这一整套。

## 实例池

所有 RSSHub 路由按以下顺序兜底、**命中即止**（沿用 portfolio 的全项目统一实例池）：

```
hub.slarker.me → rsshub.rssforever.com → rsshub.umzzz.com
→ rsshub.isrss.com → rsshub.ktachibana.party → rsshub-balancer.virworks.moe
```

中文车道额外先试 `rss.injahow.cn`（.cn 专属实例）。

## 两条来自实测的硬经验

**一、desc 深浅不一会静默毁掉摘要质量。**
各 RSSHub 实例对同一条目返回的 `desc` 长度差异极大（实测 0 ~ 11148 字：HF blog 全文 11148，
而量子位只有 0、HN 只有 32）。沿用「命中即止」会锁死在**导语版实例**上，
导致 LLM 只能对着摘要的摘要做二次摘要。因此每个源在 `sources.json` 里带 `desc_min` 门槛：
不达标继续试下一实例。这条经验来自 portfolio 的同类踩坑。

**二、本机经代理实测 ≠ Actions 可用性。**
实测 19 个源在本机报 `Tunnel connection failed: 502`（失败耗时固定在 10.0–10.3 秒，
是代理规则拦截特征，非源失效）；其中 `news.google.com` 在 news-feed 生产环境稳定在用。
**凡 502/超时一律标「待复核」，不写「不可用」**，必须在本 workflow 里复核。

## 已知失效（别再试）

| 源 | 实测 | 结论 |
|---|---|---|
| `anthropic.com/news/rss.xml` | 404 | Anthropic **没有官方 RSS**，走 rsshub `/anthropic/news` |
| `blogs.microsoft.com/ai/feed/` | 410 | 已正式下线 |
| `deeplearning.ai/the-batch/feed/` | 404 | 路径已变 |
| `arxiv.org/rss/cs.LG` | 200 但 0 条 | 别配（cs.CL / cs.AI 正常） |
| `jiqizhixin.com/rss` | 返回 HTML | 非 RSS，须走 rsshub（公共实例实测已无此路由） |

## 怎么跑

```bash
# 抓取并生成产物（Actions 每日自动跑；也可手动触发 Actions → fetch-ai → Run workflow）
python scripts/fetch_ai.py --outdir docs
python scripts/fetch_ai.py --only cn              # 只抓中文车道
python scripts/fetch_ai.py --id hf-blog,arxiv-cs-cl   # 只抓指定源（调试）
python scripts/fetch_ai.py --no-jina              # 关闭正文补抓

# 源可用性探测（只读，不写文件）
python scripts/probe_sources.py
python scripts/probe_sources.py --only cn         # 只测中文车道

# 本机调试需先开代理（谷歌系/部分源在国内直连不通）
HTTPS_PROXY=http://127.0.0.1:7890 python scripts/fetch_ai.py --outdir docs
```

探测结果会写进 Actions 的 **Step Summary**（表格形式），并打印完整日志，**不落任何文件**。

## 路线

- **P0 已完成** 源基线探测 —— 建仓 + probe workflow，源实测基线（两环境结论相反，见下）
- **P1 已完成** 抓取 → 时间窗（含保底）→ 去重 → 跨源折叠 → 跨日标记 → JSON → Pages 面板
- **P2 已完成** LLM 增强：中文摘要 + 英文标题中文化 + 三维打分 + 主题标签 + 分级折叠
- **P2.5 已完成** 「模型发布」车道：三种专用解析器 + 匿名公测识别 + 模型链补第 4 档
- **P3 已完成** 「应用案例」车道 + 案例库累积 + 终端风格前端（简报 / 案例库双视图）
- **P4** 趋势量化：周报四指标（厂商发布频率 / 开源权重占比 / 星标增速 / 主题热度）

## 新增「应用案例」车道的踩坑

- **`sort=createdAt` 的 HF 模型列表绝大多数是个人测试仓库**（实测首批 8 条全是
  `downloads=0 / likes=0` 的占位仓）。降噪两道：`exclude_tags` 剔量化/微调衍生品，
  `min_likes=2` 卡门槛。真正的机构发布几小时内就有点赞，不会漏。
- **标题里不能出现「权重」二字** —— 会命中硬规则 `TOP_KW`，把一堆测试仓硬提成「重磅」。
  故 HF 条目标题用「HF 新模型：」。
- **「过滤后为空」≠「源降级」** —— 专用解析器允许返回空列表，否则 HF 某天只有测试仓时
  会被误报为抓取失败，污染降级告警。
- **awesome 类 README 要按分类轮转取样** —— 各节长度悬殊（RAG 21 条 vs 微调 2 条），
  直接取前 N 条会让大节挤掉其它形态，案例库就失衡了。
