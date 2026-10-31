#!/usr/bin/env python3
"""Optional discovery / enrichment sources.

Three sources are supported:

* **Hugging Face Daily Papers** — no API key.  One request per day, returns
  ~50 papers with title, abstract, authors and upvotes.  Useful as a second
  discovery channel for ML-systems papers that are not in the arXiv category
  feeds yet.
* **OpenAlex** — no API key.  Used as a bibliographic fallback when the arXiv
  API/search endpoints are throttled; returns titles, full abstracts (as an
  inverted index), authors and arXiv landing links.
* **Semantic Scholar** — optional, requires ``SEMANTIC_SCHOLAR_API_KEY``.
  Anonymous requests are frequently HTTP 429, so the source is disabled when
  no key is present.  When enabled it provides discovery results plus citation
  counts / TLDRs for the selected papers.

Everything here is best-effort: network errors are reported, never raised, and
never prevent the arXiv digest from being written.
"""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from types import SimpleNamespace

from arxiv_digest import config
from arxiv_digest import filter as flt

_UA = "arXivDailyDigest/2.0 (mailto:rawking1621@gmail.com)"
_ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5})(?:v\d+)?")
_CACHE_TTL = 7 * 24 * 3600


# ── Generic HTTP JSON ──────────────────────────────────────────

