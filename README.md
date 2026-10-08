# AI 前沿雷达（ai-radar）

每日自动追踪大模型 / Agent / Skill 生态：抓取 → 硬规则筛选 → LLM 增强 → JSON → 三视图面板。

**线上**：https://ai-radar.hellohopo.dpdns.org/ （老地址 `homjanon.github.io/ai-radar/` 仍可访问、不跳转）

已接入 `nav` 首页第 3 位，并被「老张工具箱」App 收纳（`app.phase=4`、WebView 打开）。

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
docs/index.html               面板（简报 / 案例库 / 条目详情，读 latest.json 与 data/cases.json）
docs/latest.json              最新一期产物
docs/daily/{date}.json        当日归档（保留 30 天）
docs/data/cases.json          应用案例库（跨日累积，按质量保留）
docs/data/reports/{ts}.json   运行报告（每源通路/条数/降级原因，可回溯）
docs/CNAME                    自定义域（ai-radar.hellohopo.dpdns.org）
.github/workflows/probe-sources.yml   源探测（手动 + 每月自动）
.github/workflows/fetch.yml           抓取发布（每日 08:10 北京，由 Cloudflare Worker 触发）
```

## 三层处理链路

| 层 | 职责 | 谁来做 |
|---|---|---|
| **选条** | 时间窗（含保底）→ 标题去重 → 跨源折叠 → 跨日标记 | **纯硬规则**，确定性、可复现 |
| **分级** | 重磅关键词硬规则 > 加权总分（rel .45 / info .30 / fresh .25） | 硬规则 + LLM 分数；无 LLM 时用「车道基线 + 新鲜度」兜底打分 |
| **表达** | 中文摘要（40–80 字）· 英文标题中文化 · 主题标签 · 应用形态（仅 apps 车道）· **重磅条目正文中文化**（仅 top，产出 `descZh`） | **只由 LLM 做**，失败即降级为原文 |

LLM 按**语言 × 是否应用案例**四组拆批（英文 `translate` / 中文 `summarize`，各自再分
「要 type」与「不要 type」）—— 指令一旦混批，模型会整批统一处理：要么英文标题漏译，
要么给新闻也硬塞一个「应用形态」。模型链逐档降级，全失败也不空窗。

**链序**：`agnes-3.0-flash → gemini-3.5-flash-lite → gemini-3-flash → agnes-2.5-flash` ——
免费不限量的放首位，两个 gemini 额度互相独立、居中互为备份（`gemini-3-flash` 免费额度约
20 RPD 且与 `news-feed` 共用同一个 `GEMINI_API_KEY`，一天多跑几轮就会耗尽，故不排首位）。

**配额熔断**：某模型一旦返回 429/配额类错误，本轮不再对它重试（否则剩下的每一批都要白撞
一次），直接降级到下一档；新进程自动复位。非配额类错误（如 503 高需求）仍会在下一批重试
—— 那类错误通常几分钟内自愈。日志里 429 会带出响应体里的原因，否则只能看到
"Too Many Requests"，无法判断是配额还是限速。

## 面板：简报 / 案例库 / 条目详情

同一个页面上有两个 Tab，**共用同一批源与同一套打分，差别只在「留存门槛」**：

| | 简报（时间流） | 案例库（沉淀库） |
|---|---|---|
| 回答 | 今天有什么新东西 | 攒下了哪些值得反复翻的作品 |
| 取舍 | **要宽**（不漏信息） | **要严**（宁缺毋滥） |
| 上限 | 每日滚动；跨日重复**只降权、不剔除**（前端不把它排进重磅/关注区，单独成「昨日已收录」） | 600 条，按分数淘汰 |
| 排序 | 分级 → 总分 → 新鲜度 | **有分的在前、按分降序** |

**案例库的入库门槛**（`cases.min_total` / `cases.min_rel`，默认 5.0 / 5），只对 `apps` 车道生效：

- 不过门槛的条目**只进简报、不进库** —— 简报要宽（不漏），案例库要严（宁缺毋滥）；
- `min_rel` 收到 5：`rel=4` 的多是「OS 评测 / .NET WASM 运行时 / 3D 打印机器人」这类
  与 AI 关联很弱的东西，对「学别人的 AI 应用」没有价值；
- 淘汰按**分数**降序，不按 `lastSeen` —— 后者是「新的留老的扔」，与作品集用途相反；
- `cases.rescore_max`（默认 30）：没有 `score` 的老记录，每次运行自动补一小批分后
  **立即按门槛判去留** —— 只过滤「新增」是不够的，已在库里的差条目必须显式剔除。

### 外链：AIHOT 的三个板块（不复用轮子）

页脚上方有一排 AIHOT 外链 —— **模型榜 / 热度榜 / 全部动态**。这三个板块我们不重复造，
直接跳转复用（[aihot.news](https://aihot.news/)）。

**为什么只做链接、不接数据**（2026-10-08 实测）：

| AIHOT 端点 | curl 结果 | 说明 |
|---|---|---|
| `/leaderboard`、`/hot`、`/all`、`/topics`、`/daily` | **403** | Cloudflare 拦非浏览器请求 |
| `/feed.xml`、`/feed/full.xml`、`/feed/all.xml` | 200 | 开放 feed，可用 |

403 拦的是**服务端抓取**，**浏览器点击跳转不受影响** —— 所以"跳转复用"成立，
"数据接入"才需要另想办法。另外实测 `/feed.xml` 的 50 条里，有 41 条与我们已有内容重叠，
真盲区仅约 9 条；且它的中文标题与我们的英文原标题**精确去重命中 0 条**，
直接接入会把已覆盖内容原样重复进库。**故暂不接入。**

### 条目详情（内联展开）
简报里每条卡片下有 `展开全文` —— 点开后**在列表内展开正文**，不跳页、不新开 Tab：

- 正文取自 `desc`（抓取时补抓并截到 2000 字），保留原始换行；
- **同一时间只展开一个**（长列表全展开会一次渲染十几段长文，手机上会卡），
  `Esc` 可收起；卡片标题带 `aria-expanded`，键盘可达；
- 手机端点开后标题吸顶，长正文滚到底也看得到「收起」；
- 正文**缺失或过短**（≤120 字）的条目**不显示这个按钮** —— 那种情况正文和摘要重复，
  展开没有增量价值。

> **列表区不预览正文**（2026-10-08 改）。此前是三层展示：摘要 → 正文前 220 字 →
> 点开又是完整正文。中间那层被展开态**完全包含**，是纯冗余。现在只留
> 「摘要 + 展开按钮」：看完摘要有兴趣再点开，正文与译文只在展开态出现。
> （同时删掉了失效的 `.sm2` 样式。）

### 重磅条目的正文翻译（`descZh`）

新增一步**只对 `level == "top"` 的英文长文**做正文翻译，产出 `descZh`：

| 条件 | 值 | 理由 |
|---|---|---|
| 分级 | 仅 `top` | 全量约 140 条/天，全翻无谓；**该看的其实就是重磅那十来条** |
| 语言 | 仅英文源（`_translate_title`） | 中文源无需翻译 |
| 正文长度 | ≥ 300 字（`body_min_chars`） | 太短的正文与摘要重复 |
| 每批条数 | 5（`batch_size`） | 正文长，25 条/批会顶到 `max_tokens` 被截断 |
| 单条上限 | 1200 字（`body_max_chars`，临时截断、翻完还原） | 控 token，不污染产物里的完整 `desc` |
| 兜底上限 | 20 条（`max_items`） | 阈值异常时不会爆量 |

**独立模型链**（`translate.body.models`，2026-10-08 加）：正文翻译**不复用摘要链**，
而是单开一条 `gemini-3.5-flash-lite → gemini-3-flash → agnes-2.5-flash`。三个理由：

1. 摘要链首模型 `agnes-3.0-flash` 是**推理模型，长输入易 90s 超时**；正文比摘要长一个
   量级（实测 5 条 × 1200 字），把它从正文链摘出去两头受益；
2. 两条链走**不同的 Key 配额池**（agnes 不限量 / gemini 约 20 RPD），一条限流不拖累另一条；
3. 首选最轻的 `gemini-3.5-flash-lite`，把 agnes 的无限额度留给 141 条摘要 ——
   即"减轻首选模型压力"。思路学自 portfolio 仓的「导语由下一个模型生成」。

未配置 `body.models` 时**自动回退全局链**，老配置不会因此失效（冒烟测试有锁）。

⚠️ gemini 免费额度约 20 RPD 且与 news-feed 共用同一个 `GEMINI_API_KEY` ——
本链每轮仅 2 批调用（7 条 top）占用可控；但若把翻译范围扩到 `paper`/`community`
（约 8 批）会直接吃光额度，**扩展前必须先核实当日剩余额度**。

**失败语义：写不出 `descZh` 就不写** —— 前端 `bodyHTML()` 检测到没有 `descZh` 就只显示原文，
不会出现空正文。翻译是**增益**，不是依赖。整体开关是 `sources.json` 的
`translate.body.enabled`，关掉它标题/摘要/打分/分级全部照常。

前端行为：有 `descZh` 的条目，「展开全文」按钮标为 `展开全文 · 已译`，展开后出现
**「中文 / 原文」切换钮**。

### 正文网页残留清洗（`_clean_web_junk`）

Jina Reader 抓回的正文在**开头**常夹着页面 UI 碎片。它们不含 HTML 标签，
`strip_tags()` 拦不住，会一路流进 `desc` 并被送进 LLM 当正文 ——
实测 140 条里 13 条中招，top 的 7 条译文有 3 条开头带碎片。四类形态：

| 形态 | 实例 |
|---|---|
| Tailwind/CSS 残片 | `:last-child]:mb-0"> \n \n Today, we release two open decision models...` |
| SPA 占位符 | `Loading… Share Introducing Intelligent UI Introducing Intelligent UI ...` |
| 目录/导航回环 | `Share The problem The problem The result How we found the proof ...`（小标题重复两遍） |
| **行内 UI 注释** | `Millennium Prize Problems ␠(opens in a new window) represent ...`（␠ = U+2060） |

