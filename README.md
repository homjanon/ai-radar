# AI 前沿雷达（ai-radar）

持续追踪大模型 / Agent / Skill 生态的自动化聚合站。**当前处于 P0 阶段：源基线探测，尚无抓取与前端。**

## 这个仓要做什么

把国内外 AI 前沿动态（官方发布、论文、开源社区、中文资讯）自动汇总成一份「该看的少数几条」，
而不是又一堆信息流。设计原则照搬 `homjanon/news-feed` 已验证的三条：

1. **能用硬规则就不用 LLM** —— 选条、去重、时间窗全是确定性逻辑，结果可复现；
2. **每一层都必须能降级** —— 源挂了换实例，模型挂了换下一档，绝不空窗；
3. **配置驱动** —— 加源、换路由只改 `scripts/sources.json`，不改代码。

## 目录

```
scripts/sources.json          源配置（单一数据源）· 五车道 · 每源带 desc_min 门槛
scripts/probe_sources.py      源可用性探测（只读）
.github/workflows/probe-sources.yml   探测 workflow（手动 + 每月自动）
```

## 五条车道

| 车道 | 覆盖 | 代表源 |
|---|---|---|
| `official` 官方一手 | 模型/产品发布，权重最高 | OpenAI、Anthropic、DeepMind、Google AI、Hugging Face |
| `paper` 论文与基准 | 论文精选与用量趋势 | HF Daily Papers、arXiv cs.CL/cs.AI、OpenRouter |
| `community` 社区与开源 | 开发者真实热度 | Hacker News、GitHub Trending、GitHub Releases |
| `media` 媒体与分析 | 深度解读与观点 | Simon Willison、TLDR AI、Ars Technica |
| `cn` 中文动态 | 国内产业与工程视角 | 量子位、雷锋网 AI、36氪 AI、InfoQ |

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
# Actions 页面手动触发（推荐，拿到的是真实基线）
#   Actions → probe-sources → Run workflow

# 本机调试（需先开代理）
python scripts/probe_sources.py
python scripts/probe_sources.py --only cn      # 只测中文车道
python scripts/probe_sources.py --id hf-blog   # 只测某个源
```

探测结果会写进 Actions 的 **Step Summary**（表格形式），并打印完整日志，**不落任何文件**。

## 路线

- **P0（当前）** 源基线探测 —— 建仓 + 本 workflow，确定最终源清单与实例顺序
- **P1** 抓取 → 时间窗 → 去重 → JSON → Pages 面板（纯硬规则）
- **P2** LLM 增强：中文摘要 + 相关性打分 + 跨源聚类折叠 + 分级
- **P3** 接入 `nav` 宫格与 Android App + 邮件日报（可选）
- **P4** 趋势量化：周报四指标（厂商发布频率 / 开源权重占比 / 星标增速 / 主题热度）
