#!/usr/bin/env python3
"""ArXiv Daily Digest — 全部配置常量、路径、时区工具。"""

import os
from pathlib import Path
from datetime import datetime, timezone, timedelta

# ── SSL cert fix for macOS + conda Python ──────────────────────
try:
    import ssl
    import certifi
    _cafile = certifi.where()
    ssl._create_default_https_context = lambda cafile=_cafile: ssl.create_default_context(cafile=cafile)
    os.environ.setdefault('SSL_CERT_FILE', _cafile)
except ImportError:
    pass

# ── Timezone (Beijing) ─────────────────────────────────────────
TZ = timezone(timedelta(hours=8))

# Set by --date flag for backfilling a specific day.
# When non-None, bj_now() and bj_today_str() return this date instead of "now".
DATE_OVERRIDE: str | None = None

def bj_now():
    if DATE_OVERRIDE is not None:
        d = datetime.strptime(DATE_OVERRIDE, "%Y-%m-%d")
        return d.replace(hour=23, minute=59, second=59, tzinfo=TZ)
    return datetime.now(TZ)

def bj_today_str():
    if DATE_OVERRIDE is not None:
        return DATE_OVERRIDE
    return bj_now().strftime("%Y-%m-%d")

# ── Proxy / endpoint configuration (SJTU / GFW bypass) ─────────
#
# From behind the national firewall (e.g. SJTU campus):
#   - export.arxiv.org (API)  → blocked or 429 rate-limited
#   - arxiv.org/search/       → blocked / times out
#   - arxiv.org               → accessible (homepage, abs pages, list pages)
#   - cn.arxiv.org            → accessible (Chinese mirror)
#
# You can override the base URLs below to use a working mirror.
# For SJTU: try setting ARXIV_API_BASE_URL = "https://arxiv.org"
#           (the abs scrape fallback automatically mirrors your choice)
#
# Proxy bypass: by default we BYPASS the system proxy for arXiv hosts
# to avoid VPN/Clash X rate-limiting.  If you ARE behind a proxy that
# gives you access to arXiv (e.g. SJTU VPN), set ARXIV_BYPASS_PROXY=False.
ARXIV_PROXY_BYPASS_HOSTS = ["export.arxiv.org", "arxiv.org"]
ARXIV_BYPASS_PROXY = True

# Configurable base URLs — change these to use a mirror (e.g. "https://arxiv.org")
ARXIV_API_BASE_URL = "https://export.arxiv.org"   # legacy API endpoint
ARXIV_ABS_BASE_URL = "https://arxiv.org"           # abs / PDF base
ARXIV_LIST_BASE_URL = "https://arxiv.org"          # list-page base (always accessible from CN)

# When True and the API fails/returns 0 entries for a category (e.g.
# rate-limited or blocked), fall back to arxiv.org's advanced-search HTML.
# The HTML endpoint is on separate infrastructure and supports real date
# ranges, full abstracts, authors and primary categories.
ARXIV_HTML_FALLBACK = True
ARXIV_HTML_PAGE_SIZE = 50
ARXIV_HTML_MAX_RESULTS = 150
ARXIV_HTML_BREAKER_AFTER = 3   # 连续 N 个 HTML 查询失败后停止，交给 OpenAlex/HF
ARXIV_HTML_LOOKBACK_DAYS = 1   # 时区/提交日期跨日：回退时多看一天，避免漏掉目标日论文
# Topic queries used by the HTML fallback.  They are intentionally phrased
# around the group's interests instead of "all of arXiv": a full 3-day dump is
# both slow and impossible to paginate reliably when the API is throttled.
ARXIV_HTML_FALLBACK_QUERIES = [
    '"optical network"',
    '"optical interconnect"',
    '"optical circuit switch"',
    '"optical datacenter"',
    '"datacenter network"',
    '"LLM inference"',
    '"mixture of experts"',
    '"collective communication"',
    '"RDMA"',
    '"co-packaged optics"',
]

# ── PDF download robustness ────────────────────────────────────
#
# arXiv's Fastly CDN answers burst-y automated PDF fetches from a shared
# campus IP (SJTU CGNAT) with HTTP 406 "Not Acceptable" — usually from
# roughly the 10th request onward in a tight loop.  It is a throttle, not
# a bad URL: the exact same request succeeds when retried a moment later.
# So the downloader must retry instead of treating the first non-200 as fatal.
ARXIV_PDF_MAX_RETRIES = 4               # attempts per paper (1 + retries)
ARXIV_PDF_RETRY_BACKOFF = [10.0, 30.0, 60.0, 120.0]   # base seconds per retry
ARXIV_PDF_RETRY_STATUSES = (406, 429, 500, 502, 503, 504)
ARXIV_PDF_DELAY = 5.0                   # base seconds between papers
ARXIV_PDF_DELAY_JITTER = 0.5            # ± fraction of the delay, random
ARXIV_PDF_COOLDOWN_AFTER_FAILS = 3      # consecutive failures before a pause
ARXIV_PDF_COOLDOWN_SECONDS = 90.0       # length of that pause
ARXIV_PDF_TIMEOUT = 90                  # per-request socket timeout

# Fallback hosts for PDF/abs fetches, tried in order when the primary
# host keeps failing.  Empty list = no mirror fallback.
# e.g. ["https://cn.arxiv.org", "https://arxiv.org"]
ARXIV_PDF_FALLBACK_HOSTS = []

# ── arXiv API ──────────────────────────────────────────────────

