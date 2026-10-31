#!/usr/bin/env python3
"""
ArXiv Daily Digest — 主入口。

Pipeline:  fetch → filter → select → digest
模块:       fetch    filter    digest   digest

Usage:
    python3 arxiv_digest/Arxiv_filter.py                日常运行（抓取 + 生成 digest）
    python3 arxiv_digest/Arxiv_filter.py --wait          遇到 429 限流时等待 5 分钟后自动重试
    python3 arxiv_digest/Arxiv_filter.py --date YYYY-MM-DD  回填指定日期（精确窗口；
                                                          HTML 回退会自动多看 1 天时区差）
    python3 arxiv_digest/Arxiv_filter.py --from YYYY-MM-DD [--to YYYY-MM-DD]
                                                          补拉一段时间内的论文（长时间未运行后用）
    python3 arxiv_digest/Arxiv_filter.py --ignore-seen   忽略 digest 历史重新打分
    python3 arxiv_digest/Arxiv_filter.py --no-extra-sources
                                                          只用 arXiv，跳过 HF / OpenAlex / S2

数据源：arXiv API → （失败时）arxiv.org advanced search HTML → HF Daily Papers
→ OpenAlex（无 key 的 bibliographic 回退）→ Semantic Scholar（有 key 时）。
每个源的失败都写入 arxiv_digest/fetch_report.json；全部失败时退出码为 2，
不会生成一个看起来正常的空 digest。
"""

import json
import sys
from datetime import datetime
from pathlib import Path

# Ensure the project root is on sys.path so `arxiv_digest` is importable
# when this script is run directly (python3 arxiv_digest/Arxiv_filter.py).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from arxiv_digest import config
from arxiv_digest import fetch
from arxiv_digest import filter as flt
from arxiv_digest import digest
from arxiv_digest import extra_sources


def _arg_value(flag):
    """取 --flag 之后的参数；flag 未出现返回 None，出现但缺值抛 IndexError。"""
    if flag not in sys.argv:
        return None
    idx = sys.argv.index(flag)
    if idx + 1 >= len(sys.argv):
        raise IndexError(f"{flag} requires a value: {flag} YYYY-MM-DD")
    return sys.argv[idx + 1]


def _parse_range_args():
    """解析 --from / --to，返回 (date_from, date_to) 或 (None, None)。

    格式错误 / --from 晚于 --to 时打印错误并返回哨兵 (False, False)。
    """
    if "--from" not in sys.argv and "--to" not in sys.argv:
        return None, None

    from datetime import datetime as _dt
    try:
        range_from = _arg_value("--from")
        range_to = _arg_value(flag="--to")
    except IndexError as ex:
        print(f"[error] {ex}")
        return False, False

    for name, val in (("--from", range_from), ("--to", range_to)):
        if val is None:
            continue
        try:
            _dt.strptime(val, "%Y-%m-%d")
        except ValueError:
            print(f"[error] Invalid date format: {val}. Use YYYY-MM-DD.")
            return False, False

    if range_from and range_to and range_from > range_to:
        print("[error] --from must be on or before --to.")
        return False, False

    print(f"[backfill] Range: {range_from or '(unbounded)'} → {range_to or 'today'}")
    if range_to:
        # digest 标题 / 台账日期以区间终点为准
        config.DATE_OVERRIDE = range_to
    return range_from, range_to


def _entry_fields(entry):
    """Extract the fields the digest needs, independent of the source."""
    authors = flt.extract_authors(entry)
    summary = (getattr(entry, "summary", "") or "").strip()
    link = getattr(entry, "link", "") or ""
    pdf_link = link.replace("/abs/", "/pdf/") if "arxiv.org/abs/" in link else ""
    first_author, year = flt.extract_author_year(entry)
    return {
        "title": getattr(entry, "title", "") or "",
        "authors": authors,
        "first_author": first_author,
        "year": year,
        "summary": summary,
        "link": link,
        "pdf_link": pdf_link,
        "primary_category": flt.extract_primary_category(entry),
        "published": flt.extract_published(entry),
        "source": getattr(entry, "source", "arxiv"),
        "arxiv_comment": getattr(entry, "arxiv_comment", "") or "",
        "hf_upvotes": getattr(entry, "hf_upvotes", None),
        "hf_url": getattr(entry, "hf_url", "") or "",
        "hf_ai_summary": getattr(entry, "hf_ai_summary", "") or "",
        "citation_count": getattr(entry, "s2_citations", None),
        "influential_citations": getattr(entry, "s2_influential", None),
        "venue": getattr(entry, "s2_venue", "") or "",
        "s2_tldr": getattr(entry, "s2_tldr", "") or "",
    }


