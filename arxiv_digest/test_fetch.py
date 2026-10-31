#!/usr/bin/env python3
"""Fetch-layer regression tests (offline; no network).

Covers the two failure modes that made the old digest silently empty:
  * HTTP 200 + 0 entries was treated as "no papers" even when the response
    was an HTML block page rather than a valid Atom feed.
  * The HTML fallback built a query arXiv does not understand
    (``cat:cs.NI AND submittedDate:...``), so it always "found 0 papers".

Also covers the real October OMRC paper as a parser/selection regression.

Run: python3 arxiv_digest/test_fetch.py
"""

import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import feedparser

from arxiv_digest import config
from arxiv_digest import fetch
from arxiv_digest import filter as flt

CHECKS = 0


def check(cond, label):
    global CHECKS
    CHECKS += 1
    if not cond:
        raise AssertionError(f"FAIL: {label}")
    print(f"  ✓ {label}")


RESULT_HTML = r"""
<ol class="breathe-horizontal">
  <li class="arxiv-result">
    <div class="is-marginless">
      <p class="list-title is-inline-block">
        <a href="https://arxiv.org/abs/2610.30551">arXiv:2610.30551</a>
      </p>
      <div class="tags is-inline-block">
        <span class="tag is-small is-link tooltip" data-tooltip="Networking">cs.NI</span>
      </div>
    </div>
    <p class="title is-5 mathjax">
      OMRC: Multipath Reliability for Direct-Connect
      <span class="search-hit mathjax">Optical</span> Interconnects
    </p>
    <p class="authors">
      <span class="has-text-weight-semibold">Authors:</span>
      <a href="/search/?query=Ding">Eric Ding</a>,
      <a href="/search/?query=Pradeep">Keshav Pradeep</a>,
      <a href="/search/?query=Singh">Rachee Singh</a>
    </p>
    <p class="abstract mathjax">
      <span class="abstract-short has-text-grey-dark mathjax" style="display:inline;">
        Network failures degrade MRC in direct-connect optical datacenters&hellip;
        <a class="is-size-7" onclick="x()">&#9661; More</a>
      </span>
      <span class="abstract-full has-text-grey-dark mathjax" style="display:none;">
        Network failures degrade Multipath Reliable Connection (MRC) in
        direct-connect optical datacenters. We propose Optical MRC (OMRC),
        which remaps collectives and circuits, preserving 95.7% of training
        throughput and restoring stalled inference.
        <a class="is-size-7" onclick="y()">&#9651; Less</a>
      </span>
    </p>
    <p class="is-size-7"><span class="has-text-weight-semibold">Submitted</span>
      21 October, 2026; originally announced October 2026.</p>
  </li>
</ol>
"""

NO_RESULTS_HTML = """
<html><body><h1>Search</h1>
<p>Sorry, your query for all: "nonsense" produced no results.</p>
</body></html>
"""

ATOM_EMPTY = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
  <title>arXiv Query</title>
  <opensearch:totalResults>0</opensearch:totalResults>
</feed>
"""

ATOM_ONE = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
  <title>arXiv Query</title>
  <opensearch:totalResults>1</opensearch:totalResults>
  <entry>
    <id>http://arxiv.org/abs/2610.30551v1</id>
    <title>OMRC</title>
    <summary>abstract</summary>
    <link href="https://arxiv.org/abs/2610.30551v1" rel="alternate"/>
    <author><name>Eric Ding</name></author>
    <published>2026-10-22T00:00:00Z</published>
  </entry>
</feed>
"""


print("─" * 60)
print("  fetch.py — valid-feed detection, HTML parser, fallback")
print("─" * 60)

# ── Valid feed vs HTML block page ─────────────────────────────
atom_empty = feedparser.parse(ATOM_EMPTY)
atom_one = feedparser.parse(ATOM_ONE)
html_feed = feedparser.parse(b"<html><body>proxy login</body></html>")