CATEGORIES = [
    "cs.NI",            # networking — OCS/DCN, RDMA, collective communication
    "cs.DC",            # distributed systems — LLM serving, training systems
    "cs.AR",            # architecture — GPU/NPU, interconnects, near-memory compute
    "cs.PF",            # performance — serving/network measurement
    "cs.LG",            # ML systems — inference/serving papers cross-list here
    "cs.ET",            # emerging tech — optical computing / interconnect
    "physics.optics",   # photonics — OCS devices and optical fabrics
    "physics.app-ph",   # applied physics — photonic integration / CPO
    "eess.SP",          # signal processing — optical transmission / DSP
]

# 对“大而杂”的分类加一个兴趣约束，避免每天先拉 200 篇纯理论/器件论文
# 再用关键词筛选。空字符串 = 不额外约束。
# 语法是 arXiv API 的 search_query（可与 cat: 用 AND 连接）。
CATEGORY_FILTERS = {
    "cs.LG": ('(abs:"LLM" OR abs:"language model" OR abs:"inference" '
              'OR abs:"mixture of experts" OR abs:"serving")'),
    "cs.ET": '(abs:"optical" OR abs:"photonic" OR abs:"interconnect")',
    "physics.optics": ('(abs:"optical network" OR abs:"optical interconnect" '
                       'OR abs:"optical switch" OR abs:"optical datacenter" '
                       'OR abs:"optical circuit" OR abs:"co-packaged optics" '
                       'OR abs:"photonic network")'),
    "physics.app-ph": ('(abs:"optical" OR abs:"photonic" OR abs:"co-packaged" '
                       'OR abs:"interconnect")'),
    "eess.SP": '(abs:"optical" OR abs:"datacenter" OR abs:"data center")',
}

MAX_PER_CATEGORY = 200
API_DELAY = 5.0
RETRY_BACKOFF_BASE = [20.0, 60.0]       # 解析错误退避
RETRY_BACKOFF_503_BASE = [60.0, 120.0]  # 503 服务端故障退避（更长，尊重 arXiv 恢复时间）
MAX_RETRIES = 2
MAX_429_RETRIES = 3                     # --wait 模式下每页 429 冷却重试上限（每次 ~5min）
ARXIV_REQUEST_TIMEOUT = 20              # API/HTML socket timeout (seconds)
ARXIV_BREAKER_AFTER = 3                 # 连续 N 个分类失败后停止打 API，直接走 HTML

# 额外推荐源（都不阻塞主流程；失败只降级，不会生成空 digest）
ENABLE_HF_DAILY = True                  # Hugging Face Daily Papers
HF_DAILY_URL = "https://huggingface.co/api/daily_papers"
HF_DAILY_MAX_PAPERS = 50
HF_DAILY_TIMEOUT = 20

# Semantic Scholar 需要 API key 才稳定（匿名请求经常 429）。
# 设置 SEMANTIC_SCHOLAR_API_KEY 后才会启用 discovery / enrichment。
ENABLE_SEMANTIC_SCHOLAR = True
SEMANTIC_SCHOLAR_API_KEY = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "").strip()
SEMANTIC_SCHOLAR_API = "https://api.semanticscholar.org/graph/v1"
SEMANTIC_SCHOLAR_QUERIES = [
    "optical datacenter network",
    "optical circuit switching",
    "LLM inference serving",
    "mixture of experts inference",
]
SEMANTIC_SCHOLAR_LIMIT = 40
SEMANTIC_SCHOLAR_TIMEOUT = 30

# OpenAlex 是无 key 的 bibliographic 源；在 arXiv API/search 都被限流时，
# 它仍能按日期 + 主题返回标题、完整摘要、作者和 arXiv 链接。
# 免费额度是共享的，所以只发少量查询，失败静默降级。
ENABLE_OPENALEX = True
OPENALEX_API = "https://api.openalex.org/works"
OPENALEX_QUERIES = [
    "optical datacenter network",
    "optical interconnect",
    "LLM inference serving",
    "mixture of experts inference",
]
OPENALEX_LIMIT = 100
OPENALEX_TIMEOUT = 30

# ── 区间回填（--from / --to）───────────────────────────────────
RANGE_WINDOW_DAYS = 3   # 每个拉取窗口的天数（含端点），逐窗口分页拉取
MAX_PAGES = 10          # 每个窗口每分类最多翻页数（2000 篇上限保护）

# ── 自动回看窗口（默认日常模式专用，防漏跑）─────────────────────
# 日常运行除最近 3 天窗口外，自动再拉一个回看窗口，覆盖 arXiv 延迟上架
# 和忘记运行的日子。--date / --from / --to 模式下不启用。
LOOKBACK_FROM_DAYS_AGO = 8   # 回看窗口起点（今天前 N 天）
LOOKBACK_TO_DAYS_AGO = 4     # 回看窗口终点（紧接日常 3 天窗口）

# ── 高分补遗（carry-over）──────────────────────────────────────
# matched 但被 top-N 挤掉的论文记为 pending（shown:false），
# RESURFACE_DAYS 天内若再次被拉到且分数 ≥ RESURFACE_MIN_SCORE，
# 进入 "High-Score Carry-Over" 板块，展示后才永久跳过。
RESURFACE_DAYS = 7        # pending 论文可补遗的天数窗口
RESURFACE_MIN_SCORE = 12  # 补遗分数线（高于日常 MIN_SCORE）
MAX_RESURFACED = 5        # 每日补遗展示上限

# ── 主关键词权重 ────────────────────────────────────────────────

