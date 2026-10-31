#!/usr/bin/env python3
"""Digest 管理：状态文件读写、Top-N 选择、Markdown 生成、已有 digest 解析。"""

import json
import re

from arxiv_digest import config
from arxiv_digest import filter as flt


# ── JSON state I/O ─────────────────────────────────────────────

def _load_json_migrated(filepath):
    """Load JSON, migrating old http://arxiv.org/abs/... keys to canonical IDs."""
    if not filepath.exists():
        return {}, False
    with open(filepath, "r") as f:
        data = json.load(f)
    migrated = False
    new_data = {}
    for key, value in data.items():
        new_key = flt.normalize_arxiv_id(key)
        if new_key != key:
            migrated = True
        new_data[new_key] = value
    return new_data, migrated


def load_seen():
    """{arxiv_id: {title, keywords, ocs_keywords}} — 所有查阅过的论文。"""
    data, migrated = _load_json_migrated(config.STATE_FILE)
    if migrated:
        save_seen(data)
    return data


def save_seen(seen):
    with open(config.STATE_FILE, "w") as f:
        json.dump(seen, f, indent=2, ensure_ascii=False)


def load_digest_seen():
    """{arxiv_id: {title, keywords, ocs_keywords, date, shown, score}} — 上过 digest 的论文。

    shown=True  → 已实际出现在 digest 板块中，永久跳过。
    shown=False → matched 但被 top-N 挤掉（pending），可在补遗窗口内复活。
    旧格式条目无 shown 字段 → 迁移时一律视为 shown=True（保守，行为不变）。
    """
    data, migrated = _load_json_migrated(config.DIGEST_STATE_FILE)
    for value in data.values():
        if "shown" not in value:
            value["shown"] = True
            migrated = True
    if migrated:
        save_digest_seen(data)
    return data


def save_digest_seen(digest_seen):
    with open(config.DIGEST_STATE_FILE, "w") as f:
        json.dump(digest_seen, f, indent=2, ensure_ascii=False)


def save_no_kw(papers):
    """保存零关键词论文供人工复查。"""
    out = {}
    for p in papers:
        # 用 title 做 key（无 arxiv id 可用）
        key = p["title"][:120]
        out[key] = {
            "title": p["title"],
            "link": p["link"],
            "first_author": p.get("first_author", ""),
            "year": p.get("year", ""),
            "summary": p.get("summary", ""),
        }
    no_kw_file = config.NO_KEYWORD_FILE
    with open(no_kw_file, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"Zero-keyword papers: {len(papers)} → saved to {no_kw_file.name}")


# ── Top-N selection ────────────────────────────────────────────

def select_top(scored_papers, max_n):
    """按 rank_score 降序排列，截取 top-N（rank = relevance + core signal）。"""
    scored_papers.sort(key=lambda x: (x.get("rank_score", x["score"]), x["score"]),
                       reverse=True)
    total = len(scored_papers)
    return scored_papers[:max_n], total


def select_ocs(scored_papers, max_n, max_hardware):
    """OCS spotlight：software-first 排序，纯硬件论文最多 max_hardware 篇。

    先按 focus（software > mixed > neutral > hardware）分层，再按分数排序。
    这样一篇分数很高的器件论文不会挤掉控制面/可靠性/调度论文。
    只有 focus == hardware 消耗配额；neutral 是“光网络但未归到软件/器件”的
    系统级论文，允许进入 spotlight。
    """
    focus_rank = {"software": 3, "mixed": 2, "neutral": 1, "hardware": 0}
    ordered = sorted(
        scored_papers,
        key=lambda x: (focus_rank.get(x.get("focus"), 0),
                       x.get("rank_score", x["score"]), x["score"]),
        reverse=True)
    picked, hardware = [], 0
    for paper in ordered:
        if paper.get("focus") == "hardware":
            if hardware >= max_hardware:
                continue
            hardware += 1
        picked.append(paper)
        if len(picked) >= max_n:
            break
    return picked, len(ordered)


def finalize_stamps(digest_seen, selected, ocs_selected, carry_over, today):
    """Top-N 选择后调用：把实际出现在 digest 各板块的论文标记为 shown=True。

    matched 但未被任何板块选中的论文保持 shown=False（pending），
    在 config.RESURFACE_DAYS 天内仍可通过 carry-over 板块复活。
    """
    for p in list(selected) + list(ocs_selected) + list(carry_over):
        pid = flt.normalize_arxiv_id(p.get("link", ""))
        base = flt.base_arxiv_id(p.get("link", ""))
        entry = digest_seen.get(pid) or digest_seen.get(base)
        if entry is None:
            continue
        entry["shown"] = True
        entry["shown_date"] = today


