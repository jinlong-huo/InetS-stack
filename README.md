# Group Toolkit

> 可传承的组内工作流基础设施。
> 自动化发现论文 → 结构化阅读 → 知识沉淀 → 新成员上手 → 离组交接，全链路模板化。

## 地图

```
group-toolkit/
│
├── arxiv_digest/                ← 🎯 流水线模块 + 主入口
│   ├── Arxiv_filter.py          ←    主入口（fetch → filter → select → digest → send）
│   ├── config.py                ←    全部配置：关键词、阈值、路径
│   ├── fetch.py                 ←    arXiv API + 重试 + 错误分类
│   ├── filter.py                ←    关键词打分（主过滤器 + OCS）
│   ├── digest.py                ←    状态管理 + Top-N 选择 + Markdown
│   ├── emailer.py               ←    SMTP 邮件发送
│   ├── download_papers.py       ←    PDF 下载
│   └── rename_papers.py         ←    PDF 重命名为 Author_Year_Title
│
├── paper-notes/                 ← 📖 共享论文笔记模板
│   └── template.md              ←    标准笔记模板（what/why/how/pros/cons）
│
├── knowledge-base/              ← 🧠 沉淀：按主题组织的研究知识（共享）
│   ├── glossary.md              ←    术语表（新人第一站）
│   ├── reading-roadmap.md       ←    按方向分级的阅读路线图
│   └── topics/                  ←    各方向的论文脉络与 SOTA
│
├── onboarding/                  ← 🚀 上手：新成员入职指南（共享）
│   ├── welcome.md               ←    组文化 + 第一周 checklist
│   ├── tools.md                 ←    工具安装 & 使用
│   └── how-we-work.md           ←    沟通、代码、会议、文档规范
│
├── offboarding/                 ← 👋 离组：知识交接 + 个人内容提取
│   ├── exit-checklist.md        ←    离组手续清单
│   ├── knowledge-handover.md    ←    隐性知识显性化模板
│   └── extract.sh               ←    一键导出个人目录
│
├── repro/                       ← 🔬 复现：共享复现模板
│   └── template/                ←    复现模板（环境 / 结果 / 踩坑记录）
│
├── templates/                   ← 📋 模板：组内通用格式（共享）
│   ├── weekly-report.md         ←    周报
│   ├── meeting-notes.md         ←    会议记录
│   ├── paper-presentation.md    ←    组会论文报告
│   ├── internal-review.md       ←    投稿前组内预审
│   ├── writing-checklist.md     ←    论文投稿前自查清单
│   └── figure-guide.md          ←    Figure 规范（颜色 / 排版 / 工具）
│
├── survival-guide/              ← 🧭 生存手册（共享）
│   ├── how-to-choose-problem.md ←    怎么选研究方向
│   ├── how-to-write-paper.md    ←    从 outline 到 camera-ready
│   ├── how-to-give-talk.md      ←    组会 & conference presentation
│   ├── conference-list.md       ←    各顶会 deadline 和投稿经验
│   └── career-advice.md         ←    找教职 / 实习 / 工业界
│
├── members/                     ← 👤 个人工作区（每人一个目录）
│   ├── _template/               ←    新成员模板
│   └── jinlong-huo/             ←    你的目录
│       ├── paper-notes/         ←    个人论文笔记
│       ├── projects/            ←    私人项目细节
│       └── repro/               ←    个人实验复现
│
└── projects.md                  ← 📊 组内活跃项目一览（共享，仅标题和阶段）
```

---

## 目录可见性

| 目录                                                                   | 可见范围                                  | 离组时               |
| ---------------------------------------------------------------------- | ----------------------------------------- | -------------------- |
| `templates/` `onboarding/` `survival-guide/` `knowledge-base/` | **全组共享**                        | 留在 repo            |
| `paper-notes/template.md` `repro/template/`                        | **全组共享**                        | 留在 repo            |
| `projects.md`                                                        | **全组共享**（仅标题+阶段，无细节） | 更新 owner 后留 repo |
| `members/<name>/paper-notes/`                                        | **组内**                            | 导出带走             |
| `members/<name>/repro/`                                              | **组内**                            | 导出带走             |
| `members/<name>/projects/`                                           | **本人 + 导师**                     | 导出带走或交接       |

---

## 🔍 arXiv Daily Digest

每天自动从 arXiv 抓取最新论文，按“相关性 + 核心/创新信号”排序，选出最相关的 top-15 发送到邮箱。

**❗ 注意关键词选取和对应权重设置，直接关乎筛选文章质量。**