KEYWORDS = {
    # LLM 推理
    "llm": 6,
    "large language model": 5,
    "inference": 7,
    "serving": 7,
    "kv cache": 7,
    "prefill": 7,
    "decode": 7,
    "transformer": 4,
    # MoE / 稀疏专家
    "mixture of experts": 7,
    "mixture-of-experts": 7,
    "sparse mixture": 5,
    "expert parallelism": 5,
    "expert routing": 5,
    "moe": 7,
    "moe model": 5,
    "moe layer": 5,
    # 基础 ML（低权重 catch-all）
    "machine learning": 2,
    "deep learning": 2,
    "neural network": 2,
    "training": 3,
    "fine-tuning": 3,
    "agentic": 7,
    "ai agent": 7,
    # 高性能网络 / 数据中心
    "rdma": 6,
    "datacenter": 7,
    "data center": 7,
    "congestion": 5,
    "tail latency": 5,
    # 调度与资源
    "scheduling": 7,
    "resource allocation": 5,
    "load balancing": 5,
    # GPU / 性能
    "gpu": 4,
    "throughput": 4,
    "latency": 4,
    # 分布式 / 并行（辅助分）
    "distributed": 3,
    "communication": 3,
    "pipeline": 3,
    "parallelism": 3,
    "network": 7,
    "memory": 2,
    # 光互连 / 光网络（系统方向；具体光学短语才计分，避免泛 "optical" 噪声）
    "optical network": 5,
    "optical interconnect": 5,
    "optical datacenter": 6,
    "optical data center": 6,
    "optical circuit switch": 6,
    "optical circuit switching": 6,
    "direct-connect optical": 6,
    "co-packaged optics": 5,
    "silicon photonics": 4,
    # LLM serving 系统 / 推理优化（高优先级 — CacheRoute 类）
    "prefix caching": 8,
    "prefix cache": 7,
    "prefix-aware": 7,
    "prefix affinity": 8,
    "prefix-affinity": 8,
    "kv cache reuse": 7,
    "cache-aware routing": 8,
    "request routing": 6,
    "model routing": 6,
    "llm router": 6,
    "disaggregated serving": 8,
    "disaggregation": 6,
    "pd disaggregation": 7,
    "prefill-decode": 7,
    "continuous batching": 7,
    "speculative decoding": 7,
    "goodput": 6,
    "time to first token": 7,
    "ttft": 6,
    "tpot": 6,
    "time per output token": 6,
    # GPU 集合通信库
    "collective communication": 7,
    "nccl": 5,
    "rccl": 5,
    "all-reduce": 5,
    "allreduce": 5,
    "gpu direct": 5,
    "gpudirect": 5,
}

NEGATIVE_KEYWORDS = {
    # 综述 / 教程 / 观点类：不是“core”贡献，压到主榜之外
    "a survey": -4,
    "survey": -4,
    "survey paper": -4,
    "comprehensive survey": -4,
    "this survey": -8,
    "systematic review": -8,
    "literature review": -6,
    "a review": -4,
    "review paper": -4,
    "this review": -6,
    "an overview": -3,
    "overview paper": -4,
    "position paper": -4,
    "vision paper": -4,
    "research agenda": -4,
    "tutorial": -6,
    "benchmark dataset": -6,
    "comparative analysis": -4,
    "comparative study": -3,
    "decision framework": -3,
}

# ── 核心 / 创新排序（filter.py）────────────────────────────────
# 目标：优先“点明问题 + 给出新系统/算法”的论文，而不是关键词堆满的论文。
TITLE_BOOST = 0.5          # 标题命中额外 +50%（四舍五入，至少 +1）
INNOVATION_BONUS_CAP = 6   # “we propose ... / novel / prototype” 加分封顶
CORE_PICK_MIN = 16         # core_score ≥ 此值 → 标 ★ core pick
CORE_RANK_WEIGHT = 0.5     # 排序分 = score + 0.5 * core_score

INNOVATION_KEYWORDS = {
    # 明确的贡献措辞（摘要里出现）
    "we propose": 3,
    "we present": 2,
    "we introduce": 2,
    "we design": 2,
    "we build": 2,
    "we develop": 2,
    "we demonstrate": 1,
    # 系统 / 实现信号（低权重，避免泛词刷分）
    "novel": 2,
    "architecture": 1,
    "framework": 1,
    "algorithm": 1,
    "prototype": 1,
    "testbed": 1,
    "deployment": 1,
    "open-source": 1,
    "end-to-end": 1,
}

# 同义词族：同一概念只计一次（取族内最高权重），避免 MoE/KV-cache 术语堆分。
MAIN_KEYWORD_FAMILIES = {
    "moe": ["mixture of experts", "mixture-of-experts", "moe", "sparse mixture",
            "moe model", "moe layer", "expert parallelism", "expert routing"],
    "prefix_cache": ["prefix caching", "prefix cache", "prefix-aware",
                     "prefix affinity", "prefix-affinity", "kv cache reuse",
                     "cache-aware routing"],
    "disaggregation": ["disaggregated serving", "disaggregation",
                       "pd disaggregation", "prefill-decode"],
    "collective": ["collective communication", "all-reduce", "allreduce",
                   "nccl", "rccl"],
    "gpudirect": ["gpu direct", "gpudirect"],
    "ttft": ["time to first token", "ttft"],
    "tpot": ["time per output token", "tpot"],
    "routing": ["request routing", "model routing", "llm router"],
    "agents": ["agentic", "ai agent"],
    "optical": ["optical network", "optical interconnect", "optical datacenter",
                "optical data center", "optical circuit switch",
                "optical circuit switching", "direct-connect optical",
                "co-packaged optics", "silicon photonics"],
}

