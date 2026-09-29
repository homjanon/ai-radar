# AI 前沿雷达（ai-radar）

持续追踪大模型 / Agent / Skill 生态的自动化聚合站。**当前 P3：抓取 → 硬规则筛选 → LLM 增强（中文摘要 / 三维评分 / 分级）→ JSON → 双视图面板（简报 + 应用案例库），全链路已跑通。**

线上面板：**https://ai-radar.hellohopo.dpdns.org/**（老地址 `homjanon.github.io/ai-radar/` 仍可访问、不跳转）

> 已接入 `nav` 首页第 3 位，并被「老张工具箱」App 收纳（`app.phase=4`、WebView 打开）。

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
scripts/sources.json          源配置（单一数据源）· 七车道 · desc_min 门槛 · 案例库门槛 · LLM 模型链
scripts/probe_sources.py      源可用性探测（只读，复用 fetch_ai 的解析器，不再自带一套口径）
scripts/fetch_ai.py           抓取与产物生成（硬规则选条 + LLM 增强 + 案例库累积）
scripts/smoke_test.py         离线冒烟测试（抓取前的守门；不联网、不需要 Secret）
docs/index.html               双视图面板（简报 / 案例库，读 latest.json 与 data/cases.json）
docs/latest.json              最新一期产物
docs/daily/{date}.json        当日归档（保留 30 天）
docs/data/cases.json          应用案例库（跨日累积，按质量保留）
docs/data/reports/{ts}.json   运行报告（每源通路/条数/降级原因，可回溯）
docs/CNAME                    自定义域（ai-radar.hellohopo.dpdns.org）
.github/workflows/probe-sources.yml   源探测（手动 + 每月自动）
.github/workflows/fetch.yml           抓取发布（每日 07:30 北京；GitHub cron 常延迟，故提前一档）
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

链序：`agnes-3.0-flash → gemini-3.5-flash-lite → gemini-3-flash → agnes-2.5-flash` ——
**免费不限量的放首位**，两个 gemini 额度互相独立、居中互为备份（`gemini-3-flash` 免费额度约
20 RPD 且与 `news-feed` 共用同一个 `GEMINI_API_KEY`，实测一天多跑几轮就会耗尽，故不再排首位）。

**配额熔断**：某模型一旦返回 429/配额类错误，**本轮不再对它重试**（否则剩下的每一批都要白撞
一次，一次运行能白撞 7 次），直接降级到下一档；新进程自动复位。非配额类错误（如 503 高需求）
仍会在下一批重试 —— 那类错误通常几分钟内自愈。日志里 429 会带出**响应体里的原因**，
否则只能看到 "Too Many Requests"，无法判断是配额还是限速。

## 双视图：简报 与 案例库

同一个页面上有两个 Tab，**同一批源、同一套打分，差别只在门槛**：

| | 简报（时间流） | 案例库（沉淀库） |
|---|---|---|
| 回答 | 今天有什么新东西 | 攒下了哪些值得反复翻的作品 |
| 取舍 | **要宽**（不漏信息） | **要严**（宁缺毋滥） |
| 上限 | 每日滚动，跨日重复降权折叠 | 600 条，按分数淘汰 |

**案例库的入库门槛**（`cases.min_total` / `cases.min_rel`，默认 5.0 / 5）：

- `library_only` 源（`awesome-llm-apps` 的人工精选清单）**无条件入库**；
- `apps` 车道的条目须过门槛，否则**只进简报、不进库**（实测 38 → 入库 18 / 挡掉 20，
  挡掉的正是「耳机剁手清单 / 手机评测」这类推荐流内容 —— 少数派是「效率工具+数码生活」版块，本身不是 AI 源）；
- `min_rel` 由 3 收到 5：`rel=4` 的多是「OS 评测 / .NET WASM 运行时 / 3D 打印机器人」这类与
  AI 关联很弱的东西，对「学别人的 AI 应用」没有价值（库 148→147 条、日入库约 17→14 条）；
- 种子条目带 `src: "seed"` 永久保护；淘汰按**分数**降序，不按 `lastSeen`
  （后者是「新的留老的扔」，与作品集用途相反）；
- `cases.rescore_max`：换口径前入库、没有 `score` 的老记录，每次运行自动补一小批分
  （走同一套 LLM 打分）后立即按门槛判去留 —— **只过滤"新增"是不够的**，
  已在库里的差条目必须显式剔除，否则永久留存。

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

**定时**：GitHub Actions `schedule`（`30 23 * * *` UTC = 北京 **07:30**）。为什么是 07:30 而不是
08:10 —— GitHub 的 schedule 要排共享队列、实测经常延迟十几到几十分钟，往前留一档才能保证
08:00 前拿到产物。本 workflow 也开着 `workflow_dispatch`，可接外部触发（如 Cloudflare 定时），
但**一旦启用必须同时删掉 `schedule`**：两边各跑一次而 `concurrency` 只排队不合并，
会导致 LLM 免费额度翻倍消耗、产物提交两次。

## 路线

- **P0 已完成** 源基线探测 —— 建仓 + probe workflow，源实测基线（两环境结论相反，见下）
- **P1 已完成** 抓取 → 时间窗（含保底）→ 去重 → 跨源折叠 → 跨日标记 → JSON → Pages 面板
- **P2 已完成** LLM 增强：中文摘要 + 英文标题中文化 + 三维打分 + 主题标签 + 分级折叠
- **P2.5 已完成** 「模型发布」车道：三种专用解析器 + 匿名公测识别 + 模型链补第 4 档
- **P3 已完成** 「应用案例」车道 + 案例库累积 + 终端风格前端（简报 / 案例库双视图）
- **P3.5 已完成** 案例库选条规则（入库门槛 + 按分数淘汰 + 补分自愈）· 米白主题 · 自定义域
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

## 新增「累积型数据集」的坑（案例库）

与"每日产物"不同，**数据集是增量的**，加规则时的迁移成本高一个量级：

- **「不新增」≠「删除」** —— 只改入库条件，已在库里的差条目躺在缓存里永不消失。
  必须显式剔除当轮命中的不达标项，并给无判据的老记录**补分**（见「双视图」一节）。
- **门槛要留「判不了就不杀」的口子** —— 老记录、LLM 不可用时都没有分数，此时一律保留，
  否则会误删种子和全部历史。同理入库门槛在 LLM 不可用时只按兜底总分把关，
  否则 **LLM 一挂案例库就静默停止增长**（不报错、只是不再有新条目）。
- **分类口径变更要整库归一化** —— URL 已失效的老条目永不进入写库循环，
  只靠"命中即更新"会留下永久孤儿筛选项（实测过一个只占 1 条的孤儿分类长期占着筛选位）。

