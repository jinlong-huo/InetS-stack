#!/usr/bin/env python3
"""Catch-up 机制评估测试 — 模拟「每天手动运行，但偶尔会忘记」的场景。

场景:
  Day 1 (2026-08-01, 繁忙日): 16 篇同分高分论文竞争 top-15
      → 15 篇上 digest (shown=True)，第 16 篇 + 5 篇低分达线论文被挤下
      → 记入 pending (shown=False)
  Day 2-4: 忘记运行（无任何操作）
  Day 5 (2026-08-05): 日常窗口 + 自动回看窗口重新拉到这 6 篇 pending 论文
      → 分数 ≥ RESURFACE_MIN_SCORE 的进入 High-Score Carry-Over 板块
      → 低分 pending 继续跳过；legacy 条目（无 shown 字段）按已展示处理；
        超过 RESURFACE_DAYS 天的 pending 不再补遗

运行: python3 arxiv_digest/test_catchup.py   （或 make test）
"""

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from arxiv_digest import config
from arxiv_digest import digest
from arxiv_digest import filter as flt
from arxiv_digest.Arxiv_filter import evaluate_entries

CHECKS = 0


def check(cond, label):
    global CHECKS
    CHECKS += 1
    if not cond:
        raise AssertionError(f"FAIL: {label}")
    print(f"  ✓ {label}")


def make_entry(arxiv_id, title, summary):
    """模拟 feedparser entry（evaluate_entries 所需的全部字段）。"""
    return SimpleNamespace(
        id=f"http://arxiv.org/abs/{arxiv_id}",
        title=title,
        summary=summary,
        link=f"https://arxiv.org/abs/{arxiv_id}",
        author="Test Author",
        published_parsed=None,
    )


# ── 论文文本（分数由 filter 的加权/去重规则决定，不写死具体分值）─────
# 高分：覆盖 llm / serving / kv cache / prefill
RICH_TITLE = "KV Cache Management for LLM Serving"
RICH_SUMMARY = "A system for llm serving with kv cache and prefill."
# 低分：仅 gpu + memory — 达线但低于补遗线
THIN_TITLE = "GPU Memory Notes"
THIN_SUMMARY = "A short note on gpu memory."
# 0 分: 零关键词
ZERO_TITLE = "Biology of Cell Membranes"
ZERO_SUMMARY = "A study of cell membranes and proteins."

DAY1 = "2026-08-01"
DAY5 = "2026-08-05"

RICH_IDS = [f"2608.000{i:02d}v1" for i in range(1, 16)]   # 15 篇高分（占满 top-15）
CUT_ID = "2608.00100v1"        # 第 16 篇同分高分 → 被挤下 → pending
THIN_IDS = [f"2608.002{i:02d}v1" for i in range(1, 6)]    # 5 篇低分达线 → pending
LEGACY_ID = "2608.00900v1"     # 历史条目（无 shown 字段）
AGED_ID = "2608.00901v1"       # 8 天前的 pending → 超出补遗窗口
NEW_ID = "2608.00300v1"        # Day 5 新论文


def run_pipeline(entries, today, seen, digest_seen):
    """模拟 main() 的 Stage 2-4：评估 → top-N → 盖章 → 生成 markdown。"""
    stats = {cat: {"total": len(entries), "skipped": 0, "already_seen": 0,
                   "selected": 0, "ocs_selected": 0}
             for cat in config.CATEGORIES}
    result = evaluate_entries([("cs.NI", entries)], today, seen, digest_seen,
                              stats)
    selected, total_main = digest.select_top(result["selected"],
                                             config.MAX_PAPERS)
    ocs_selected, total_ocs = digest.select_top(result["ocs_selected"],
                                                config.MAX_OCS_PAPERS)
    carry_over, _ = digest.select_top(result["carry_over"],
                                      config.MAX_RESURFACED)
    digest.finalize_stamps(result["new_digest_seen"], selected, ocs_selected,
                           carry_over, today)
    digest.generate_markdown(selected, ocs_selected, total_main, total_ocs,
                             carry_over)
    return result, selected, carry_over


