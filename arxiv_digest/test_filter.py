#!/usr/bin/env python3
"""filter.py 打分质量测试（纯离线，无网络）。

覆盖：
  - 同义词族去重：MoE / KV-cache 术语不能反复堆分
  - 标题权重：标题命中 > 摘要命中
  - 综述降权
  - “we propose / novel”贡献加分
  - OCS software / hardware 分流：软件论文优先，硬件词降权
  - OCS 纯硬件配额：select_ocs 最多保留 MAX_OCS_HARDWARE 篇

运行: python3 arxiv_digest/test_filter.py   （或 make test）
"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from arxiv_digest import config
from arxiv_digest import digest
from arxiv_digest import filter as flt

CHECKS = 0


def check(cond, label):
    global CHECKS
    CHECKS += 1
    if not cond:
        raise AssertionError(f"FAIL: {label}")
    print(f"  ✓ {label}")


print("─" * 60)
print("  filter.py — core / innovation / software-first cases")
print("─" * 60)

# ── 同义词族去重 ──────────────────────────────────────────────
moe_family = set(config.MAIN_KEYWORD_FAMILIES["moe"])
d = flt.score_main_detail(
    "MoE Serving with Mixture-of-Experts and Expert Parallelism",
    "We serve mixture of experts models with moe layers, sparse mixture "
    "routing and expert parallelism.",
)
moe_hits = [kw for kw in d["matched"] if kw in moe_family]
check(len(moe_hits) <= 1, f"MoE family counted once (got {moe_hits})")
check(d["score"] < 60, "MoE synonym stacking does not inflate the score")

# ── 标题权重 ──────────────────────────────────────────────────
title_hit = flt.score_main_detail("KV Cache Serving for LLM Inference", "")
summary_hit = flt.score_main_detail("Unrelated Systems Paper",
                                    "We mention kv cache serving for llm inference.")
check(title_hit["base_score"] > summary_hit["base_score"],
      "title hit outranks the same terms in the abstract")
check(title_hit["core_score"] > summary_hit["core_score"],
      "title hit produces a stronger core signal")

# ── 综述降权 ──────────────────────────────────────────────────
paper = flt.score_main_detail(
    "KV Cache Serving for LLM Inference",
    "We propose a system for llm serving with kv cache and prefill.",
)
survey = flt.score_main_detail(
    "A Survey of KV Cache Serving for LLM Inference",
    "We propose a system for llm serving with kv cache and prefill.",
)
check(survey["score"] < paper["score"], "survey penalty lowers the score")
check(survey["core_score"] < paper["core_score"], "survey lowers the core score")

# ── 贡献措辞加分 ──────────────────────────────────────────────
plain = flt.score_main_detail("KV Cache Serving for LLM Inference",
                              "A system for llm serving with kv cache.")
proposed = flt.score_main_detail(
    "KV Cache Serving for LLM Inference",
    "We propose a novel system for llm serving with kv cache and a prototype.",
)
check(proposed["innovation_bonus"] > 0, "contribution wording gets a bonus")
check(proposed["score"] > plain["score"], "contribution bonus raises the score")
check(proposed["innovation_bonus"] <= config.INNOVATION_BONUS_CAP,
      "contribution bonus is capped")

# ── OCS software vs hardware ──────────────────────────────────
software = flt.score_ocs_detail(
    "Scheduling Optical Circuit Switches for LLM Training",
    "We design a control-plane scheduler for reconfigurable optical "
    "datacenter fabrics, with topology reconfiguration and traffic engineering.",
)
hardware = flt.score_ocs_detail(
    "Silicon Photonics Microring Modulator with Low Insertion Loss",
    "We fabricate a photonic integrated circuit with a microring resonator "
    "modulator, measuring insertion loss and crosstalk.",
)
check(software["focus"] in ("software", "mixed"),
      f"software OCS paper classified software (got {software['focus']})")
check(hardware["focus"] == "hardware",
      f"device OCS paper classified hardware (got {hardware['focus']})")
check(software["score"] > hardware["score"],
      "software OCS paper outranks a device paper")

# 纯硬件论文不能凭空吃 software 词（无 OCS 词汇）
noise = flt.score_ocs_detail(
    "Naive Prompt Optimization: Rethinking the Complex Prompt Search",
    "We use an agent and scheduling to optimize prompts for autonomous agents.",
)
check(noise["score"] == 0, "generic software words do not create an OCS match")

# ── OCS 硬件配额 ──────────────────────────────────────────────
hardware_papers = [
    {"title": f"Device {i}", "score": 9 - i, "rank_score": 9 - i,
     "focus": "hardware", "matched": []}
    for i in range(5)
]
software_papers = [
    {"title": f"Software {i}", "score": 5 - i, "rank_score": 5 - i,
     "focus": "software", "matched": []}
    for i in range(2)
]
picked, total = digest.select_ocs(
    software_papers + hardware_papers, max_n=5,
    max_hardware=config.MAX_OCS_HARDWARE,
)
check(total == 7, "select_ocs reports the total candidate count")
check(sum(1 for p in picked if p["focus"] == "hardware")
      <= config.MAX_OCS_HARDWARE,
      "hardware-only papers respect MAX_OCS_HARDWARE")
check(any(p["focus"] == "software" for p in picked),
      "software papers are selected before deferred hardware")

# ── OMRC regression（Oct 2026, Cornell）──────────────────────
# 这篇论文被旧规则标成 neutral optics，OCS 分数低于 neutral 门槛，
# 于是虽然抓到了，却既没进 main，也没进 spotlight。
omrc_title = ("OMRC: Multipath Reliability for Direct-Connect "
              "Optical Interconnects")
omrc_summary = (
    "Network failures degrade Multipath Reliable Connection (MRC) in "
    "direct-connect optical datacenters. We propose Optical MRC (OMRC), "
    "which remaps collectives and circuits, preserving 95.7% of training "
    "throughput, restoring stalled inference, and achieving 4.7x MRC's "
    "simulated bandwidth.")
omrc_main = flt.score_main_detail(omrc_title, omrc_summary)
omrc_ocs = flt.score_ocs_detail(omrc_title, omrc_summary)
check(omrc_main["score"] >= config.MIN_SCORE,
      "OMRC passes the main digest threshold")
check(omrc_main["core_score"] > 0,
      "OMRC gets a core signal from the optical title")
check(omrc_ocs["focus"] == "software",
      f"OMRC is classified software-first (got {omrc_ocs['focus']})")
check(omrc_ocs["score"] >= config.OCS_MIN_SCORE,
      "OMRC clears the OCS spotlight threshold")
check(any(k in omrc_ocs["matched"]
          for k in ("reliability", "remap", "collectives")),
      "OMRC matches reliability / remapping / collective terms")

# OpenAlex 偶尔会把长 DOI 误解析成 arXiv ID，digest 里不能出现死链。
check(flt.valid_arxiv_id("2610.30551v1"), "valid arXiv id accepted")
check(not flt.valid_arxiv_id("2026.9150015"),
      "malformed long arXiv id rejected")

# ── Oct 13 regression: optical KV-cache network ───────────────
# 真正“10 月 13 日的光学论文”：arXiv 2610.18444。
# 旧流程在 API 限流时把抓取失败当成 0 条，导致它根本没进入打分。
kv_title = "An Optical KV Cache Network for Disaggregated LLM Serving"
kv_summary = (
    "In disaggregated LLM serving, each request's KV cache moves from a "
    "prefill GPU to a decode GPU. An optical circuit switch (OCS) can give "
    "every transfer a full-rate circuit, but a mechanical OCS needs tens of "
    "milliseconds to reconfigure. We hide this delay inside the prefill with "
    "a reservation scheduler that commits each circuit while prefill is still "
    "computing. Each GPU reaches the OCS through wavelength-multiplexed (WDM) "
    "optics. Simulations on real serving traces match 400G packet fabrics at "
    "0.55 to 0.78 times the cost.")
kv_main = flt.score_main_detail(kv_title, kv_summary)
kv_ocs = flt.score_ocs_detail(kv_title, kv_summary)
check(kv_main["score"] >= config.MIN_SCORE,
      "Oct 13 optical KV-cache paper passes the main threshold")
check(kv_ocs["focus"] in ("software", "mixed"),
      f"Oct 13 paper is an OCS paper (got {kv_ocs['focus']})")
check(kv_ocs["score"] >= config.OCS_MIXED_MIN_SCORE,
      "Oct 13 paper clears the mixed OCS threshold")
check("optical circuit switch" in kv_ocs["matched"],
      "Oct 13 paper matches the OCS concept explicitly")

# 10 篇高分器件论文不能把 OMRC 挤出 spotlight
omrc_candidate = {
    "title": omrc_title, "score": omrc_ocs["score"],
    "rank_score": omrc_ocs["rank_score"], "focus": "software",
    "matched": omrc_ocs["matched"],
}
high_hardware = [
    {"title": f"Device {i}", "score": 99, "rank_score": 99,
     "focus": "hardware", "matched": []}
    for i in range(10)
]
picked, _ = digest.select_ocs(high_hardware + [omrc_candidate],
                              max_n=5, max_hardware=config.MAX_OCS_HARDWARE)
check(picked and picked[0]["title"] == omrc_title,
      "software-first selection puts OMRC ahead of high-score hardware")
check(sum(1 for p in picked if p["focus"] == "hardware")
      <= config.MAX_OCS_HARDWARE,
      "hardware quota still holds with OMRC present")

print("─" * 60)
print(f"  All {CHECKS} checks passed.")
print("─" * 60)