def _get_json(url, timeout=25, headers=None):
    """GET JSON.  Returns ``(obj, status, error)``; never raises."""
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "application/json",
        **(headers or {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 0) or resp.getcode() or 0
            return json.loads(resp.read().decode("utf-8", errors="replace")), status, None
    except urllib.error.HTTPError as e:
        return None, e.code, f"HTTP {e.code}"
    except Exception as e:  # timeout / DNS / JSON
        return None, 0, f"{type(e).__name__}: {str(e)[:120]}"


def _post_json(url, payload, timeout=30, headers=None):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "User-Agent": _UA,
            "Content-Type": "application/json",
            "Accept": "application/json",
            **(headers or {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 0) or resp.getcode() or 0
            return json.loads(resp.read().decode("utf-8", errors="replace")), status, None
    except urllib.error.HTTPError as e:
        return None, e.code, f"HTTP {e.code}"
    except Exception as e:
        return None, 0, f"{type(e).__name__}: {str(e)[:120]}"


# ── Hugging Face Daily Papers ──────────────────────────────────

def _hf_dates(date_from, date_to, max_days=3):
    """Return at most ``max_days`` dates ending at date_to (newest first)."""
    out = []
    day = date_to
    while day >= date_from and len(out) < max_days:
        out.append(day)
        day -= timedelta(days=1)
    return out


def _parse_hf_items(items):
    """Convert HF daily-paper JSON items into duck-typed entries."""
    entries = []
    for item in items or []:
        paper = item.get("paper") if isinstance(item, dict) else None
        if not isinstance(paper, dict):
            continue
        raw_id = str(paper.get("id") or "").strip()
        m = _ARXIV_ID_RE.search(raw_id)
        if not m:
            continue
        arxiv_id = m.group(1)
        title = (paper.get("title") or item.get("title") or "").strip()
        summary = (paper.get("summary") or item.get("summary") or "").strip()
        if not title:
            continue

        authors = []
        for a in paper.get("authors") or []:
            if isinstance(a, dict):
                name = a.get("name") or ((a.get("user") or {}).get("fullname"))
                if name:
                    authors.append(SimpleNamespace(name=name))
        if not authors and paper.get("submittedOnDailyBy"):
            name = (paper["submittedOnDailyBy"] or {}).get("fullname")
            if name:
                authors.append(SimpleNamespace(name=name))

        published = (paper.get("publishedAt") or "")[:10]
        try:
            published_parsed = time.strptime(published, "%Y-%m-%d")
        except ValueError:
            published_parsed = None

        link = f"{config.ARXIV_ABS_BASE_URL.rstrip('/')}/abs/{arxiv_id}"
        entry = SimpleNamespace(
            id=link,
            title=title,
            summary=summary,
            link=link,
            arxiv_primary_category={"term": ""},
            author=authors[0].name if authors else "Unknown",
            authors=authors,
            published=paper.get("publishedAt"),
            updated=paper.get("submittedOnDailyAt"),
            published_parsed=published_parsed,
            source="huggingface",
            arxiv_comment="",
            hf_upvotes=paper.get("upvotes") or 0,
            hf_url=f"https://huggingface.co/papers/{arxiv_id}",
            hf_ai_summary=(paper.get("ai_summary") or "").strip(),
            hf_github=paper.get("githubRepo") or "",
        )
        entries.append(entry)
    return entries


def fetch_hf_daily(date_from, date_to):
    """Return ``(entries, report)`` for HF Daily Papers.  Never raises."""
    report = {"source": "huggingface", "enabled": config.ENABLE_HF_DAILY,
              "dates": [], "entries": 0, "errors": []}
    if not config.ENABLE_HF_DAILY:
        return [], report

    all_entries = []
    seen = set()
    for day in _hf_dates(date_from, date_to):
        report["dates"].append(str(day))
        url = (f"{config.HF_DAILY_URL}?"
               f"{urllib.parse.urlencode({'date': f'{day:%Y-%m-%d}'})}")
        data, status, error = _get_json(url, timeout=config.HF_DAILY_TIMEOUT)
        if data is None:
            report["errors"].append(f"{day}: {error or status}")
            continue
        entries = _parse_hf_items(data)
        for entry in entries:
            key = flt.base_arxiv_id(entry.id)
            if key in seen:
                continue
            seen.add(key)
            all_entries.append(entry)
        time.sleep(0.5)

    report["entries"] = len(all_entries)
    return all_entries, report


# ── OpenAlex (no key; fallback when arXiv search is throttled) ─

def _reconstruct_abstract(inverted):
    """OpenAlex returns abstracts as {word: [positions]}; rebuild the text."""
    if not isinstance(inverted, dict) or not inverted:
        return ""
    positions = {}
    for word, idxs in inverted.items():
        for idx in idxs or []:
            try:
                positions[int(idx)] = word
            except (TypeError, ValueError):
                continue
    if not positions:
        return ""
    return " ".join(positions[i] for i in sorted(positions))


def _parse_openalex_items(items):
    """Convert OpenAlex works into duck-typed entries."""
    entries = []
    for work in items or []:
        if not isinstance(work, dict):
            continue
        title = (work.get("title") or "").strip()
        if not title:
            continue

        locations = []
        primary = work.get("primary_location") or {}
        if primary.get("landing_page_url"):
            locations.append(primary["landing_page_url"])
        for loc in work.get("locations") or []:
            if isinstance(loc, dict) and loc.get("landing_page_url"):
                locations.append(loc["landing_page_url"])

        arxiv_id = ""
        link = ""
        for url in locations:
            m = _ARXIV_ID_RE.search(url or "")
            if m:
                arxiv_id = m.group(1)
                link = f"{config.ARXIV_ABS_BASE_URL.rstrip('/')}/abs/{arxiv_id}"
                break
        if not link:
            doi = (work.get("doi") or "").replace("https://doi.org/", "")
            if doi:
                link = f"https://doi.org/{doi}"
            elif work.get("id"):
                link = work["id"]
            else:
                continue

        authors = []
        for authorship in work.get("authorships") or []:
            if not isinstance(authorship, dict):
                continue
            name = ((authorship.get("author") or {}).get("display_name")
                    or authorship.get("raw_author_name") or "")
            if name:
                authors.append(SimpleNamespace(name=name))

        published = (work.get("publication_date") or "")[:10]
        try:
            published_parsed = time.strptime(published, "%Y-%m-%d")
        except ValueError:
            published_parsed = None

        entry_id = link if arxiv_id else f"openalex:{work.get('id', link)}"
        entry = SimpleNamespace(
            id=entry_id,
            title=title,
            summary=_reconstruct_abstract(work.get("abstract_inverted_index")),
            link=link,
            arxiv_primary_category={"term": ""},
            author=authors[0].name if authors else "Unknown",
            authors=authors,
            published=work.get("publication_date"),
            updated=work.get("publication_date"),
            published_parsed=published_parsed,
            source="openalex",
            arxiv_comment="",
            openalex_id=work.get("id"),
            openalex_citations=work.get("cited_by_count"),
        )
        entries.append(entry)
    return entries


def fetch_openalex(date_from, date_to):
    """Return ``(entries, report)`` for the no-key OpenAlex fallback."""
    report = {"source": "openalex", "enabled": config.ENABLE_OPENALEX,
              "queries": [], "entries": 0, "errors": []}
    if not config.ENABLE_OPENALEX:
        return [], report

    fields = ("id,title,abstract_inverted_index,authorships,"
              "primary_location,locations,publication_date,ids,doi,"
              "cited_by_count")
    all_entries = []
    seen = set()

    for query in config.OPENALEX_QUERIES:
        report["queries"].append(query)
        params = {
            "search": query,
            "filter": (f"from_publication_date:{date_from:%Y-%m-%d},"
                       f"to_publication_date:{date_to:%Y-%m-%d}"),
            "per-page": str(config.OPENALEX_LIMIT),
            "select": fields,
        }
        url = f"{config.OPENALEX_API}?{urllib.parse.urlencode(params)}"
        data = None
        for attempt, delay in enumerate((0, 2, 6)):
            if delay:
                time.sleep(delay)
            data, status, error = _get_json(
                url, timeout=config.OPENALEX_TIMEOUT)
            if data is not None or status == 0:
                break
        if data is None:
            report["errors"].append(f"{query}: {error or status}")
            continue

        for entry in _parse_openalex_items(data.get("results") or []):
            key = flt.base_arxiv_id(entry.id) if "arxiv.org" in entry.id \
                else entry.id
            if key in seen:
                continue
            seen.add(key)
            all_entries.append(entry)
        time.sleep(1.0)

    report["entries"] = len(all_entries)
    return all_entries, report


# ── Semantic Scholar ───────────────────────────────────────────

def _s2_headers():
    key = config.SEMANTIC_SCHOLAR_API_KEY
    return {"x-api-key": key} if key else {}


def _parse_s2_items(items):
    """Convert Semantic Scholar paper objects into duck-typed entries."""
    entries = []
    for paper in items or []:
        if not isinstance(paper, dict):
            continue
        title = (paper.get("title") or "").strip()
        if not title:
            continue
        external = paper.get("externalIds") or {}
        raw_arxiv = str(external.get("ArXiv") or "")
        m = _ARXIV_ID_RE.search(raw_arxiv)
        arxiv_id = m.group(1) if m else ""
        paper_id = str(paper.get("paperId") or "")
        if arxiv_id:
            link = f"{config.ARXIV_ABS_BASE_URL.rstrip('/')}/abs/{arxiv_id}"
            entry_id = link
        elif paper.get("url"):
            link = paper["url"]
            entry_id = f"s2:{paper_id or link}"
        else:
            continue

        authors = []
        for a in paper.get("authors") or []:
            if isinstance(a, dict) and a.get("name"):
                authors.append(SimpleNamespace(name=a["name"]))
        published = (paper.get("publicationDate") or "")[:10]
        try:
            published_parsed = time.strptime(published, "%Y-%m-%d")
        except ValueError:
            published_parsed = None
        tldr = ""
        if isinstance(paper.get("tldr"), dict):
            tldr = (paper["tldr"].get("text") or "").strip()

        entry = SimpleNamespace(
            id=entry_id,
            title=title,
            summary=(paper.get("abstract") or tldr or "").strip(),
            link=link,
            arxiv_primary_category={"term": ""},
            author=authors[0].name if authors else "Unknown",
            authors=authors,
            published=paper.get("publicationDate"),
            updated=paper.get("publicationDate"),
            published_parsed=published_parsed,
            source="semantic-scholar",
            arxiv_comment="",
            s2_id=paper_id,
            s2_citations=paper.get("citationCount"),
            s2_influential=paper.get("influentialCitationCount"),
            s2_venue=(paper.get("venue") or "").strip(),
            s2_tldr=tldr,
            s2_fields=paper.get("fieldsOfStudy") or [],
        )
        entries.append(entry)
    return entries


def fetch_semantic_scholar(date_from, date_to):
    """Return ``(entries, report)`` for optional S2 discovery.  Never raises."""
    report = {"source": "semantic-scholar",
              "enabled": bool(config.ENABLE_SEMANTIC_SCHOLAR
                              and config.SEMANTIC_SCHOLAR_API_KEY),
              "queries": [], "entries": 0, "errors": []}
    if not report["enabled"]:
        report["reason"] = ("disabled: set SEMANTIC_SCHOLAR_API_KEY "
                            "to enable (anonymous API is often 429)")
        return [], report

    fields = ("title,abstract,authors,externalIds,publicationDate,venue,"
              "citationCount,influentialCitationCount,tldr,fieldsOfStudy,url")
    all_entries = []
    seen = set()

    for query in config.SEMANTIC_SCHOLAR_QUERIES:
        report["queries"].append(query)
        params = {
            "query": query,
            "limit": str(config.SEMANTIC_SCHOLAR_LIMIT),
            "fields": fields,
            "year": f"{date_from.year}-{date_to.year}",
        }
        url = (f"{config.SEMANTIC_SCHOLAR_API}/paper/search?"
               f"{urllib.parse.urlencode(params)}")
        data = None
        for attempt, delay in enumerate((0, 3, 8)):
            if delay:
                time.sleep(delay)
            data, status, error = _get_json(
                url, timeout=config.SEMANTIC_SCHOLAR_TIMEOUT,
                headers=_s2_headers())
            if data is not None or status != 429:
                break
        if data is None:
            report["errors"].append(f"{query}: {error or status}")
            continue

        for entry in _parse_s2_items(data.get("data") or []):
            published = (entry.published or "")[:10]
            if published and not (str(date_from) <= published <= str(date_to)):
                # S2 returns publication date, not arXiv submission date;
                # keep items with no date or inside the requested range.
                continue
            key = flt.base_arxiv_id(entry.id)
            if key in seen:
                continue
            seen.add(key)
            all_entries.append(entry)
        time.sleep(1.2)

    report["entries"] = len(all_entries)
    return all_entries, report


# ── Enrichment of selected papers ──────────────────────────────

def _cache_path():
    return config.SCRIPT_DIR / "enrich_cache.json"


def _load_cache():
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(cache):
    try:
        _cache_path().write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except OSError:
        pass


def _enrich_from_s2(papers, report):
    """One batched S2 request for all selected arXiv IDs."""
    if not (config.ENABLE_SEMANTIC_SCHOLAR and config.SEMANTIC_SCHOLAR_API_KEY):
        report["s2_enrichment"] = "disabled"
        return
    ids = []
    for p in papers:
        pid = flt.base_arxiv_id(p.get("link", ""))
        if pid:
            ids.append(f"arXiv:{pid}")
    ids = list(dict.fromkeys(ids))
    if not ids:
        return

    cache = _load_cache()
    now = time.time()
    missing = []
    for arxiv_id in ids:
        key = arxiv_id.split(":", 1)[1]
        cached = cache.get(key)
        if cached and now - cached.get("ts", 0) < _CACHE_TTL:
            _merge_s2(papers, key, cached.get("data") or {})
        else:
            missing.append(arxiv_id)

    if not missing:
        report["s2_enrichment"] = f"{len(ids)} cached"
        return

    fields = ("title,citationCount,influentialCitationCount,venue,tldr,"
              "externalIds")
    url = (f"{config.SEMANTIC_SCHOLAR_API}/paper/batch?"
           f"{urllib.parse.urlencode({'fields': fields})}")
    data = None
    for attempt, delay in enumerate((0, 3, 8)):
        if delay:
            time.sleep(delay)
        data, status, error = _post_json(
            url, {"ids": missing}, timeout=config.SEMANTIC_SCHOLAR_TIMEOUT,
            headers=_s2_headers())
        if data is not None or status != 429:
            break
    if data is None:
        report["s2_enrichment"] = f"failed: {error or status}"
        return

    for arxiv_id, item in zip(missing, data):
        if not isinstance(item, dict):
            continue
        key = arxiv_id.split(":", 1)[1]
        payload = {
            "citationCount": item.get("citationCount"),
            "influentialCitationCount": item.get("influentialCitationCount"),
            "venue": item.get("venue") or "",
            "tldr": (item.get("tldr") or {}).get("text", "")
            if isinstance(item.get("tldr"), dict) else "",
        }
        cache[key] = {"ts": now, "data": payload}
        _merge_s2(papers, key, payload)
    _save_cache(cache)
    report["s2_enrichment"] = f"{len(missing)} fetched"


def _merge_s2(papers, base_id, data):
    for p in papers:
        if flt.base_arxiv_id(p.get("link", "")) != base_id:
            continue
        if data.get("citationCount") is not None:
            p["citation_count"] = data["citationCount"]
        if data.get("influentialCitationCount") is not None:
            p["influential_citations"] = data["influentialCitationCount"]
        if data.get("venue"):
            p["venue"] = data["venue"]
        if data.get("tldr"):
            p["s2_tldr"] = data["tldr"]


def enrich_papers(papers, hf_entries=None, s2_entries=None,
                  openalex_entries=None, report=None):
    """Attach HF/S2/OpenAlex metadata to selected papers.  Never raises."""
    report = report if report is not None else {}
    hf_entries = hf_entries or []
    s2_entries = s2_entries or []
    openalex_entries = openalex_entries or []

    hf_index = {}
    for entry in hf_entries:
        hf_index[flt.base_arxiv_id(entry.id)] = entry
    s2_index = {}
    for entry in s2_entries:
        s2_index[flt.base_arxiv_id(entry.id)] = entry
    openalex_index = {}
    for entry in openalex_entries:
        openalex_index[flt.base_arxiv_id(entry.id)] = entry

    for p in papers:
        key = flt.base_arxiv_id(p.get("link", ""))
        hf = hf_index.get(key)
        if hf is not None:
            p["hf_upvotes"] = getattr(hf, "hf_upvotes", 0)
            p["hf_url"] = getattr(hf, "hf_url", "")
            p["hf_ai_summary"] = getattr(hf, "hf_ai_summary", "")
        s2 = s2_index.get(key)
        if s2 is not None:
            if getattr(s2, "s2_citations", None) is not None:
                p["citation_count"] = s2.s2_citations
            if getattr(s2, "s2_influential", None) is not None:
                p["influential_citations"] = s2.s2_influential
            if getattr(s2, "s2_venue", ""):
                p["venue"] = s2.s2_venue
            if getattr(s2, "s2_tldr", ""):
                p["s2_tldr"] = s2.s2_tldr
        oa = openalex_index.get(key)
        if oa is not None and p.get("citation_count") is None:
            if getattr(oa, "openalex_citations", None) is not None:
                p["citation_count"] = oa.openalex_citations
                p["citation_source"] = "OpenAlex"

    _enrich_from_s2(papers, report)


def collect(date_from, date_to, enabled=True):
    """Collect all optional sources.

    Returns a dict with ``entries`` (list of ``(source, [entries])``),
    ``hf_entries``, ``s2_entries``, ``openalex_entries`` and a JSON-safe
    ``report``.
    """
    out = {"entries": [], "hf_entries": [], "s2_entries": [],
           "openalex_entries": [], "report": {}}
    if not enabled:
        out["report"]["extra_sources"] = "disabled by CLI"
        return out

    hf_entries, hf_report = fetch_hf_daily(date_from, date_to)
    out["hf_entries"] = hf_entries
    out["report"]["huggingface"] = hf_report
    if hf_entries:
        out["entries"].append(("huggingface", hf_entries))

    oa_entries, oa_report = fetch_openalex(date_from, date_to)
    out["openalex_entries"] = oa_entries
    out["report"]["openalex"] = oa_report
    if oa_entries:
        out["entries"].append(("openalex", oa_entries))

    s2_entries, s2_report = fetch_semantic_scholar(date_from, date_to)
    out["s2_entries"] = s2_entries
    out["report"]["semantic_scholar"] = s2_report
    if s2_entries:
        out["entries"].append(("semantic-scholar", s2_entries))

    return out