**判据用「结构」而不是「关键词」**：正文里出现一个 `Share` 或 `Loading` 完全正常
（实测有 "The page shows a Loading state"），按词删必然误伤。所以：

- **目录块**：靠 3~5 词的 n-gram 回环识别 —— 重复一结束、出现新句子就是正文起点。
  只在开头 80 词窗口内找，全篇扫描会把正文里的正常重复也算进来
  （实测反例：GPT-6 那篇正文有 "7-Speed Bicycle" 重复 3 次，全篇扫描会把切点算到第 192 词、一刀砍掉半篇）。
- **CSS 残片 / Loading**：这两个字符串本身就是特征，精确匹配即可。
- **行内 UI 注释**（2026-10-08 补）：前三条规则都抓不到它 —— 既不只在开头、也不是
  重复片段、更无 CSS 特征。实测 4 条 `openai.com` 的 `desc` 里嵌着
  `\u2060(opens in a new window)`：一个 **U+2060 WORD JOINER**（不可见）拼上无障碍提示。
  **危害不止难看：它会被模型忠实翻译成「（在新窗口中打开）」混进中文译文**，
  等于网页 UI 文案被译成中文出现在正文里。
  处理方式：就地删掉 U+2060/U+FEFF/U+200B 与提示短语（英文态 + 中文态）。
  这是纯 UI 附属文字，**行内删除不影响任何语义**。