check(fetch._is_valid_api_feed(atom_empty), "empty Atom feed is valid")
check(fetch._is_valid_api_feed(atom_one), "Atom feed with entries is valid")
check(not fetch._is_valid_api_feed(html_feed),
      "HTML block page is NOT a valid Atom feed")

state, _ = fetch._page_outcome(atom_empty, 200, None)
check(state == fetch.PAGE_EMPTY, "valid 200 + totalResults=0 → empty")
state, _ = fetch._page_outcome(html_feed, 200, None)
check(state == fetch.PAGE_FAILED,
      "200 + HTML + no totalResults → failed (not empty)")
state, _ = fetch._page_outcome(atom_one, 200, None)
check(state == fetch.PAGE_OK, "valid feed with entries → ok")

# ── HTML parser ───────────────────────────────────────────────
entries = fetch._parse_html_results(RESULT_HTML, fallback_date=date(2026, 10, 22))
check(len(entries) == 1, "HTML parser finds one paper")
entry = entries[0]
check(flt.base_arxiv_id(entry.id) == "2610.30551", "HTML parser extracts arXiv id")
check(entry.title.startswith("OMRC:"), "HTML parser extracts title")
check([a.name for a in entry.authors] ==
      ["Eric Ding", "Keshav Pradeep", "Rachee Singh"],
      "HTML parser extracts all authors")
check(entry.arxiv_primary_category["term"] == "cs.NI",
      "HTML parser extracts primary category")
check("preserving 95.7%" in entry.summary and "More" not in entry.summary,
      "HTML parser extracts the full abstract, not the snippet")
check(flt.extract_published(entry) == "2026-10-21",
      "HTML parser extracts submitted date")

# ── Category query includes the interest filter ───────────────
url = fetch._build_category_url("physics.optics", date(2026, 10, 22),
                                date(2026, 10, 22))
check("abs%3A%22optical+network%22" in url,
      "category URL applies the optics interest filter")

# ── Advanced-search query is actually accepted ───────────────
with mock.patch.object(fetch, "_fetch_bytes_no_proxy",
                       return_value=(RESULT_HTML.encode(), 200)):
    got, state, detail = fetch._search_html_query(
        '"optical interconnect"', date(2026, 10, 21), date(2026, 10, 23))
check(state == fetch.PAGE_OK and len(got) == 1,
      "advanced-search parser returns the OMRC result")
with mock.patch.object(fetch, "_fetch_bytes_no_proxy",
                       return_value=(NO_RESULTS_HTML.encode(), 200)):
    got, state, _ = fetch._search_html_query(
        "nonsense", date(2026, 10, 21), date(2026, 10, 23))
check(state == fetch.PAGE_EMPTY and not got,
      "real 'no results' page is recognised as empty")

# ── fetch_all: all API failures still recover via HTML ───────
recovered = SimpleNamespace(
    id="http://arxiv.org/abs/2610.30551v1",
    title="OMRC: Multipath Reliability for Direct-Connect Optical Interconnects",
    summary="Network failures in direct-connect optical datacenters. "
            "We remap collectives and circuits.",
    link="https://arxiv.org/abs/2610.30551v1",
    author="Eric Ding",
    authors=[SimpleNamespace(name="Eric Ding")],
    published_parsed=None,
    arxiv_primary_category={"term": "cs.NI"},
    source="arxiv-html",
    arxiv_comment="",
)
report = {}
with mock.patch.object(fetch, "_fetch_window",
                       return_value=([], {"state": "failed", "pages": 1,
                                          "total": None})), \
        mock.patch.object(fetch, "_search_html_topics",
                          return_value=([recovered],
                                        {"state": "ok", "entries": 1,
                                         "queries_ok": 1,
                                         "queries_failed": 0})), \
        mock.patch.object(fetch.time, "sleep", return_value=None):
    entries_by_cat, stats = fetch.fetch_all(
        date_from="2026-10-22", date_to="2026-10-22", report=report)
check(any(cat == "arxiv.html" and papers for cat, papers in entries_by_cat),
      "HTML fallback entries are returned under arxiv.html")