**关注方向**：LLM 推理 / GPU 数据中心 / RDMA 网络 / 光交换 (OCS) / 调度与资源分配 /
光数据中心可靠性（failure、circuit remapping、collective remapping）。

打分优先 **core / innovative** 论文：标题命中加权、同一概念的同义词只计一次、
综述/教程降权、`we propose / novel / prototype` 等贡献措辞小额加分。
OCS spotlight 是 **software-first**：控制面、调度、路由、重配置、可靠性、
故障恢复、collective remapping 全额计分；硅光/MEMS/WSS/调制器等器件词降权，
纯器件论文最多 2 篇，且排序时软件论文永远排在器件论文之前。

Digest 现在给出**完整摘要**、作者（过多时前 8 位 + et al.）、主分类、提交日期、
arXiv/PDF 链接、命中原因（关键词 + 权重 + 是否标题命中）、HF upvotes 和
OpenAlex/Semantic Scholar 引用数/TLDR（有数据时）。不是只给一个 400 字摘要片段。

### 数据源与抓取可靠性

| 源 | 是否需要 key | 作用 |
| --- | --- | --- |
| arXiv API | 否 | 主源：按分类 + 日期窗口抓取 |
| arXiv advanced search HTML | 否 | API 429/超时时的自动回退，支持真实日期范围和完整摘要 |
| OpenAlex | 否 | 无 key 的 bibliographic 回退，arXiv search 被限流时仍能按日期+主题找到论文 |
| Hugging Face Daily Papers | 否 | 第二个发现源，补充 ML systems 论文 |
| Semantic Scholar | 是（`SEMANTIC_SCHOLAR_API_KEY`） | 发现 + 引用数/TLDR enrichment |

抓取层把 **“当天没有论文”** 和 **“抓取失败”** 分开：只有带
`opensearch:totalResults` 的合法 Atom feed 才允许判空；代理登录页、HTML
拦截页、超时、429 都算失败。任一分类失败会触发 HTML 回退；全部分类失败
且没有回退结果时，程序以非零退出码结束，并写 `arxiv_digest/fetch_report.json`，
不会再生成一个看起来正常的空 digest。

`--date YYYY-MM-DD` 现在只抓**这一天**（旧实现会悄悄抓最近 3 天，容易让
更早的论文挤掉目标日的论文）。补一段日期用 `--from/--to`。


### 快速开始

**1. 装依赖**

```bash
pip install feedparser
```

**2. 配邮箱**

密码**不要写在代码里**，二选一：

```bash
export ARXIV_DIGEST_EMAIL_PASSWORD="你的Gmail应用专用密码"
# 或: echo "密码" > .email_password
```

> Gmail 应用专用密码：https://myaccount.google.com/apppasswords（需先开两步验证）

**3. 测试**

```bash
python3 arxiv_digest/Arxiv_filter.py --send
```

常用参数：

```bash
python3 arxiv_digest/Arxiv_filter.py --date 2026-10-13       # 只回填这一天
python3 arxiv_digest/Arxiv_filter.py --from 2026-10-01 --to 2026-10-13
python3 arxiv_digest/Arxiv_filter.py --ignore-seen           # 重评历史窗口
python3 arxiv_digest/Arxiv_filter.py --wait                  # 429 时等待重试
python3 arxiv_digest/Arxiv_filter.py --no-extra-sources      # 跳过 HF/OpenAlex/S2
```

### 调参

| 参数                       | 默认值 | 作用                                  |
| -------------------------- | ------ | ------------------------------------- |
| `MIN_SCORE`                | 5      | 主 digest 初筛门槛，越低越多          |
| `MAX_PAPERS`               | 15     | 主 digest 最多显示几篇                |
| `TITLE_BOOST`              | 0.5    | 标题命中额外权重（核心工作在标题点题） |
| `CORE_PICK_MIN`            | 16     | 标 ★ core pick 的 core_score 门槛     |
| `OCS_MIN_SCORE`            | 6      | OCS software / neutral 论文门槛       |
| `OCS_MIXED_MIN_SCORE`      | 10     | 软件/器件信号混合论文的门槛           |
| `OCS_HARDWARE_MIN_SCORE`   | 12     | 纯器件论文进入 spotlight 的更高门槛   |
| `OCS_HARDWARE_WEIGHT`      | 0.35   | 器件/物理词权重                       |
| `MAX_OCS_HARDWARE`         | 2      | OCS spotlight 器件论文上限            |
| `MAX_OCS_PAPERS`           | 10     | OCS spotlight 最多显示几篇            |
| `SUMMARY_MAX_CHARS`        | 2400   | digest 摘要最大字符数（默认全文）     |
| `CATEGORY_FILTERS`         | 见 config | 对 cs.LG / physics.optics 等大分类加兴趣约束 |