**防误伤**（误删正文比漏清垃圾严重得多）：

1. 只动**开头**/**结尾**/**明确的行内 UI 注释**，正文中段文字一律不碰；
2. 切割前确认砍掉的那段**没有句末标点**（目录小标题不会成句）—— 这条让「数学那篇」
   正确地选择了**不切割**：它 `Share` 后直接是正文，没有目录块；
3. **安全阀**：删除量超过原文 30% 整体放弃，宁可留垃圾；
4. 安全阀**只统计结构清洗的删除量**，不含行内 UI 注释 —— 后者是固定短语，
   在长文里可能命中多次，按原比例算会把好正文误判成"砍太狠"（触发时退回
   `_strip_inline_ui()` 的"只做行内清理"版本，而不是退回带垃圾的原文）。

效果（140 条线上真实数据）：CSS 残片 5→0、目录块 6→0，改动 13 条，**零误伤、安全阀零触发**。

## 七条车道

| 车道 | 覆盖 | 代表源 |
|---|---|---|
| `model` 模型发布 | **新模型上架与匿名公测**（技术领先最直接的信息） | llm-leaderboard（含上架时间）、OpenRouter stealth、HF 新模型 |
| `official` 官方一手 | 模型/产品发布，权重最高 | OpenAI、Anthropic、DeepMind、Google AI、Hugging Face |
| `apps` 应用案例 | **别人用 AI 做出来的东西**（找灵感 + 学实现） | Show HN、V2EX 分享创造、少数派、r/SideProject、HF Spaces |
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
该仓已代抓 LMArena Elo 与上架时间，省掉了「自己抓 JS 渲染页 + 自己做跨日快照 diff」这一整套。

