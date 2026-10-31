#!/usr/bin/env python3
"""Offline tests for Hugging Face / Semantic Scholar source parsing.

No network calls: only the pure parsers and enrichment merge are exercised.

Run: python3 arxiv_digest/test_sources.py
"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from arxiv_digest import config
from arxiv_digest import extra_sources as extra

CHECKS = 0


def check(cond, label):
    global CHECKS
    CHECKS += 1
    if not cond:
        raise AssertionError(f"FAIL: {label}")
    print(f"  ✓ {label}")


print("─" * 60)
print("  extra_sources.py — HF / S2 parsing and enrichment")
print("─" * 60)

HF_ITEMS = [{
    "paper": {
        "id": "2610.30551",
        "title": "OMRC: Multipath Reliability for Direct-Connect Optical Interconnects",
        "summary": "Network failures degrade MRC in direct-connect optical datacenters.",
        "authors": [{"name": "Eric Ding"}, {"name": "Keshav Pradeep"}],
        "publishedAt": "2026-10-22T00:00:00.000Z",
        "upvotes": 42,
        "ai_summary": "Reliability for optical interconnects.",
        "githubRepo": "",
    }
}]

hf_entries = extra._parse_hf_items(HF_ITEMS)
check(len(hf_entries) == 1, "HF parser keeps one paper")
hf = hf_entries[0]
check(hf.source == "huggingface", "HF source tag is set")
check(hf.hf_upvotes == 42, "HF upvotes are captured")
check([a.name for a in hf.authors] == ["Eric Ding", "Keshav Pradeep"],
      "HF authors are captured")
check("2610.30551" in hf.id, "HF entry points at the arXiv abs page")

S2_ITEMS = [{
    "paperId": "abc123",
    "title": "OMRC: Multipath Reliability for Direct-Connect Optical Interconnects",
    "abstract": "Network failures degrade MRC in optical datacenters.",
    "authors": [{"name": "Eric Ding"}, {"name": "Rachee Singh"}],
    "externalIds": {"ArXiv": "2610.30551"},
    "publicationDate": "2026-10-22",
    "venue": "HotNets",
    "citationCount": 7,
    "influentialCitationCount": 2,
    "tldr": {"text": "Remaps circuits and collectives to survive failures."},
    "fieldsOfStudy": ["Computer Science"],
}]

s2_entries = extra._parse_s2_items(S2_ITEMS)
check(len(s2_entries) == 1, "S2 parser keeps one paper")
s2 = s2_entries[0]
check(s2.source == "semantic-scholar", "S2 source tag is set")
check(s2.s2_citations == 7 and s2.s2_influential == 2,
      "S2 citation counts are captured")
check(s2.s2_tldr.startswith("Remaps"), "S2 TLDR is captured")
check("2610.30551" in s2.id, "S2 entry is matched back to arXiv")

# S2 paper without an arXiv id must still be representable by its own id/link.
orphan = extra._parse_s2_items([{
    "paperId": "xyz",
    "title": "A Non-arXiv Optical Paper",
    "abstract": "Optical switching.",
    "url": "https://example.org/paper",
    "externalIds": {},
}])
check(orphan and orphan[0].id == "s2:xyz",
      "non-arXiv S2 papers get a stable s2: id")

# ── OpenAlex parser / abstract reconstruction ────────────────
OA_ITEMS = [{
    "id": "https://openalex.org/W1",
    "title": "OMRC: Multipath Reliability for Direct-Connect Optical Interconnects",
    "abstract_inverted_index": {
        "Network": [0], "failures": [1], "degrade": [2],
        "optical": [3], "datacenters.": [4],
        "We": [5], "remap": [6], "collectives.": [7],
    },
    "authorships": [
        {"author": {"display_name": "Eric Ding"}},
        {"author": {"display_name": "Rachee Singh"}},
    ],
    "primary_location": {
        "landing_page_url": "https://arxiv.org/abs/2610.30551",
    },
    "locations": [{"landing_page_url": "https://arxiv.org/abs/2610.30551"}],
    "publication_date": "2026-10-22",
    "cited_by_count": 3,
    "doi": None,
}]
oa_entries = extra._parse_openalex_items(OA_ITEMS)
check(len(oa_entries) == 1, "OpenAlex parser keeps one work")
oa = oa_entries[0]
check(oa.source == "openalex", "OpenAlex source tag is set")
check(oa.summary.startswith("Network failures degrade optical"),
      "OpenAlex abstract is reconstructed from the inverted index")
check([a.name for a in oa.authors] == ["Eric Ding", "Rachee Singh"],
      "OpenAlex authors are captured")
check("2610.30551" in oa.id, "OpenAlex work is linked back to arXiv")

# ── Enrichment merge ─────────────────────────────────────────
paper = {
    "title": "OMRC",
    "link": "https://arxiv.org/abs/2610.30551v1",
    "summary": "abstract",
}
old_key = config.SEMANTIC_SCHOLAR_API_KEY
config.SEMANTIC_SCHOLAR_API_KEY = ""   # keep the test offline
try:
    report = {}
    extra.enrich_papers([paper], hf_entries=hf_entries,
                        s2_entries=s2_entries,
                        openalex_entries=oa_entries, report=report)
finally:
    config.SEMANTIC_SCHOLAR_API_KEY = old_key
check(paper["hf_upvotes"] == 42, "enrichment merges HF upvotes by base id")
check(paper["citation_count"] == 7, "enrichment merges S2 citation count")
check(paper["venue"] == "HotNets", "enrichment merges S2 venue")
check(report.get("s2_enrichment") == "disabled",
      "S2 batch enrichment is skipped without an API key")

# OpenAlex citations fill the gap when S2 is unavailable.
paper_oa = {"title": "OMRC", "link": "https://arxiv.org/abs/2610.30551v1",
            "summary": "abstract"}
extra.enrich_papers([paper_oa], hf_entries=[], s2_entries=[],
                    openalex_entries=oa_entries, report={})
check(paper_oa["citation_count"] == 3,
      "OpenAlex citation count is used when S2 is absent")

# ── collect(enabled=False) makes no requests ─────────────────
out = extra.collect(None, None, enabled=False)
check(out["entries"] == [] and out["hf_entries"] == [],
      "collect(enabled=False) is a no-op")
check("disabled" in out["report"]["extra_sources"],
      "collect reports why optional sources were skipped")

# ── HF date window stays small ───────────────────────────────
from datetime import date
dates = extra._hf_dates(date(2026, 10, 1), date(2026, 10, 31))
check(len(dates) == 3 and dates[0] == date(2026, 10, 31),
      "HF date helper caps lookback at 3 days")

print("─" * 60)
print(f"  All {CHECKS} checks passed.")
print("─" * 60)