# ── Markdown generation ────────────────────────────────────────

_SOURCE_LABELS = {
    "arxiv": "arXiv",
    "arxiv-html": "arXiv (HTML fallback)",
    "huggingface": "Hugging Face Daily",
    "semantic-scholar": "Semantic Scholar",
    "openalex": "OpenAlex",
}


def _paper_authors(paper):
    authors = [str(a).strip() for a in (paper.get("authors") or []) if str(a).strip()]
    if not authors:
        fallback = paper.get("first_author")
        authors = [fallback] if fallback and fallback != "Unknown" else []
    if not authors:
        return "N/A"
    limit = getattr(config, "DIGEST_MAX_AUTHORS", 8)
    if len(authors) > limit:
        return ", ".join(authors[:limit]) + f", et al. ({len(authors)} authors)"
    return ", ".join(authors)


def _paper_pdf_link(paper):
    if paper.get("pdf_link"):
        return paper["pdf_link"]
    link = paper.get("link", "")
    if "arxiv.org/abs/" in link:
        return link.replace("/abs/", "/pdf/")
    return ""


def _paper_why(paper, limit=8):
    details = paper.get("matched_detail") or []
    if details:
        parts = []
        for item in details[:limit]:
            title_mark = "*" if item.get("in_title") else ""
            parts.append(f"{item['keyword']} ({item['weight']}{title_mark})")
        return ", ".join(parts)
    return ", ".join(paper.get("matched", [])[:limit]) or "—"


def _paper_meta_lines(paper):
    lines = []
    if paper.get("hf_upvotes"):
        url = paper.get("hf_url") or ""
        suffix = f" ([discussion]({url}))" if url else ""
        lines.append(f"**HF:** {paper['hf_upvotes']} upvotes{suffix}")
    if paper.get("citation_count"):
        extra = ""
        if paper.get("influential_citations"):
            extra = f" ({paper['influential_citations']} influential)"
        lines.append(f"**Citations:** {paper['citation_count']}{extra}")
    if paper.get("venue"):
        lines.append(f"**Venue:** {paper['venue']}")
    if paper.get("s2_tldr"):
        lines.append(f"**TLDR:** {paper['s2_tldr']}")
    if paper.get("arxiv_comment"):
        lines.append(f"**Comment:** {paper['arxiv_comment']}")
    return lines


def _render_paper(paper, index, section, total=None):
    """Render one paper with enough metadata to judge relevance without opening it."""
    title = (paper.get("title") or "Untitled").replace("\n", " ").strip()
    core_mark = "★ " if section == "main" and paper.get("core_pick") else ""
    lines = [f"### {index}. {core_mark}{title}", ""]

    lines.extend([f"**Authors:** {_paper_authors(paper)}", ""])

    meta = []
    if paper.get("primary_category"):
        meta.append(f"**Category:** {paper['primary_category']}")
    if paper.get("published"):
        meta.append(f"**Submitted:** {paper['published']}")
    source = _SOURCE_LABELS.get(paper.get("source", ""), paper.get("source", ""))
    if source:
        meta.append(f"**Source:** {source}")
    if paper.get("first_seen"):
        meta.append(f"**First seen:** {paper['first_seen']}")
    if meta:
        lines.extend(["  |  ".join(meta), ""])

    if section == "ocs":
        lines.extend([
            f"**OCS Score:** {paper.get('score', 'N/A')}  |  "
            f"**Focus:** {paper.get('focus', 'N/A')}  |  "
            f"**Software / Hardware:** {paper.get('software_score', 'N/A')} / "
            f"{paper.get('hardware_score', 'N/A')}",
            "",
            f"**Why:** {_paper_why(paper)}",
            "",
        ])
    else:
        score_line = (f"**Score:** {paper.get('score', 'N/A')}  |  "
                      f"**Core:** {paper.get('core_score', 'N/A')}")
        if paper.get("rank_score") is not None:
            score_line += f"  |  **Rank:** {paper['rank_score']:.1f}"
        lines.extend([score_line, "", f"**Why:** {_paper_why(paper)}", ""])

    abs_link = paper.get("link", "")
    pdf_link = _paper_pdf_link(paper)
    links = [f"[abs]({abs_link})"] if abs_link else []
    if pdf_link:
        links.append(f"[PDF]({pdf_link})")
    if links:
        lines.extend([f"**Links:** {' · '.join(links)}", ""])

    meta_lines = _paper_meta_lines(paper)
    if meta_lines:
        lines.extend(["  \n".join(meta_lines), ""])

    summary = (paper.get("summary") or "").strip()
    max_chars = getattr(config, "SUMMARY_MAX_CHARS", 2400)
    if len(summary) > max_chars:
        summary = summary[:max_chars].rstrip() + "…"
    lines.extend(["**Abstract:**", "", summary or "_No abstract available._", ""])
    lines.extend(["---", ""])
    return lines