**分类**：`cs.NI`, `cs.DC`, `cs.AR`, `cs.PF`, `cs.LG`, `cs.ET`,
`physics.optics`, `physics.app-ph`, `eess.SP`。`cs.AI` 已移除（泛 AI 噪声），
`cs.LG` / `cs.ET` 用于补 ML systems 和光互连；`CATEGORY_FILTERS` 会对
`cs.LG`、`physics.optics` 等大分类加 `abs:"..."` 兴趣约束，减少无效抓取。

**额外源开关**：

```bash
python3 arxiv_digest/Arxiv_filter.py --no-extra-sources   # 只跑 arXiv
export SEMANTIC_SCHOLAR_API_KEY="..."                     # 启用 S2 发现/enrichment
```


### 校园网 / 受限网络（SJTU）说明

在 SJTU 校园网（或任何受限网络）下，arXiv 各域名的可达性并不一致：

| 主机                       | 状态              | 说明                             |
| -------------------------- | ----------------- | -------------------------------- |
| `export.arxiv.org`（API）  | ❌ 被封 / 429 限流 | 传统 API 接口，校园网基本不可用  |
| `arxiv.org/search/advanced` | ⚠️ 时好时坏       | 高级搜索页，支持日期范围/摘要；会被限流 |
| `arxiv.org`（abs / list）  | ✅ 可用            | 主页、摘要页、列表页均正常       |
| `cn.arxiv.org`             | ✅ 可用            | 国内镜像                         |
| `openalex.org`             | ✅ 通常可用        | 无 key bibliographic 回退        |
| `huggingface.co`           | ✅ 通常可用        | HF Daily Papers（可选源）        |
| `api.semanticscholar.org`  | ⚠️ 匿名常 429      | 配 API key 后稳定                |

因此抓取在 API 失败时会**自动回退**到 `arxiv.org/search/advanced`，使用真实
日期范围按主题查询（光网络、LLM inference、MoE、集合通信、RDMA），并解析
完整摘要/作者/主分类。开关是 `ARXIV_HTML_FALLBACK`；查询列表是
`ARXIV_HTML_FALLBACK_QUERIES`。如果高级搜索也被限流，OpenAlex 会按
日期 + 主题提供第三路结果；HF Daily Papers 同时作为独立的 ML systems 源。
若所有 arXiv 路径都不可用且没有额外源结果，程序会明确失败（退出码 2），
而不是写一个空 digest。

**改用镜像**：在 `config.py` 里改这三个基址即可（默认走官方域名）：

```python
ARXIV_API_BASE_URL  = "https://export.arxiv.org"  # API 基址
ARXIV_ABS_BASE_URL  = "https://arxiv.org"         # abs / PDF 基址
ARXIV_LIST_BASE_URL = "https://arxiv.org"         # 列表页基址
ARXIV_PDF_FALLBACK_HOSTS = ["https://cn.arxiv.org"]  # 主站持续失败时的备用镜像
```

**代理**：默认**绕过**系统代理（避免 Clash X / Surge 对 arXiv 限流）。
如果你用 SJTU VPN 才能访问 arXiv，请设 `ARXIV_BYPASS_PROXY = False`。

### PDF 下载：HTTP 406 与限流

从共享校园 IP（SJTU 是 CGNAT，多人共用一个出口 IP）批量下载 PDF 时，
arXiv 的 Fastly CDN 会返回 **HTTP 406 "Not Acceptable"** —— 通常从第 10 篇左右开始。

**这不是坏链接，而是限流**：同一个请求过几秒重试就能成功。
因此下载器对 406/429/500/502/503/504 会**指数退避重试**，而不是一次失败就放弃：

| 参数                              | 默认值                 | 作用                       |
| --------------------------------- | ---------------------- | -------------------------- |
| `ARXIV_PDF_MAX_RETRIES`           | 4                      | 每篇最大重试次数           |
| `ARXIV_PDF_RETRY_BACKOFF`         | `[10, 30, 60, 120]` 秒 | 各次重试的基础退避         |
| `ARXIV_PDF_DELAY`                 | 5.0 秒                 | 篇与篇之间的基础间隔       |
| `ARXIV_PDF_DELAY_JITTER`          | 0.5                    | 间隔随机抖动（±50%）       |
| `ARXIV_PDF_COOLDOWN_AFTER_FAILS`  | 3                      | 连续失败几次后长暂停       |
| `ARXIV_PDF_COOLDOWN_SECONDS`      | 90 秒                  | 长暂停时长                 |