check(report["html_fallback"] and report["html_fallback"][0]["entries"] == 1,
      "fetch report records the HTML recovery")
check(not report["fatal"], "recovered fetch is not reported as fatal")
check(report["degraded"], "API failure is still recorded as degraded")
check(stats["arxiv.html"]["total"] == 1,
      "stats include the fallback pseudo-category")

# ── fetch_all: no recovery → fatal ───────────────────────────
report2 = {}
with mock.patch.object(fetch, "_fetch_window",
                       return_value=([], {"state": "failed", "pages": 1,
                                          "total": None})), \
        mock.patch.object(fetch, "_search_html_topics",
                          return_value=([], {"state": "failed", "entries": 0,
                                             "queries_ok": 0,
                                             "queries_failed": 10})), \
        mock.patch.object(fetch.time, "sleep", return_value=None):
    fetch.fetch_all(date_from="2026-10-22", date_to="2026-10-22",
                    report=report2)
check(report2["fatal"], "unrecovered total failure is reported as fatal")

# ── Fallback results are limited to the failed category ──────
def _fake_window(cat, d1, d2, wait):
    if cat == "cs.ET":
        return [], {"state": "failed", "pages": 1, "total": None}
    return [], {"state": "empty", "pages": 1, "total": 0}


et_entry = fetch._HtmlEntry(
    id="https://arxiv.org/abs/2610.11111",
    title="Optical interconnect for chiplets",
    summary="A real cs.ET paper.",
    link="https://arxiv.org/abs/2610.11111",
    arxiv_primary_category={"term": "cs.ET"},
    authors=[SimpleNamespace(name="A. Author")],
)
ar_entry = fetch._HtmlEntry(
    id="https://arxiv.org/abs/2610.22222",
    title="A cs.AR paper that should not leak into cs.ET",
    summary="Wrong category.",
    link="https://arxiv.org/abs/2610.22222",
    arxiv_primary_category={"term": "cs.AR"},
    authors=[SimpleNamespace(name="B. Author")],
)
report3 = {}
with mock.patch.object(fetch, "_fetch_window", side_effect=_fake_window), \
        mock.patch.object(fetch, "_search_html_topics",
                          return_value=([et_entry, ar_entry],
                                        {"state": "ok", "entries": 2,
                                         "queries_ok": 1,
                                         "queries_failed": 0})), \
        mock.patch.object(fetch.time, "sleep", return_value=None):
    entries_by_cat, stats3 = fetch.fetch_all(
        date_from="2026-10-22", date_to="2026-10-22", report=report3)
html_papers = dict(entries_by_cat).get("arxiv.html", [])
check(len(html_papers) == 1 and html_papers[0].title.startswith("Optical"),
      "HTML fallback drops results from categories that did not fail")
check(stats3["arxiv.html"]["total"] == 1,
      "fallback stats count only used papers")
check(report3["html_fallback"][0]["used"] == 1,
      "fallback report records the used count")

# ── A genuine empty day is not a failure ─────────────────────
report4 = {}
with mock.patch.object(fetch, "_fetch_window",
                       return_value=([], {"state": "empty", "pages": 1,
                                          "total": 0})), \
        mock.patch.object(fetch.time, "sleep", return_value=None):
    fetch.fetch_all(date_from="2026-10-25", date_to="2026-10-25",
                    report=report4)
check(not report4["fatal"], "valid empty categories are not fatal")
check(len(report4["ok_categories"]) == len(config.CATEGORIES),
      "all categories are healthy on a genuine empty day")

# ── Date windows ─────────────────────────────────────────────
windows = list(fetch._iter_date_windows(date(2026, 10, 1), date(2026, 10, 7)))
check(windows[0][1] == date(2026, 10, 7) and len(windows) == 3,
      "3-day windows cover the requested range exactly")

print("─" * 60)
print(f"  All {CHECKS} checks passed.")
print("─" * 60)