## 实例池

所有 RSSHub 路由按以下顺序兜底、**命中即止**（沿用 portfolio 的全项目统一实例池）：

```
hub.slarker.me → rsshub.rssforever.com → rsshub.umzzz.com
→ rsshub.isrss.com → rsshub.ktachibana.party → rsshub-balancer.virworks.moe
```

中文车道额外先试 `rss.injahow.cn`（.cn 专属实例）。

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
python scripts/fetch_ai.py --only cn                     # 只抓中文车道
python scripts/fetch_ai.py --id hf-blog,arxiv-cs-cl       # 只抓指定源（调试）
python scripts/fetch_ai.py --no-jina                     # 关闭正文补抓

# 源可用性探测（只读，不写文件）
python scripts/probe_sources.py
python scripts/probe_sources.py --only cn                # 只测中文车道

# 本机调试需先开代理（谷歌系/部分源在国内直连不通）
HTTPS_PROXY=http://127.0.0.1:7890 python scripts/fetch_ai.py --outdir docs
```

探测结果会写进 Actions 的 **Step Summary**（表格形式），并打印完整日志，**不落任何文件**。

**定时**：北京 **08:10**，由 **Cloudflare Worker 心跳**触发（`qdii-dispatch`，与 `news-feed`
同一套）。本仓 workflow **只保留 `workflow_dispatch`，不使用 GitHub 自带 `schedule`**
（为什么 + 不能改回去，见「踩坑记录 · 定时与触发」）。

漏跑补跑：`/trigger?repo=ai-radar&key=<DISPATCH_KEY>`，或 Actions → fetch-ai → Run workflow。

## 踩坑记录

**数据管线**

- **`sort=createdAt` 的 HF 模型列表绝大多数是个人测试仓库**（实测首批 8 条全是
  `downloads=0 / likes=0` 的占位仓）。降噪两道：`exclude_tags` 剔量化/微调衍生品，
  `min_likes=2` 卡门槛。真正的机构发布几小时内就有点赞，不会漏。
- **标题里不能出现「权重」二字** —— 会命中硬规则 `TOP_KW`，把一堆测试仓硬提成「重磅」，
  故 HF 条目标题用「HF 新模型：」。
- **「过滤后为空」≠「源降级」** —— 专用解析器允许返回空列表，否则 HF 某天只有测试仓时
  会被误报为抓取失败、污染降级告警。
- **本机经代理实测 ≠ Actions 可用性** —— 实测 19 个源在本机报 `Tunnel connection failed: 502`，
  失败耗时固定在 10.0–10.3 秒（代理规则拦截特征，非源失效）。**凡 502/超时一律标「待复核」**，
  必须在本 workflow 里复核。
- **desc 深浅不一会静默毁掉摘要质量** —— 各 RSSHub 实例对同一条目返回的 `desc` 长度差异极大
  （实测 0 ~ 11148 字：HF blog 全文 11148，而量子位只有 0）。沿用「命中即止」会锁死在
  **导语版实例**上，故每源带 `desc_min` 门槛：不达标继续试下一实例。

**LLM 层**

- **降级路径必须也有可用的默认值** ⚠️ —— LLM 全失败时若分级完全依赖它的分数，所有条目
  都会落到最低级、**首屏只剩一个折叠条**。故备有「车道基线 + 新鲜度」兜底打分。
- **分级用「分数下限 + 名额上限」**，不要纯绝对阈值 —— 模型某天整体给分偏高时 top 会失控
  （实测 90 条里 50 条被判重磅）。
- **提示词必须做分数锚定** —— 只写"0-10 分"模型自然给 8-9 分；要写明每档对应什么，
  并强调"不要普遍给高分"。
- **单条失败不能拖垮整批** —— `Microsoft Data Formulator` 这种纯专有名词标题模型不译是
  合理的，原逻辑单条未译就抛异常、白跑掉 43 条。改为「含中文才采纳，整批译出率 <30% 才判失败」。
- **配额类错误要熔断** —— 某模型 429 后若不熔断，剩下的每一批都会再去撞一次（一次运行能白撞
  7 次）。但 **503/高需求不熔断** —— 那类几分钟自愈，下一批重试才有意义。
- **`str(HTTPError)` 会丢掉响应体** —— 而配额指标与重试建议都在 body 里，
  不打出来就只能靠猜。

**累积型数据集（案例库）**

- **「不新增」≠「删除」** —— 改入库条件只能挡住新增，已在库里的差条目躺在缓存里永不消失。
- **门槛读的字段，先确认它真的被赋值过** ⚠️ 最隐蔽的一种失败 —— 新门槛若读一个此前没人用的
  字段，要先确认它确实被赋值；否则门槛会**静默地把整类内容全部拦掉**，而日志只显示一个
  「过滤掉 N 条」的数字，从数字上完全看不出异常。
- **「判不了」必须保留** —— 无分数的老记录、LLM 不可用时一律保留，否则会误删全部历史；
  同理入库门槛在 LLM 不可用时只按兜底总分把关，否则 LLM 一挂案例库就静默停止增长。
- **分类口径变更要整库归一化** —— URL 已失效的老条目永不进入写库循环，只靠「命中即更新」
  会留下永久孤儿筛选项（`COARSE_BY_FINE` 就是为此保留的历史分类折算表）。

**定时与触发**

- **不要给本仓加回 GitHub `schedule`** —— 定时统一由 Cloudflare `qdii-dispatch` 的心跳打
  `workflow_dispatch`（按需立即执行、不排队）。GitHub 自带 `schedule` 走共享队列，实测延迟极大
  （应 07:30、实际 10:18，晚 2 小时 48 分）。**两边并存会真跑两轮**：`concurrency` 只排队不合并
  （`group: fetch-ai` / `cancel-in-progress: false`），结果是 LLM 免费额度翻倍消耗、产物重复提交。
- **验证 CF → 本仓的通路，别拿 `fetch.yml` 试** —— 它会真跑一轮，消耗 LLM 额度并提交产物。
  改用只读的 `probe-sources.yml`（不写文件、不提交、不用 LLM）：在调度器临时挂一个槽位指向它，
  到点看 `gh run list` 是否出现 `workflow_dispatch` 运行，一次即可验证「PAT 已授权本仓 +
  workflow 文件名正确 + 通路通」，随后撤掉该槽位。