# 标题里必须出现一个“核心领域锚点”，才算 core pick；
# 否则 “Scheduling in Cloud/Fog/Edge” 这类泛调度标题会被误判为核心。
CORE_MAIN_KEYWORDS = {
    "llm", "large language model", "inference", "serving", "kv cache",
    "prefill", "decode", "moe", "mixture of experts", "mixture-of-experts",
    "expert parallelism", "disaggregation", "disaggregated", "rdma",
    "datacenter", "data center", "gpu", "collective communication",
    "prefix cache", "prefix caching", "request routing", "model routing",
    "speculative decoding", "goodput", "ttft", "tpot", "agentic", "ai agent",
    "optical network", "optical interconnect", "optical datacenter",
    "optical data center", "optical circuit switch", "optical circuit switching",
    "direct-connect optical", "co-packaged optics", "silicon photonics",
}

MIN_SCORE = 5
MAX_PAPERS = 15
SUMMARY_MAX_CHARS = 2400   # digest 保留完整摘要（arXiv 摘要通常 < 2k 字符）
DIGEST_MAX_AUTHORS = 8     # 作者太多时显示前 N 位 + et al.

# ── OCS / 光交换 关键词（独立过滤器）────────────────────────────

OCS_KEYWORDS = {
    # 光电路交换 (core OCS)
    "optical circuit switch": 7,
    "optical circuit switching": 7,
    "optical switch": 5,
    "optical switching": 5,
    "photonic switch": 6,
    "photonic switching": 5,
    # 光互连 / 光网络
    "optical interconnect": 5,
    "optical network": 5,
    "optical fabric": 5,
    "photonic interconnect": 5,
    "photonic network": 5,
    "optical data center": 5,
    "optical datacenter": 5,
    "all-optical network": 5,
    "all optical network": 5,
    # 可重构光拓扑
    "reconfigurable optical": 5,
    "optical topology": 5,
    "reconfigurable topology": 5,
    "reconfigurable network": 5,
    "topology engineering": 7,
    "topology reconfiguration": 7,
    "optical reconfiguration": 5,
    # 波分 / MEMS / 交叉连接 / ROADM
    "wavelength division multiplex": 5,
    "optical mems": 5,
    "optical cross-connect": 5,
    "optical cross connect": 5,
    "reconfigurable optical add-drop": 5,
    "roadm": 3,
    # 共封装光学 / 硅光
    "co-packaged optics": 6,
    "co-packaged optical": 6,
    "co packaged optics": 6,
    "optical transceiver": 5,
    "silicon photonic": 4,
    "silicon photonics": 4,
    # 光 I/O
    "optical i/o": 5,
    "optical io": 4,
    # 弹性光网络 / 空分复用
    "elastic optical network": 5,
    "flexgrid": 5,
    "flex grid": 5,
    "space division multiplex": 5,
    "multi-core fiber": 5,
    "multicore fiber": 5,
    "hollow-core fiber": 5,
    "hollow core fiber": 5,
    "anti-resonant fiber": 5,
    # AI/ML 驱动的光网络
    "optical network optimization": 6,
    "optical network automation": 5,
    "digital twin optical": 6,
    "machine learning optical network": 6,
    "reinforcement learning optical": 6,
    "llm optical network": 5,
    "optical network planning": 5,
    # 光网络调度
    "optical scheduling": 6,
    "optical resource allocation": 5,
    # OCS 器件 / 波长选择开关
    "wavelength selective switch": 6,
    "wavelength-selective switch": 6,
    "arrayed waveguide grating": 5,
    "arrayed-waveguide grating": 5,
    "micro-ring resonator": 5,
    "microring resonator": 5,
    "microresonator": 4,
    "optical resonator": 3,
    "mach-zehnder modulator": 5,
    "mach zehnder modulator": 5,
    "mach-zehnder interferometer": 4,
    "optical frequency comb": 4,
    "frequency comb": 3,
    "sagnac": 3,
    # 光交换范式
    "optical burst switching": 6,
    "optical packet switching": 6,
    "optical label switching": 5,
    "optical flow switching": 5,
    "hybrid optical-electrical": 5,
    "optical-electrical switch": 5,
    "optoelectronic switch": 5,
    "free-space optical": 4,
    "free space optical": 4,
    "visible light communication": 3,
    # 数据中心光网络
    "optical data center network": 6,
    "optical datacenter network": 6,
    "optical dcn": 5,
    "intra-datacenter optical": 5,
    "intra-data center optical": 5,
    "intra-datacenter optical": 5,
    # 波分复用 / 网格
    "dense wavelength division": 5,
    "wdm": 3,
    "optical grid": 4,
    "flex-rate": 4,
    "flex rate": 4,
    # 光网络 AI/ML 控制面
    "optical network control": 5,
    "software-defined optical": 5,
    "sdn optical": 4,
    "optical network security": 5,
    "optical network survivability": 5,
    "optical network resilience": 5,
    "quality of transmission": 4,
    "optical performance monitoring": 5,
    # 量子 / 光网络融合
    "quantum optical network": 4,
    "entanglement distribution optical": 5,
    # CPO / 共封装光学生态（含 LPO / NPO / 光引擎）
    "co-packaged": 5,
    "linear pluggable optics": 6,
    "linear-pluggable optics": 6,
    "linear receive optics": 5,
    "near-packaged optics": 5,
    "optical engine": 5,
    "external laser source": 5,
    "elsfp": 5,
    "optical io chiplet": 5,
    "optical i/o chiplet": 5,
    "chiplet optical": 4,
    "fiber attach unit": 4,
}