def _carry_over_eligible(prev, score, today):
    """pending（matched 但被 top-N 挤掉）论文是否达到补遗条件：
    本次分数 ≥ RESURFACE_MIN_SCORE 且首次 matched 日期在 RESURFACE_DAYS 天内。"""
    if score < config.RESURFACE_MIN_SCORE:
        return False
    from datetime import datetime as _dt
    try:
        stamp = _dt.strptime(prev.get("date", ""), "%Y-%m-%d").date()
        now = _dt.strptime(today, "%Y-%m-%d").date()
    except ValueError:
        return False
    return 0 <= (now - stamp).days <= config.RESURFACE_DAYS


def evaluate_entries(entries_by_cat, today, seen, digest_seen, stats,
                     ignore_seen=False):
    """Filter + score + gate 全部拉取结果（纯函数式，无网络 — 可测试）。

    返回 dict:
      selected / ocs_selected / carry_over — 候选列表（top-N 之前）
      no_kw_papers / near_misses           — 诊断用
      new_seen / new_digest_seen           — 更新后的状态（matched 未选中者
                                             记为 pending: shown=False）
      counts                               — {skipped, already_seen}
      stats                                — 就地更新各分类计数
    """
    selected = []
    ocs_selected = []
    carry_over = []
    no_kw_papers = []          # 零关键词论文 — 供人工复查
    near_misses = []
    counts = {"skipped": 0, "already_seen": 0}
    seen_this_run = set()
    new_seen = dict(seen)
    new_digest_seen = dict(digest_seen)

    for cat, entries in entries_by_cat:
        feed_selected = 0
        feed_ocs_selected = 0
        feed_skipped = 0
        feed_already_seen = 0

        for entry in entries:
            paper_id = flt.normalize_arxiv_id(entry.id)
            run_key = flt.base_arxiv_id(entry.id)
            source = getattr(entry, "source", "arxiv")

            # 拒绝 OpenAlex/HTML 解析出的畸形 arXiv ID（例如 2026.9150015），
            # 否则 digest 里会出现打不开的链接。S2 的非 arXiv 条目用 s2: id，
            # 不受此限制。
            if (source != "semantic-scholar"
                    and "arxiv.org" in (getattr(entry, "link", "") or "")
                    and not flt.valid_arxiv_id(entry.id)):
                feed_skipped += 1
                counts["skipped"] += 1
                continue

            # 跨 feed / 跨源去重（同次运行内；忽略 v1/v2 差异）
            if run_key in seen_this_run:
                feed_skipped += 1
                counts["skipped"] += 1
                continue
            seen_this_run.add(run_key)

            prev = digest_seen.get(paper_id) or digest_seen.get(run_key)
            # 旧格式条目无 shown 字段 → 视为已上过 digest
            prev_shown = prev is None or prev.get("shown", True)
            prev_date = prev.get("date", "") if prev else ""

            title = entry.title
            summary = entry.summary
            link = entry.link
            fields = _entry_fields(entry)

            # ── 主过滤器（含 core / innovation 信号）──
            main = flt.score_main_detail(title, summary)
            score = main["score"]
            max_single = main["max_single"]
            matched = main["matched"]

            # ── OCS 过滤器（software / hardware 分流）──
            ocs = flt.score_ocs_detail(title, summary)
            ocs_score = ocs["score"]
            ocs_matched = ocs["matched"]

            # ── 跨天去重 / 补遗门 ──
            if not ignore_seen and prev is not None and prev_date != today:
                if prev_shown:
                    # 已实际展示过 → 永久跳过
                    feed_already_seen += 1
                    counts["already_seen"] += 1
                    continue
                # pending：高分 → carry-over 补遗；低分 → 继续跳过（保留 pending）
                if _carry_over_eligible(prev, score, today):
                    carry_over.append({
                        **fields,
                        "title": title, "score": score,
                        "max_single": max_single, "matched": matched,
                        "matched_detail": main["matched_detail"],
                        "core_score": main["core_score"],
                        "rank_score": main["rank_score"],
                        "core_pick": main["core_pick"],
                        "innovation_matched": main["innovation_matched"],
                        "first_seen": prev_date,
                    })
                else:
                    feed_already_seen += 1
                    counts["already_seen"] += 1
                # 刷新 pending 记录（保留首次 matched 日期，更新分数/关键词）
                new_digest_seen[paper_id] = {
                    **prev, "title": title,
                    "keywords": matched[:3],
                    "ocs_keywords": ocs_matched[:3],
                    "score": score,
                    "core_score": main["core_score"],
                    "rank_score": main["rank_score"],
                    "core_pick": main["core_pick"],
                    "ocs_focus": ocs["focus"],
                    "ocs_software": ocs["software_score"],
                    "ocs_hardware": ocs["hardware_score"],
                }
                continue

            # ── 正常打分路径（新论文 / 今日已见过的重跑）──

            # 两次过滤器均未命中 → 零关键词，单独记录供复查
            if score == 0 and ocs_score == 0:
                no_kw_papers.append({
                    **fields,
                    "summary": summary[:500],
                })

            if score >= config.MIN_SCORE:
                selected.append({
                    **fields,
                    "title": title, "score": score,
                    "max_single": max_single, "matched": matched,
                    "matched_detail": main["matched_detail"],
                    "core_score": main["core_score"],
                    "title_score": main["title_score"],
                    "innovation_bonus": main["innovation_bonus"],
                    "innovation_matched": main["innovation_matched"],
                    "rank_score": main["rank_score"],
                    "core_pick": main["core_pick"],
                })
                feed_selected += 1
            elif score >= config.MIN_SCORE - 2:
                near_misses.append({
                    "title": title, "score": score,
                    "max_single": max_single, "matched": matched,
                    "core_score": main["core_score"],
                    "rank_score": main["rank_score"],
                    "link": link,
                })

            if ocs["focus"] == "hardware":
                ocs_min = config.OCS_HARDWARE_MIN_SCORE
            elif ocs["focus"] == "mixed":
                ocs_min = config.OCS_MIXED_MIN_SCORE
            else:
                ocs_min = config.OCS_MIN_SCORE
            if ocs_score >= ocs_min:
                ocs_selected.append({
                    **fields,
                    "title": title, "score": ocs_score,
                    "matched": ocs_matched,
                    "matched_detail": ocs["matched_detail"],
                    "software_score": ocs["software_score"],
                    "hardware_score": ocs["hardware_score"],
                    "focus": ocs["focus"],
                    "rank_score": ocs["rank_score"],
                })
                feed_ocs_selected += 1

            # 流水账
            new_seen[paper_id] = {
                "title": title,
                "keywords": matched[:3],
                "ocs_keywords": ocs_matched[:3],
            }

            # 台账：matched 者先记为 pending（shown=False），
            # 待 top-N 选择后由 finalize_stamps 把实际展示者置 True
            if score >= config.MIN_SCORE or ocs_score >= ocs_min:
                new_digest_seen[paper_id] = {
                    "title": title,
                    "keywords": matched[:3],
                    "ocs_keywords": ocs_matched[:3],
                    "date": today,
                    "shown": False,
                    "score": score,
                    "core_score": main["core_score"],
                    "rank_score": main["rank_score"],
                    "core_pick": main["core_pick"],
                    "ocs_focus": ocs["focus"],
                    "ocs_software": ocs["software_score"],
                    "ocs_hardware": ocs["hardware_score"],
                }

        # 更新该分类的统计
        s = stats[cat]
        s["skipped"] = feed_skipped
        s["already_seen"] = feed_already_seen
        s["selected"] = feed_selected
        s["ocs_selected"] = feed_ocs_selected

    return {
        "selected": selected,
        "ocs_selected": ocs_selected,
        "carry_over": carry_over,
        "no_kw_papers": no_kw_papers,
        "near_misses": near_misses,
        "new_seen": new_seen,
        "new_digest_seen": new_digest_seen,
        "counts": counts,
    }