另外：下载内容会校验 `%PDF-` magic bytes，CDN 的 HTML 拦截页不会被当成 PDF 存盘。
若仍频繁 406，就把 `ARXIV_PDF_FALLBACK_HOSTS` 设为 `["https://cn.arxiv.org"]` 走镜像。

**失败可直接重跑** —— 已存在的文件会自动跳过，只补缺失的：

```bash
python3 arxiv_digest/download_papers.py
python3 arxiv_digest/verify_downloads.py --days 7 --download   # 审计并补下
```

---

## 📖 Paper Notes

每读完一篇论文，在 **个人目录** 下复制模板 → 填空。

```bash
cp paper-notes/template.md members/<your-name>/paper-notes/2026/作者-关键词.md
```

模板覆盖 **What / Why / How / Pros / Cons / Follow-ups**。三个月后你不会记得这篇论文讲了什么，但笔记会。

→ [paper-notes/template.md](paper-notes/template.md)

---

## 🧠 Knowledge Base

按主题沉淀知识，不按论文排列。**全组共享资产。**

- [glossary.md](knowledge-base/glossary.md) — 术语表，新人第一站
- [reading-roadmap.md](knowledge-base/reading-roadmap.md) — 按方向分级的阅读路线
- [topics/](knowledge-base/topics/) — 各方向论文脉络与 SOTA

---

## 🚀 Onboarding

新成员入职指南，按顺序读：

1. [welcome.md](onboarding/welcome.md) — 组文化 & 第一周 checklist
2. [tools.md](onboarding/tools.md) — 装好所有工具
3. [how-we-work.md](onboarding/how-we-work.md) — 日常规范
4. [reading-roadmap](knowledge-base/reading-roadmap.md) — 开始读论文

---

## 👋 Offboarding

离组流程。两件事：你带走你的，组留下组的。

1. [exit-checklist.md](offboarding/exit-checklist.md) — 逐项打勾，不遗漏
2. [knowledge-handover.md](offboarding/knowledge-handover.md) — 隐性知识显性化
3. [extract.sh](offboarding/extract.sh) — 一键导出个人目录

```bash
./offboarding/extract.sh <your-name> ~/Desktop/group-export
```

---

## 🔬 Repro

复现论文的实验记录。每篇一个目录，记录环境、结果、踩坑。
个人复现记录放 `members/<name>/repro/`。

→ [repro/README.md](repro/README.md)

---

## 📋 Templates

| 模板                                                    | 用途           | 频率         |
| ------------------------------------------------------- | -------------- | ------------ |
| [weekly-report/](templates/weekly-report/)               | 周报           | 每周五       |
| [meeting-notes.md](templates/meeting-notes.md)           | 会议记录       | 每次会议     |
| [paper-presentation.md](templates/paper-presentation.md) | 组会讲论文     | 轮到你       |
| [internal-review.md](templates/internal-review.md)       | 投稿前组内预审 | 每次投稿前   |
| [writing-checklist.md](templates/writing-checklist.md)   | 论文投稿前自查 | 每次投稿前   |
| [figure-guide.md](templates/figure-guide.md)             | Figure 规范    | 画图前看一眼 |

---

## 🧭 Survival Guide

组员真正需要、但很少被写下来的东西。按需阅读：

| 文档                                                               | 什么时候读       |
| ------------------------------------------------------------------ | ---------------- |
| [how-to-choose-problem.md](survival-guide/how-to-choose-problem.md) | 不知道做什么方向 |
| [how-to-write-paper.md](survival-guide/how-to-write-paper.md)       | 第一次写 paper   |
| [how-to-give-talk.md](survival-guide/how-to-give-talk.md)           | 下个月要讲 talk  |
| [conference-list.md](survival-guide/conference-list.md)             | 选投稿目标       |
| [career-advice.md](survival-guide/career-advice.md)                 | 考虑下一步       |

---

## 📊 Active Projects

→ [projects.md](projects.md) — 谁在做什么，什么阶段，什么产出（仅标题和阶段，细节在个人目录）。

---

## 设计原则

- **模板驱动** — 能填空不写空白页。
- **写下来才算发生过** — 讨论、决策、理解，全落成文字。
- **一个人的笔记 → 全组的资产** — glossary 补一个术语、reading-roadmap 加一篇推荐，下一个成员直接复用。
- **共享与个人分离** — 个人笔记、项目细节、实验记录放在 `members/<name>/`，离组时一键导出，共享资产不受影响。
- **来有 onboarding，走有 offboarding** — 进组有 checklist 接你，离组有 checklist 送你。
- **迭代优于完美** — 不完整的 PR > 空着的 TODO。