# 需光通信上下文才生效的缩写（防 "Naive Prompt Optimization" 类误匹配）
OCS_CONTEXT_KEYWORDS = {
    "cpo": 5,
    "lpo": 4,
    "npo": 4,
}
OCS_CONTEXT_REQUIRED = (
    "optical", "optic", "photonic", "transceiver", "packag",
    "interconnect", "fiber", "datacenter", "data center", "switch",
)

OCS_NEGATIVE_KEYWORDS = {
    "a survey": -6,
    "survey paper": -6,
    "comprehensive survey": -8,
    "this survey": -6,
    "tutorial": -6,
    # CV / medical 噪声（完全无关，保留重罚）
    "optical flow": -8,
    "optical coherence tomography": -8,
    "optical character recognition": -8,
    # 传感 / 成像（降低权重 — 光网络论文偶尔会提及，不应一票否决）
    "optical sensor": -4,
    "fiber sensor": -4,
    "optical camera": -4,
    "optical image": -4,
}

# ── OCS 软件 / 硬件分流 ────────────────────────────────────────
# 组里的 OCS 工作以软件为主：控制面、调度、路由、重配置算法优先；
# 纯器件/物理（硅光、WSS、MEMS、调制器、光纤）降权，但不驱逐。
OCS_SOFTWARE_TERMS = {
    "topology engineering", "topology reconfiguration", "reconfigurable topology",
    "reconfigurable network", "optical reconfiguration",
    "optical network optimization", "optical network automation",
    "machine learning optical network", "reinforcement learning optical",
    "llm optical network", "optical network planning", "optical scheduling",
    "optical resource allocation", "digital twin optical",
    "optical network control", "software-defined optical", "sdn optical",
    "optical network security", "optical network survivability",
    "optical network resilience", "quality of transmission",
    "optical performance monitoring",
    # 系统可靠性 / 故障恢复 / 集合通信重映射（OMRC 这类工作）
    "optical datacenter", "optical data center", "direct-connect optical",
    "direct connect optical", "optical interconnect",
    "multipath reliable connection", "circuit remapping",
    "collective remapping", "failure recovery", "optical network reliability",
    "optical network resilience", "optical network survivability",
    "collective communication", "circuit reconfiguration",
}
OCS_HARDWARE_TERMS = {
    "wavelength division multiplex", "dense wavelength division", "wdm",
    "optical mems", "optical cross-connect", "optical cross connect",
    "reconfigurable optical add-drop", "roadm",
    "co-packaged optics", "co-packaged optical", "co packaged optics",
    "co-packaged", "linear pluggable optics", "linear-pluggable optics",
    "linear receive optics", "near-packaged optics", "optical engine",
    "external laser source", "elsfp", "optical io chiplet",
    "optical i/o chiplet", "chiplet optical", "fiber attach unit",
    "optical transceiver", "silicon photonic", "silicon photonics",
    "optical i/o", "optical io", "elastic optical network", "flexgrid",
    "flex grid", "space division multiplex", "multi-core fiber",
    "multicore fiber", "hollow-core fiber", "hollow core fiber",
    "anti-resonant fiber", "wavelength selective switch",
    "wavelength-selective switch", "arrayed waveguide grating",
    "arrayed-waveguide grating", "micro-ring resonator",
    "microring resonator", "microresonator", "optical resonator",
    "mach-zehnder modulator", "mach zehnder modulator",
    "mach-zehnder interferometer", "optical frequency comb",
    "frequency comb", "sagnac", "free-space optical", "free space optical",
    "visible light communication", "optical grid", "flex-rate", "flex rate",
    "quantum optical network", "entanglement distribution optical",
}
OCS_HARDWARE_KEYWORDS = {
    # 器件 / 物理 / 表征：降权并归入 hardware，避免挤占 software spotlight
    "non-volatile": 4, "nonvolatile": 4, "composition-engineered": 4,
    "photonic crystal": 4, "metasurface": 4, "nanophotonic": 4,
    "nanophotonics": 4, "optomechanical": 4, "lithium tantalate": 4,
    "thin-film": 3, "cavity": 3, "interferometer": 3, "spectroscopy": 4,
    "microscopy": 4, "fluorescence": 4, "magnetometry": 4, "sensing": 2,
    "sensor": 2, "imaging": 2, "holographic": 2, "fabrication": 3,
    "epitaxial": 3, "wafer": 3, "photodetector": 4, "laser": 2,
    "modulator": 4, "waveguide": 4, "amplifier": 3, "oscillator": 3,
    "resonator": 3, "quantum": 2, "single-photon": 4, "photon pair": 4,
    "entanglement": 3, "spin": 2, "mems": 4, "packaging": 3,
}