def generate_markdown(papers, ocs_papers, total_main, total_ocs,
                      carry_over=None, notes=None):
    """生成 daily_digest.md。carry_over: 高分补遗论文（含 first_seen 日期）。"""

    today = config.bj_today_str()
    carry_over = carry_over or []

    lines = [f"# arXiv Daily Digest ({today})", ""]
    if notes:
        lines.extend([f"> {note}" for note in notes] + [""])

    # ── 主 Digest ──
    main_note = (
        f"Showing top {len(papers)} of {total_main} matched"
        if total_main > len(papers) else f"Total: {len(papers)}"
    )
    lines.extend(["## Main Digest", "", f"{main_note}", ""])
    if papers:
        for i, paper in enumerate(papers, start=1):
            lines.extend(_render_paper(paper, i, "main"))
    else:
        lines.extend(["_No papers matched the main filter today._", "", "---", ""])

    # ── High-Score Carry-Over（高分补遗）──
    lines.extend(["## High-Score Carry-Over (missed earlier this week)", ""])
    if carry_over:
        lines.extend([f"Total: {len(carry_over)}", ""])
        for i, paper in enumerate(carry_over, start=1):
            lines.extend(_render_paper(paper, i, "carry"))
    else:
        lines.extend(["_No carry-over papers this week._", "", "---", ""])

    # ── OCS Spotlight ──
    ocs_note = (
        f"Showing top {len(ocs_papers)} of {total_ocs} matched"
        if total_ocs > len(ocs_papers) else f"Total: {len(ocs_papers)}"
    )
    lines.extend([
        "## OCS & Optical Networking Spotlight (software-first)", "",
        f"{ocs_note}", "",
        "Software/control/reliability terms count fully; device/physics terms "
        f"are down-weighted and hardware-only papers are capped at "
        f"{config.MAX_OCS_HARDWARE}.", "",
    ])
    if ocs_papers:
        for i, paper in enumerate(ocs_papers, start=1):
            lines.extend(_render_paper(paper, i, "ocs"))
    else:
        lines.extend(["_No OCS-related papers found today._", "", "---", ""])

    config.OUTPUT_FILE.write_text("\n".join(lines), encoding="utf-8")



# ── Console output ──────────────────────────────────────────────

def print_results(papers, ocs_papers, carry_over=None):
    """Print selected papers directly to stdout so you see results immediately."""
    width = 80

    def _print_section(heading, papers):
        if not papers:
            print(f"\n{'─'*width}")
            print(f"  {heading}: 0 papers matched")
            print(f"{'─'*width}")
            return
        print(f"\n{'═'*width}")
        print(f"  {heading} — {len(papers)} papers")
        print(f"{'═'*width}")
        for i, p in enumerate(papers, 1):
            title = p["title"].replace("\n", " ").strip()
            score = p["score"]
            keywords = ", ".join(p.get("matched", [])[:6])
            link = p.get("link", "")
            author = p.get("first_author", "N/A")
            year = p.get("year", "N/A")
            snippet = p.get("summary", "")[:260].replace("\n", " ").strip()
            first_seen = p.get("first_seen")
            category = p.get("primary_category", "")

            print(f"\n  {i:2d}. {title}")
            extra = ""
            if p.get("core_score") is not None:
                extra += f"  |  Core: {p['core_score']}"
            if p.get("focus"):
                extra += f"  |  Focus: {p['focus']}"
            print(f"      Score: {score}  |  Author: {author}  |  Year: {year}{extra}")
            if category:
                print(f"      Category: {category}")
            if first_seen:
                print(f"      First seen: {first_seen} (carry-over)")
            print(f"      Keywords: {keywords}")
            print(f"      Link: {link}")
            print(f"      Abstract: {snippet}...")
        print(f"\n{'─'*width}")

    _print_section("Main Digest", papers)
    _print_section("High-Score Carry-Over", carry_over or [])
    _print_section("OCS & Optical Networking Spotlight", ocs_papers)


# ── Helper ─────────────────────────────────────────────────────

def _is_empty(md_text):
    """Markdown 内容是否为空 digest（任何板块有论文条目即非空）。"""
    return re.search(r"^### \d+\. ", md_text, re.MULTILINE) is None