def main():
    do_wait = "--wait" in sys.argv
    exact_date = None

    # ── --date YYYY-MM-DD：只抓这一天（不是最近 3 天）──
    if "--date" in sys.argv:
        idx = sys.argv.index("--date")
        if idx + 1 < len(sys.argv):
            date_arg = sys.argv[idx + 1]
            try:
                datetime.strptime(date_arg, "%Y-%m-%d")
            except ValueError:
                print(f"[error] Invalid date format: {date_arg}. Use YYYY-MM-DD.")
                raise SystemExit(2)
            config.DATE_OVERRIDE = date_arg
            exact_date = date_arg
            print(f"[backfill] Targeting exact date: {date_arg}")
        else:
            print("[error] --date requires a value: --date YYYY-MM-DD")
            raise SystemExit(2)

    # ── --from / --to：区间回填（优先于 --date）──
    range_from, range_to = _parse_range_args()
    if range_from is False:
        raise SystemExit(2)
    if range_from is None and range_to is None and exact_date:
        range_from = range_to = exact_date

    # ── Stage 1: Fetch ─────────────────────────────────────
    print("Fetching from arXiv...")
    fetch_report = {}
    entries_by_cat, stats = fetch.fetch_all(
        wait_on_429=do_wait, date_from=range_from, date_to=range_to,
        report=fetch_report)

    # ── Optional discovery sources: HF Daily Papers / Semantic Scholar ──
    extra = {"entries": [], "hf_entries": [], "s2_entries": [],
             "openalex_entries": [], "report": {}}
    if "--no-extra-sources" not in sys.argv:
        try:
            d_from = datetime.strptime(fetch_report["date_from"], "%Y-%m-%d").date()
            d_to = datetime.strptime(fetch_report["date_to"], "%Y-%m-%d").date()
        except (KeyError, TypeError, ValueError):
            d_from = d_to = config.bj_now().date()
        print("\nFetching optional sources (Hugging Face / Semantic Scholar)...")
        try:
            extra = extra_sources.collect(d_from, d_to)
        except Exception as ex:  # discovery must never break the digest
            print(f"  [extra] disabled after error: {ex}")
            extra["report"]["fatal_error"] = str(ex)
        s2 = extra.get("report", {}).get("semantic_scholar", {})
        if s2 and not s2.get("enabled"):
            print(f"  [s2] {s2.get('reason', 'disabled')}")

    for source, source_entries in extra.get("entries", []):
        entries_by_cat.append((source, source_entries))
        stats[source] = {
            "total": len(source_entries), "skipped": 0, "already_seen": 0,
            "selected": 0, "ocs_selected": 0,
        }

    fetch_report["extra_sources"] = extra.get("report", {})
    try:
        config.FETCH_REPORT_FILE.write_text(
            json.dumps(fetch_report, indent=2, ensure_ascii=False),
            encoding="utf-8")
    except OSError:
        pass

    total_in_feeds = sum(s["total"] for s in stats.values())
    if total_in_feeds == 0:
        failed_fetch = (fetch_report.get("fatal")
                        or not fetch_report.get("ok_categories"))
        if failed_fetch:
            print("=" * 60)
            print("⚠️  CRITICAL: no usable entries and no healthy arXiv "
                  "category.")
            print("    This is a fetch failure, not an empty arXiv day.")
            print(f"    Per-category report: {config.FETCH_REPORT_FILE}")
            print("=" * 60)
            raise SystemExit(2)
        print("[skip] No new papers in the selected window "
              "(all categories returned a valid empty feed).")
        return
    if fetch_report.get("fatal") and not extra.get("entries"):
        print("=" * 60)
        print("⚠️  CRITICAL: every arXiv category failed and no fallback "
              "returned papers.")
        print(f"    Per-category report: {config.FETCH_REPORT_FILE}")
        print("=" * 60)
        raise SystemExit(2)
    if fetch_report.get("degraded"):
        failed = ", ".join(fetch_report.get("failed_categories", []))
        print(f"[warn] Degraded fetch: {failed}. Continuing with the "
              f"categories/sources that did return data.")

    # ── Stage 2: Filter ────────────────────────────────────
    print(f"Filtering {total_in_feeds} entries...")

    seen = digest.load_seen()
    digest_seen = digest.load_digest_seen()
    today = config.bj_today_str()
    ignore_seen = "--ignore-seen" in sys.argv
    if ignore_seen:
        print("[ignore-seen] Reprocessing all entries "
              "regardless of digest history.")

    result = evaluate_entries(entries_by_cat, today, seen, digest_seen,
                              stats, ignore_seen=ignore_seen)

    no_kw_papers = result["no_kw_papers"]
    near_misses = result["near_misses"]
    new_seen = result["new_seen"]
    new_digest_seen = result["new_digest_seen"]
    total_skipped = result["counts"]["skipped"]
    total_already_seen = result["counts"]["already_seen"]

    # ── Stage 3: Select ────────────────────────────────────
    selected, total_main = digest.select_top(result["selected"],
                                             config.MAX_PAPERS)
    ocs_selected, total_ocs = digest.select_ocs(
        result["ocs_selected"], config.MAX_OCS_PAPERS,
        config.MAX_OCS_HARDWARE)
    carry_over, total_carry = digest.select_top(result["carry_over"],
                                                config.MAX_RESURFACED)

    # ── Optional metadata: HF upvotes, S2 citations/TLDR ──
    enrich_report = {}
    if "--no-extra-sources" in sys.argv:
        enrich_report["skipped"] = "disabled by --no-extra-sources"
    else:
        try:
            extra_sources.enrich_papers(
                selected + ocs_selected + carry_over,
                hf_entries=extra.get("hf_entries"),
                s2_entries=extra.get("s2_entries"),
                openalex_entries=extra.get("openalex_entries"),
                report=enrich_report)
        except Exception as ex:  # enrichment is never allowed to break digest
            print(f"[warn] enrichment skipped: {ex}")
            enrich_report["error"] = str(ex)
    fetch_report["enrichment"] = enrich_report

    # 实际展示者置 shown=True，其余 matched 保持 pending
    digest.finalize_stamps(new_digest_seen, selected, ocs_selected,
                           carry_over, today)

    # 保存状态
    digest.save_seen(new_seen)
    digest.save_digest_seen(new_digest_seen)
    digest.save_no_kw(no_kw_papers)

    # ── 零关键词建议 ──
    if no_kw_papers:
        suggestions = flt.suggest_keywords(no_kw_papers)
        if suggestions:
            print(f"\nKeyword suggestions from zero-kw papers:")
            for bg, cnt in suggestions:
                print(f"  + \"{bg}\"  (×{cnt})")
            print()

    # ── 统计输出 ───────────────────────────────────────────
    print(f"Total entries across feeds: {total_in_feeds}")
    print(f"Duplicates skipped (this run): {total_skipped}")
    print(f"All-time papers seen: {len(new_seen)}")
    print(f"Already in previous digest: {len(digest_seen)} → "
          f"skipped {total_already_seen} today")
    print(f"Main filter matched: {total_main} → top {len(selected)}")
    print(f"OCS spotlight matched: {total_ocs} → top {len(ocs_selected)}")
    print(f"Carry-over candidates (pending, score ≥ {config.RESURFACE_MIN_SCORE}): "
          f"{total_carry} → top {len(carry_over)}")
    print(f"Zero-keyword papers (no filter hit): {len(no_kw_papers)}")
    new_today = total_in_feeds - total_skipped - total_already_seen
    print(f"New papers seen this run: {new_today}")
    print()

    # ── 0-match 诊断 ───────────────────────────────────────
    if total_main == 0 and total_ocs == 0 and total_carry == 0:
        near_misses.sort(key=lambda x: x["score"], reverse=True)
        top_near = near_misses[:10]
        if top_near:
            print("─" * 60)
            print(f"Top {len(top_near)} near-miss papers "
                  f"(score {config.MIN_SCORE-2}–{config.MIN_SCORE-1}, "
                  f"threshold={config.MIN_SCORE}):")
            for i, p in enumerate(top_near, 1):
                print(f"  {i:2d}. [{p['score']}/{p['max_single']}] "
                      f"{p['title'][:90]}")
                if p["matched"]:
                    print(f"       matched: {', '.join(p['matched'][:5])}")
            print()
            print("If these look relevant, consider lowering MIN_SCORE "
                  "or adding keywords.")
            print("─" * 60)
            print()
        else:
            print(f"No papers scored ≥ {config.MIN_SCORE-2} — "
                  f"keyword coverage may be too narrow.")
            print()

    # ── Fallback: 结果为空但今天已有非空 digest → 保留 ──────
    if not selected and not ocs_selected and not carry_over:
        if config.OUTPUT_FILE.exists():
            existing = config.OUTPUT_FILE.read_text(encoding="utf-8")
            if today in existing and not digest._is_empty(existing):
                print(f"[skip] Result is empty but today's digest already "
                      f"has content — keeping existing {config.OUTPUT_FILE}")
                return
        print("[skip] No papers matched and no existing digest to fall back "
              "on — nothing to send. This may be a rate-limit or API outage.")
        if fetch_report.get("fatal"):
            raise SystemExit(2)
        return

    # ── Stage 4: Digest ────────────────────────────────────
    notes = []
    if fetch_report.get("degraded"):
        failed = ", ".join(fetch_report.get("failed_categories", []))
        notes.append(f"Fetch degraded: {failed} failed; remaining sources "
                     f"are shown. See fetch_report.json.")
    digest.generate_markdown(selected, ocs_selected, total_main, total_ocs,
                             carry_over, notes=notes)
    digest.print_results(selected, ocs_selected, carry_over)
    try:
        config.FETCH_REPORT_FILE.write_text(
            json.dumps(fetch_report, indent=2, ensure_ascii=False),
            encoding="utf-8")
    except OSError:
        pass
    print(f"Saved to {config.OUTPUT_FILE}")


if __name__ == "__main__":
    main()