OCS_SOFTWARE_KEYWORDS = {
    # 控制面 / 编排
    "control plane": 6, "data plane": 4, "sdn": 5,
    "software-defined": 6, "orchestration": 6, "controller": 4,
    "network operating system": 6, "intent-based": 5, "api": 2,
    "telemetry": 5, "automation": 5,
    # 调度 / 路由 / 算法
    "scheduling": 5, "routing": 5, "traffic engineering": 6,
    "resource allocation": 4, "topology design": 6,
    "reconfiguration algorithm": 7, "optimization": 3, "planning": 3,
    "algorithm": 4, "provisioning": 4, "failure recovery": 5,
    "fault tolerance": 4, "monitoring": 3,
    # 系统 / 实验
    "prototype": 4, "testbed": 5, "deployment": 4, "simulation": 3,
    "emulation": 4,
    # 可靠性 / 故障 / 重映射（光数据中心系统论文的高信号词）
    "reliability": 6, "reliable": 3, "failure": 5, "failures": 5,
    "failure recovery": 6, "fault tolerance": 5, "resilience": 5,
    "survivability": 4, "remapping": 6, "remap": 5, "circuit remapping": 7,
    "collective remapping": 7, "collectives": 5, "collective": 4,
    "multipath": 4, "reliable connection": 5,
    "direct-connect": 5, "stalled inference": 4, "training throughput": 4,
    "circuit reconfiguration": 5, "optical circuit": 4,
    # ML for optics
    "machine learning": 3, "reinforcement learning": 4,
    "graph neural": 4, "agent": 3,
}

OCS_HARDWARE_WEIGHT = 0.35   # 纯硬件词只计 35%：软件优先，但硬件论文不消失
MAX_OCS_HARDWARE = 2         # OCS spotlight 里纯硬件论文最多占 2 篇

# 通用 software 词（scheduling/routing/optimization/agent…）只有在
# 明确的光网络语境里才计分，避免把普通物理/器件论文拉进 software。
OCS_SOFTWARE_CONTEXT_REQUIRED = (
    "optical circuit switch", "optical circuit switching", "optical switch",
    "optical switching", "optical network", "optical datacenter",
    "optical data center", "optical fabric", "optical interconnect",
    "reconfigurable optical", "optical topology", "topology", "datacenter",
    "data center",
)

OCS_KEYWORD_FAMILIES = {
    "ocs_switch": ["optical circuit switch", "optical circuit switching",
                   "optical switch", "optical switching", "photonic switch",
                   "photonic switching"],
    "ocs_network": ["optical network", "optical data center", "optical datacenter",
                    "optical data center network", "optical datacenter network",
                    "optical dcn", "optical fabric", "optical interconnect",
                    "photonic interconnect", "photonic network",
                    "all-optical network", "all optical network",
                    "intra-datacenter optical", "intra-data center optical"],
    "ocs_cpo": ["co-packaged optics", "co-packaged optical", "co packaged optics",
                "co-packaged"],
    "ocs_comb": ["optical frequency comb", "frequency comb"],
    "ocs_fiber": ["multi-core fiber", "multicore fiber", "hollow-core fiber",
                  "hollow core fiber", "anti-resonant fiber"],
    "ocs_fso": ["free-space optical", "free space optical"],
    "ocs_wdm": ["wavelength division multiplex", "dense wavelength division", "wdm"],
    "ocs_ml": ["optical network optimization", "optical network automation",
               "machine learning optical network", "reinforcement learning optical",
               "llm optical network", "machine learning", "reinforcement learning",
               "graph neural"],
    "ocs_sched": ["optical scheduling", "optical resource allocation", "scheduling",
                  "resource allocation", "planning", "optical network planning"],
    "ocs_control": ["optical network control", "control plane", "controller",
                    "orchestration"],
    "ocs_sdn": ["software-defined optical", "sdn optical", "sdn",
                "software-defined"],
    "ocs_topology": ["topology engineering", "topology reconfiguration",
                     "reconfigurable topology", "reconfigurable network",
                     "optical topology", "topology design"],
    "ocs_reconfig": ["reconfigurable optical", "optical reconfiguration",
                     "reconfiguration algorithm"],
    "ocs_wss": ["wavelength selective switch", "wavelength-selective switch"],
    "ocs_awg": ["arrayed waveguide grating", "arrayed-waveguide grating"],
    "ocs_ring": ["micro-ring resonator", "microring resonator",
                 "microresonator", "optical resonator"],
    "ocs_mzm": ["mach-zehnder modulator", "mach zehnder modulator",
                "mach-zehnder interferometer"],
    "ocs_roadm": ["roadm", "reconfigurable optical add-drop"],
    "ocs_xconn": ["optical cross-connect", "optical cross connect"],
    "ocs_io": ["optical i/o", "optical io"],
    "ocs_silicon": ["silicon photonic", "silicon photonics"],
    "ocs_mems": ["optical mems", "mems"],
    "ocs_laser": ["external laser source", "laser"],
    "ocs_modulator": ["modulator", "mach-zehnder modulator",
                      "mach zehnder modulator", "mach-zehnder interferometer"],
    "ocs_waveguide": ["waveguide", "photonic integrated circuit",
                      "integrated photonics", "photonic chip", "photonic chiplet"],
    "ocs_sensing": ["sensing", "sensor", "imaging", "fluorescence",
                    "spectroscopy", "microscopy"],
    "ocs_quantum": ["quantum", "single-photon", "photon pair", "entanglement",
                    "entanglement distribution optical", "quantum optical network"],
    "ocs_cavity": ["cavity", "resonator", "optical resonator", "microresonator",
                   "micro-ring resonator", "microring resonator"],
    "ocs_fab": ["fabrication", "epitaxial", "wafer", "thin-film",
                "composition-engineered", "non-volatile", "nonvolatile"],
    "ocs_reliability": ["reliability", "reliable", "failure", "failures",
                        "failure recovery", "resilience", "survivability",
                        "fault tolerance", "reliable connection"],
    "ocs_remap": ["remap", "remaps", "remapping", "circuit remapping",
                  "collective remapping", "circuit reconfiguration"],
    "ocs_collective": ["collective", "collectives", "collective communication",
                       "all-reduce", "allreduce"],
    "ocs_optical_dc": ["optical datacenter", "optical data center",
                       "direct-connect optical", "direct connect optical",
                       "optical interconnect", "direct-connect"],
}