def main():
    tmp = Path(tempfile.mkdtemp(prefix="arxiv_catchup_test_"))
    config.OUTPUT_FILE = tmp / "daily_digest.md"
    config.STATE_FILE = tmp / "seen_papers.json"
    config.DIGEST_STATE_FILE = tmp / "digest_papers.json"
    print(f"[setup] temp state dir: {tmp}\n")

    # ── Day 1: 繁忙日，21 篇达线论文竞争 top-15 ──────────────
    print(f"Day 1 ({DAY1}): busy day — 16 rich (27 分) + 5 thin (6 分)")
    day1_entries = (
        [make_entry(i, RICH_TITLE, RICH_SUMMARY) for i in RICH_IDS]
        + [make_entry(CUT_ID, RICH_TITLE, RICH_SUMMARY)]
        + [make_entry(i, THIN_TITLE, THIN_SUMMARY) for i in THIN_IDS]
    )
    r1, sel1, carry1 = run_pipeline(day1_entries, DAY1, {}, {})
    ds1 = r1["new_digest_seen"]

    check(len(sel1) == 15, "top-15 selected on busy day")
    check(len(carry1) == 0, "no carry-over on first run")
    check(all(ds1[i]["shown"] is True for i in RICH_IDS),
          "15 shown papers stamped shown=True")
    check(ds1[CUT_ID]["shown"] is False and ds1[CUT_ID]["score"] >= 12,
          "cut high-scorer is pending (shown=False) with score recorded")
    check(all(ds1[i]["shown"] is False for i in THIN_IDS),
          "5 thin papers pending (shown=False)")

    # ── Day 2-4: 忘记运行（无操作）──────────────────────────
    print("Day 2-4: forgotten (no runs)")

    # ── Day 5: 日常 + 回看窗口重新拉到 pending 论文 ──────────
    print(f"Day 5 ({DAY5}): catch-up run via lookback window")
    digest_seen_day5 = dict(ds1)
    # 历史 legacy 条目：旧格式（无 shown 字段）
    digest_seen_day5[LEGACY_ID] = {
        "title": RICH_TITLE, "keywords": [], "ocs_keywords": [],
        "date": DAY1,
    }
    # 8 天前的 pending（超出 RESURFACE_DAYS=7 窗口）
    digest_seen_day5[AGED_ID] = {
        "title": RICH_TITLE, "keywords": [], "ocs_keywords": [],
        "date": "2026-07-28", "shown": False, "score": 27,
    }

    day5_entries = (
        [make_entry(CUT_ID, RICH_TITLE, RICH_SUMMARY)]
        + [make_entry(i, THIN_TITLE, THIN_SUMMARY) for i in THIN_IDS]
        + [make_entry(LEGACY_ID, RICH_TITLE, RICH_SUMMARY)]
        + [make_entry(AGED_ID, RICH_TITLE, RICH_SUMMARY)]
        + [make_entry(NEW_ID, RICH_TITLE, RICH_SUMMARY)]
        + [make_entry("2608.00400v1", ZERO_TITLE, ZERO_SUMMARY)]
    )
    r5, sel5, carry5 = run_pipeline(day5_entries, DAY5,
                                    r1["new_seen"], digest_seen_day5)
    ds5 = r5["new_digest_seen"]

    check(len(carry5) == 1 and carry5[0]["first_seen"] == DAY1,
          "cut high-scorer resurfaced in carry-over with original date")
    check(r5["counts"]["already_seen"] == 7,
          "5 thin + 1 legacy + 1 aged pending all skipped (already_seen=7)")
    check(ds5[CUT_ID]["shown"] is True,
          "carry-over paper stamped shown=True after being shown")
    check(all(ds5[i]["shown"] is False for i in THIN_IDS),
          "thin papers remain pending (below carry-over bar)")
    check(any(p["link"].endswith(NEW_ID) for p in sel5)
          and ds5[NEW_ID]["shown"] is True,
          "new paper selected normally and stamped shown=True")
    check(len(r5["no_kw_papers"]) == 1, "zero-keyword paper recorded")

    # ── Markdown 输出 ───────────────────────────────────────
    md = config.OUTPUT_FILE.read_text(encoding="utf-8")
    check("## High-Score Carry-Over (missed earlier this week)" in md,
          "markdown has carry-over section")
    check(f"**First seen:** {DAY1}" in md,
          "carry-over entry shows original first-seen date")
    check(not digest._is_empty(md), "digest with carry-over is not empty")

    # ── 状态持久化 + legacy 迁移 ─────────────────────────────
    digest.save_digest_seen(ds5)
    loaded = digest.load_digest_seen()
    check(loaded[CUT_ID]["shown"] is True, "state round-trips through JSON")

    legacy_only = {"2608.00001v1": {"title": "t", "keywords": [],
                                    "ocs_keywords": [], "date": DAY1}}
    config.DIGEST_STATE_FILE.write_text(json.dumps(legacy_only))
    migrated = digest.load_digest_seen()
    check(migrated["2608.00001v1"]["shown"] is True,
          "legacy entries (no shown field) migrate to shown=True")

    # ── 缩写误匹配回归（context-gated acronyms）────────────────
    print("Acronym false-positive regression:")
    npo_s, _ = flt.score_ocs(
        "Naive Prompt Optimization: Rethinking the Need for Complex Prompt Search",
        "Efficiently improving autonomous agents, with prompt optimization "
        "emerging as a promising approach.")
    check(npo_s == 0, "'npo' does not match 'Naive Prompt Optimization'")
    cpo_s, _ = flt.score_ocs(
        "Co-Packaged Optics for AI Datacenter Interconnects",
        "We demonstrate a CPO optical engine with external laser source "
        "for datacenter optical interconnect.")
    check(cpo_s >= 3, "real CPO paper still matches via optical context")
    slo_s, _, _ = flt.score_main("SLO-aware Graph Layout",
                                 "We optimize the SLO decomposition of graphs.")
    check(slo_s == 0, "'slo' does not match without serving context")

    print(f"\nAll {CHECKS} checks passed ✓")
    print("结论: 忘记运行的日子里，高分被挤论文会在下次运行时")
    print("      通过回看窗口 + Carry-Over 板块自动补遗。")


if __name__ == "__main__":
    main()