OCS_MIN_SCORE = 6
OCS_MIXED_MIN_SCORE = 10    # mixed = 软件/器件信号同时出现，常是误匹配
OCS_HARDWARE_MIN_SCORE = 12  # 纯器件论文要更高分才进 spotlight
MAX_OCS_PAPERS = 10

# 需 LLM serving 上下文才生效的缩写（防 "SLO 图优化" 类误匹配）
MAIN_CONTEXT_KEYWORDS = {
    "slo": 5,
}
MAIN_CONTEXT_REQUIRED = (
    "serving", "inference", "llm", "latency", "throughput",
    "goodput", "gpu", "token", "request",
)

# ── 下载子文件夹路由（classify.py / download_papers.py）─────────
# download_papers.py 按标题+摘要片段+digest 已匹配关键词为每篇论文选子文件夹。
# 打分：词边界匹配（同 filter.py），或该词出现在 **Keywords:** 行中即计分。
#   1. 最高分 < SUBFOLDER_MIN_SCORE → fallback（LLM/misc、OCS/applications）
#   2. 与最高分差距 ≤ SUBFOLDER_TIE_WINDOW 的候选中，按 SUBFOLDER_PRECEDENCE
#      （特异优先）取最先者 — MoE-serving 论文归 moe 而非 inference
#   3. "distributed" 胜出 → 顶层 Distributed/（无 LLM/ 前缀）
#   4. OCS Spotlight 论文一律走 OCS 侧；Main/Carry-Over 论文仅当文本含
#      SUBFOLDER_OCS_SIDE_WORDS 中任一词且 OCS 侧得分更高时改判 OCS 侧
#      （修复 carry-over 光网络论文被误归 LLM/ 的问题）
# 路由决策（子文件夹+证据）追加到 download_log.json 供复查与调参。

SUBFOLDER_MIN_SCORE = 5     # 低于此分 → fallback 子文件夹
SUBFOLDER_TIE_WINDOW = 6    # 特异优先裁决的平局窗口

SUBFOLDER_OCS_SIDE_WORDS = (
    "optical", "photonic", "photonics", "fiber", "fibre",
    "wavelength", "roadm", "mems", "transceiver", "silicon photonic",
)

SUBFOLDER_PRECEDENCE = {
    "LLM": ["moe", "memory", "agents", "distributed", "train", "eval", "inference"],
    "OCS": ["hardware", "topology", "algorithms"],
}

SUBFOLDER_FALLBACK = {"LLM": "LLM/misc", "OCS": "OCS/applications"}

SUBFOLDER_RULES = {
    "LLM": {
        # MoE / 稀疏专家（最特异，优先级最高）
        "moe": {
            "mixture of experts": 7,
            "mixture-of-experts": 7,
            "moe": 7,
            "sparse mixture": 5,
            "sparse expert": 5,
            "expert parallelism": 6,
            "expert routing": 6,
            "expert placement": 5,
            "expert offloading": 5,
            "expert locality": 5,
            "expert capacity": 4,
            "expert": 4,
            "experts": 4,
            "gating network": 3,
            "router": 3,
        },
        # KV cache / 内存管理
        "memory": {
            "kv cache": 7,
            "kv-cache": 7,
            "pagedattention": 7,
            "paged attention": 7,
            "kv cache reuse": 7,
            "prefix caching": 6,
            "prefix cache": 6,
            "kv compression": 5,
            "memory management": 6,
            "memory pool": 6,
            "memory pooling": 6,
            "memory hierarchy": 5,
            "offloading": 5,
            "long-term memory": 5,
            "agent memory": 5,
            "memory": 3,
            "cache": 3,
        },
        # Agentic AI / 智能体
        "agents": {
            "agentic": 7,
            "ai agent": 7,
            "llm agent": 7,
            "multi-agent": 6,
            "multiagent": 6,
            "web agent": 6,
            "tool call": 5,
            "tool calling": 5,
            "tool use": 5,
            "orchestration": 4,
            "trajectory": 3,
            "reflexion": 4,
            "agent": 5,
            "agents": 5,
        },
        # 分布式训练 / 集合通信 → 顶层 Distributed/
        "distributed": {
            "collective communication": 7,
            "nccl": 6,
            "rccl": 6,
            "all-reduce": 6,
            "allreduce": 6,
            "all-gather": 5,
            "allgather": 5,
            "reduce-scatter": 5,
            "collective": 5,
            "rdma": 6,
            "roce": 5,
            "infiniband": 5,
            "congestion control": 5,
            "ecn": 4,
            "distributed training": 6,
            "data parallelism": 5,
            "tensor parallelism": 4,
            "pipeline parallelism": 4,
            "parallelism": 3,
            "cluster scheduling": 5,
            "job scheduling": 5,
            "communication optimization": 4,
            "communication scheduling": 5,
            "kernel bypass": 4,
            "gpudirect": 5,
            "gpu direct": 5,
            "dpdk": 4,
        },
        # 训练 / 微调
        "train": {
            "fine-tuning": 6,
            "fine tuning": 6,
            "fine-tune": 6,
            "finetuning": 6,
            "lora": 5,
            "pre-training": 5,
            "pretraining": 5,
            "training": 4,
            "train": 3,
            "rlhf": 4,
            "dpo": 4,
            "sft": 4,
            "distillation": 5,
            "knowledge distillation": 6,
            "data curation": 4,
            "data mixing": 4,
            "curriculum": 3,
            "gradient": 3,
            "checkpoint": 4,
            "optimizer": 3,
            "scaling law": 4,
        },
        # 评测 / 基准
        "eval": {
            "benchmark": 6,
            "evaluation": 5,
            "evaluate": 5,
            "eval": 5,
            "leaderboard": 5,
            "llm-as-a-judge": 6,
            "llm as a judge": 6,
            "reward model": 5,
            "human evaluation": 5,
            "contamination": 4,
            "metric": 4,
            "test suite": 4,
            "probing": 3,
        },
        # 推理 / serving（最泛化，优先级最低；低权重 catch-all 保证不落 misc）
        "inference": {
            "inference": 6,
            "serving": 6,
            "llm serving": 7,
            "inference serving": 7,
            "decode": 4,
            "decoding": 5,
            "prefill": 6,
            "continuous batching": 7,
            "speculative decoding": 7,
            "disaggregated serving": 8,
            "pd disaggregation": 7,
            "prefill-decode": 6,
            "ttft": 5,
            "time to first token": 6,
            "tpot": 5,
            "batching": 4,
            "scheduling": 4,
            "load balancing": 4,
            "request routing": 4,
            "model routing": 4,
            "goodput": 5,
            "throughput": 4,
            "latency": 4,
            "quantization": 4,
            "attention": 3,
            "kernel": 3,
            "gpu": 3,
            "transformer": 3,
            "token": 3,
            "llm": 3,
            "large language model": 3,
        },
    },
    "OCS": {
        # 器件 / 物理：硅光、MEMS、WSS、调制器、光纤传输实验
        "hardware": {
            "silicon photonics": 6,
            "silicon photonic": 6,
            "photonic integrated circuit": 6,
            "integrated photonics": 5,
            "photonic chip": 5,
            "photonic chiplet": 6,
            "mems": 5,
            "optical mems": 6,
            "wavelength selective switch": 6,
            "wavelength-selective switch": 6,
            "micro-ring resonator": 6,
            "microring resonator": 6,
            "microresonator": 5,
            "mach-zehnder": 5,
            "modulator": 5,
            "vcsel": 6,
            "semiconductor optical amplifier": 6,
            "external laser source": 5,
            "laser": 3,
            "waveguide": 5,
            "co-packaged optics": 5,
            "co-packaged": 5,
            "optical engine": 5,
            "transceiver": 4,
            "photodetector": 5,
            "hollow-core fiber": 5,
            "hollow core fiber": 5,
            "optical fiber": 4,
            "fiber transmission": 5,
            "fiber": 3,
            "transmission": 4,
            "optical frequency comb": 5,
            "frequency comb": 4,
            "injection locking": 4,
            "beam steering": 4,
            "epitaxial": 4,
            "iii-v": 4,
            "fabrication": 3,
            "experimental demonstration": 4,
            "insertion loss": 4,
            "crosstalk": 4,
            "photonic": 3,
            "optical": 2,
        },
        # 拓扑 / 架构：OCS fabric、可重构 DCN、光电混合交换
        "topology": {
            "optical circuit switch": 7,
            "optical circuit switching": 7,
            "ocs": 6,
            "topology engineering": 6,
            "topology reconfiguration": 6,
            "reconfigurable topology": 6,
            "optical topology": 6,
            "reconfigurable optical": 5,
            "reconfigurable data center": 6,
            "reconfigurable network": 5,
            "hybrid electrical-optical": 5,
            "hybrid optical-electrical": 5,
            "optical switch": 5,
            "optical switching": 5,
            "photonic switch": 6,
            "optical fabric": 5,
            "switch architecture": 5,
            "network topology": 5,
            "data center network": 4,
            "optical interconnect": 4,
            "photonic interconnect": 4,
            "scale-up": 4,
            "interconnect": 3,
            "topology": 5,
            "reconfigurable": 4,
            "fabric": 3,
        },
        # 算法 / 控制：路由、调度、RL 优化、QoT、数字孪生
        "algorithms": {
            "routing": 5,
            "routing and wavelength": 6,
            "traffic engineering": 5,
            "traffic grooming": 6,
            "spectrum allocation": 5,
            "resource allocation": 4,
            "scheduling": 4,
            "optimization": 4,
            "reinforcement learning": 5,
            "machine learning": 4,
            "graph neural": 4,
            "birkhoff": 6,
            "matching": 4,
            "algorithm": 4,
            "heuristic": 3,
            "digital twin": 5,
            "control plane": 4,
            "sdn": 4,
            "software-defined": 4,
            "quality of transmission": 5,
            "provisioning": 4,
            "survivability": 4,
            "monitoring": 3,
            "planning": 3,
        },
        # applications = fallback（OCS/applications），无规则
    },
}

# ── 路径 ───────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_FILE = SCRIPT_DIR / "seen_papers.json"
DIGEST_STATE_FILE = SCRIPT_DIR / "digest_papers.json"
OUTPUT_FILE = SCRIPT_DIR / "daily_digest.md"
DOWNLOAD_LOG = SCRIPT_DIR / "download_log.json"
FETCH_REPORT_FILE = SCRIPT_DIR / "fetch_report.json"
NO_KEYWORD_FILE = SCRIPT_DIR / "no_keyword_papers.json"
