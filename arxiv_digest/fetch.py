#!/usr/bin/env python3
"""ArXiv 获取层：API 查询、失败分类、重试、以及可用的 HTML 回退。

设计原则
--------
1. **“没有论文”和“抓取失败”必须分开。** 旧实现把 429 / 超时 / 代理登录页
   都当成 0 条，最终生成空 digest，看起来像 arXiv 当天没有论文。
   现在每页返回 ``ok / empty / failed`` 三态；只有带 opensearch:totalResults
   的合法 Atom feed 才允许判定为 empty。
2. **失败要能回退。** export.arxiv.org 在校园网/共享出口 IP 上经常 429，
   但 arxiv.org 的 advanced-search HTML 仍然可用。HTML 回退使用真实日期
   范围、完整摘要、作者和主分类，不再拼一个 arXiv 不认识的
   ``cat:cs.NI AND submittedDate:...`` 查询。
3. **回退是主题驱动的。** 与其在限流时抓“全 arXiv”，不如按组里的兴趣
   查询（光网络、LLM inference、MoE、集合通信、RDMA）。这样既少请求，
   又不会把 digest 变成器件论文垃圾场。
4. **任何单页失败都不应该静默消失。** ``fetch_all(..., report=...)`` 会把
   每个分类的状态、失败原因、HTML 回退结果写进 report，主入口据此决定
   是“降级继续”还是“大声失败退出”。
"""

import html as html_lib
import os
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from types import SimpleNamespace

import feedparser

from arxiv_digest import config
from arxiv_digest import filter as flt

# ── Proxy bypass (no_proxy) for arXiv hosts ────────────────────
# Many proxy/VPN tools (Clash X, Surge, etc.) intercept export.arxiv.org
# and cause 429 rate-limits or timeouts.  We set no_proxy for these hosts
# so the OS proxy settings are bypassed for arXiv API/PDF requests.
_ARXIV_NO_PROXY = ",".join(config.ARXIV_PROXY_BYPASS_HOSTS)
_NO_PROXY_OLD = os.environ.get("no_proxy", "")
if config.ARXIV_BYPASS_PROXY:
    if _ARXIV_NO_PROXY not in _NO_PROXY_OLD:
        os.environ["no_proxy"] = (
            f"{_NO_PROXY_OLD},{_ARXIV_NO_PROXY}"
            if _NO_PROXY_OLD else _ARXIV_NO_PROXY
        )

# Page / fetch states.  Keep them as plain strings so reports stay JSON-safe.
PAGE_OK = "ok"
PAGE_EMPTY = "empty"
PAGE_FAILED = "failed"
PAGE_PARTIAL = "partial"

_STATE_RANK = {PAGE_OK: 0, PAGE_EMPTY: 0, PAGE_PARTIAL: 1, PAGE_FAILED: 2}

USER_AGENT = "arXivDailyDigest/2.0 (mailto:rawking1621@gmail.com)"


# ── Dates / URL construction ───────────────────────────────────

def _parse_date_arg(s):
    """'YYYY-MM-DD' → datetime.date。格式错误抛 ValueError。"""
    return datetime.strptime(s, "%Y-%m-%d").date()


def _iter_date_windows(d_from, d_to):
    """(from, to) 日期窗口生成器：新→旧，每个窗口最多 RANGE_WINDOW_DAYS 天。"""
    step = timedelta(days=config.RANGE_WINDOW_DAYS - 1)
    cur_end = d_to
    while cur_end >= d_from:
        cur_start = max(d_from, cur_end - step)
        yield cur_start, cur_end
        cur_end = cur_start - timedelta(days=1)


def _build_search_query(category, date_from, date_to):
    """构造某个分类在闭区间内的 arXiv API search_query。"""
    query = (f"cat:{category} AND submittedDate:"
             f"[{date_from:%Y%m%d}0000 TO {date_to:%Y%m%d}2359]")
    extra = config.CATEGORY_FILTERS.get(category, "")
    if extra:
        query = f"{query} AND {extra}"
    return query


def _build_category_url(category, date_from, date_to, start=0):
    """构造单个分类的查询 URL。start 是翻页偏移。"""
    params = {
        "search_query": _build_search_query(category, date_from, date_to),
        "start": str(start),
        "max_results": str(config.MAX_PER_CATEGORY),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    qs = urllib.parse.urlencode(params)
    base = config.ARXIV_API_BASE_URL.rstrip("/")
    return f"{base}/api/query?{qs}"


# ── Error / feed classification ────────────────────────────────

def _is_fatal_error(bozo_exception):
    """不可重试的错误（SSL 证书、DNS 解析）→ 重试是浪费时间。"""
    if bozo_exception is None:
        return False
    msgs = []
    ex = bozo_exception
    seen = set()
    while ex is not None and id(ex) not in seen:
        seen.add(id(ex))
        msgs.append(str(ex))
        msgs.append(type(ex).__name__)
        if hasattr(ex, 'reason') and not isinstance(ex.reason, type(ex)):
            if isinstance(ex.reason, str):
                msgs.append(ex.reason)
                break
            ex = ex.reason
        elif hasattr(ex, '__cause__') and ex.__cause__ is not None:
            ex = ex.__cause__
        else:
            break
    combined = ' '.join(msgs)
    if 'SSL' in combined or 'certificate verify failed' in combined.lower():
        return True
    if 'gaierror' in combined or 'errno 8' in combined.lower():
        return True
    return False


def _classify_status(feed):
    """返回 (status_int, bozo_exception, is_429, is_503, is_fatal)。"""
    bozo = getattr(feed, 'bozo_exception', None) if feed is not None else None
    status = getattr(feed, 'status', 'N/A') if feed is not None else 'N/A'
    status_int = int(status) if str(status).isdigit() else 0
    return (status_int, bozo, status_int == 429, status_int == 503,
            _is_fatal_error(bozo))


def _feed_total(feed):
    """arXiv feed 的总命中数（opensearch_totalresults），未知时为 None。"""
    try:
        return int(feed.feed.get('opensearch_totalresults'))
    except (AttributeError, TypeError, ValueError):
        return None


def _is_valid_api_feed(feed):
    """这到底是不是一个合法的 arXiv Atom feed？

    仅凭 ``status == 200 and bozo is None and not entries`` 不能说明“没有
    论文”：代理登录页、CDN 拦截页、空响应也可能解析成 0 条。合法 feed
    至少有 Atom 版本，并且有 title 或 opensearch:totalResults。
    """
    if feed is None:
        return False
    version = str(getattr(feed, "version", "") or "").lower()
    if "atom" not in version:
        return False
    meta = getattr(feed, "feed", {}) or {}
    return bool(meta.get("title")) or _feed_total(feed) is not None


def _is_arxiv_error_feed(feed):
    """Detect arXiv API error masquerading as HTTP 200 with one 'Error' entry."""
    if feed is None or not getattr(feed, 'entries', None):
        return None
    if len(feed.entries) != 1:
        return None
    entry = feed.entries[0]
    title = (getattr(entry, 'title', '') or '').strip()
    if title.lower() == 'error':
        summary = getattr(entry, 'summary', '')
        return (f"arXiv API rejected the query: {summary}" if summary else
                "arXiv API returned 'Error' entry (no details)")
    return None


def _page_outcome(feed, status_int, bozo):
    """把一次响应归类为 (state, detail)。"""
    error_msg = _is_arxiv_error_feed(feed)
    if error_msg:
        return PAGE_FAILED, error_msg
    if getattr(feed, 'entries', None):
        return PAGE_OK, None
    if (status_int == 200 and bozo is None and _is_valid_api_feed(feed)):
        total = _feed_total(feed)
        if total == 0:
            return PAGE_EMPTY, None
    if status_int == 0:
        return PAGE_FAILED, "connection/timeout (no HTTP status)"
    if status_int == 200:
        return PAGE_FAILED, "HTTP 200 but no usable entries/totalResults"
    return PAGE_FAILED, f"HTTP {status_int}"


# ── HTTP helpers ───────────────────────────────────────────────

def _fetch_bytes_no_proxy(url, timeout=None, accept=None):
    """Fetch URL bytes while bypassing the system/VPN proxy for arXiv hosts.

    Returns ``(data, status)``:
      - data = raw bytes for 2xx, else None
      - status = HTTP status int, or 0 on connection/timeout error
    """
    timeout = timeout or config.ARXIV_REQUEST_TIMEOUT
    status = 0
    if config.ARXIV_BYPASS_PROXY:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )
    else:
        opener = urllib.request.build_opener()

    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": accept or "application/atom+xml, application/xml, text/html, */*",
    })
    try:
        with opener.open(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode() or 0
            if 200 <= status < 300:
                return resp.read(), status
    except urllib.error.HTTPError as e:
        return None, e.code
    except Exception:
        return None, 0
    return None, status


def _feed_from_bytes(data):
    """Parse raw bytes with feedparser."""
    if not data:
        return None
    try:
        return feedparser.parse(data)
    except Exception:
        return None


def _do_fetch(url):
    """Single API attempt.  Returns the six-tuple used by the retry loop."""
    data, status_int = _fetch_bytes_no_proxy(url)
    feed = _feed_from_bytes(data)
    if feed is None:
        try:
            feed = feedparser.parse(url, agent=USER_AGENT)
        except Exception:
            feed = None
    return (feed, *_classify_status(feed))


def _prompt_retry(reason, wait_sec=None):
    """Ask the user whether to wait and retry.  Non-interactive returns False."""
    if wait_sec is None:
        wait_sec = 300 + random.randint(0, 60)
    mins, secs = wait_sec // 60, wait_sec % 60

    if not sys.stdin.isatty():
        print(f"\n  {reason}")
        print("  (non-interactive — skipping retry; use --wait for auto-retry)")
        return False

    print(f"\n{'─' * 55}")
    print(f"  {reason}")
    print(f"  Wait {mins}m{secs}s and retry automatically? [Y/n] ",
          end="", flush=True)
    try:
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if answer in ("", "y", "yes"):
        print(f"  Waiting {wait_sec}s...", end="", flush=True)
        for remaining in range(wait_sec, 0, -1):
            time.sleep(1)
            if remaining % 30 == 0:
                print(f"\n  ({remaining // 60}m{remaining % 60:02d}s "
                      f"remaining)...", end="", flush=True)
        print(" retrying!")
        return True
    print("  Skipping — run again later or use --wait for auto-retry.")
    return False


# ── API page/window retrieval ──────────────────────────────────

def _fetch_one_page(url, wait_on_429, label, rate_limit_retries=0):
    """Fetch one API page with retries.

    Returns ``(entries, total, state)`` where state is ok / empty / failed.
    ``empty`` is only returned for a valid feed with zero results; every
    ambiguous response is ``failed``.
    """
    prompted = False
    last_detail = ""

    for attempt in range(config.MAX_RETRIES + 1):
        feed, status_int, bozo, is_429, is_503, fatal = _do_fetch(url)
        state, detail = _page_outcome(feed, status_int, bozo)
        last_detail = detail or last_detail

        if state == PAGE_OK:
            suffix = " (after retry)" if attempt else ""
            print(f"  [{label}] ✓ {len(feed.entries)} entries{suffix}")
            return feed.entries, _feed_total(feed), PAGE_OK
        if state == PAGE_EMPTY:
            print(f"  [{label}] ✓ 0 entries (valid feed, no papers in window)")
            return [], _feed_total(feed), PAGE_EMPTY
        if detail and "rejected the query" in detail:
            print(f"\n  [{label}] ⛔ {detail}")
            print("         This is NOT a rate-limit — the query itself is invalid.")
            print(f"         URL: {url}")
            return [], None, PAGE_FAILED

        if is_429:
            wait_sec = 300 + random.randint(0, 60)
            if wait_on_429 and rate_limit_retries < config.MAX_429_RETRIES:
                print(f"\n  [{label}] ⛔ HTTP 429 rate-limit "
                      f"({rate_limit_retries + 1}/{config.MAX_429_RETRIES}). "
                      f"Waiting {wait_sec // 60}min then retrying...")
                time.sleep(wait_sec)
                rate_limit_retries += 1
                continue
            if not wait_on_429 and not prompted and _prompt_retry(
                    f"[{label}] arXiv is rate-limiting this IP (HTTP 429).",
                    wait_sec):
                prompted = True
                continue
            print(f"  [{label}] ⛔ HTTP 429 — giving up on this page.")
            return [], None, PAGE_FAILED

        if fatal:
            print(f"  [{label}] ⛔ [FATAL] non-retryable error: "
                  f"{str(bozo)[:180]}")
            return [], None, PAGE_FAILED

        if status_int == 0 and attempt >= 1:
            # A blackholed host costs a full timeout per attempt.  One
            # retry is enough before handing over to the HTML fallback.
            break
        if attempt >= config.MAX_RETRIES:
            break

        base = (config.RETRY_BACKOFF_503_BASE if is_503
                else config.RETRY_BACKOFF_BASE)
        delay = base[min(attempt, len(base) - 1)] * (0.75 + random.random() * 0.5)
        if status_int == 0:
            delay = min(delay, 5.0)
        reason = (f"HTTP {status_int}" if status_int else "connection error")
        print(f"  [{label}] [retry] {reason} — "
              f"attempt {attempt + 2}/{config.MAX_RETRIES + 1}, "
              f"waiting {delay:.1f}s...")
        time.sleep(delay)

    print(f"  [{label}] ⛔ {last_detail or 'no usable response'} — "
          f"giving up after {config.MAX_RETRIES + 1} attempt(s).")
    return [], None, PAGE_FAILED


def _fetch_window(category, date_from, date_to, wait_on_429):
    """拉取单个分类在 [date_from, date_to] 窗口内的全部论文（自动翻页）。

    Returns ``(entries, meta)``; meta["state"] is ok / empty / partial / failed.
    """
    all_entries = []
    total = None
    state = PAGE_FAILED
    pages = 0

    for page in range(config.MAX_PAGES):
        url = _build_category_url(category, date_from, date_to,
                                  start=len(all_entries))
        entries, total, page_state = _fetch_one_page(
            url, wait_on_429=wait_on_429, label=category)
        pages = page + 1
        if page_state == PAGE_FAILED:
            state = PAGE_PARTIAL if all_entries else PAGE_FAILED
            break
        if page_state == PAGE_EMPTY or not entries:
            state = PAGE_OK if all_entries else PAGE_EMPTY
            break

        all_entries.extend(entries)
        state = PAGE_OK
        if (total is not None and len(all_entries) >= total) or \
                len(entries) < config.MAX_PER_CATEGORY:
            break

        delay = config.API_DELAY * (0.75 + random.random() * 0.5)
        print(f"  [{category}] [next page] waiting {delay:.1f}s...")
        time.sleep(delay)
    else:
        if total is None or len(all_entries) < total:
            state = PAGE_PARTIAL
            print(f"  [{category}] ⚠ page cap reached ({config.MAX_PAGES} "
                  f"pages) — got {len(all_entries)} entries")

    return all_entries, {"state": state, "pages": pages, "total": total}


# ── HTML advanced-search fallback ──────────────────────────────

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}


def _clean_fragment(fragment):
    """Strip tags / entities / collapsed whitespace from an HTML fragment."""
    if not fragment:
        return ""
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = html_lib.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _parse_html_results(page_html, fallback_date=None):
    """Parse arxiv.org/search/advanced result blocks into entry objects.

    Kept separate from the HTTP code so it can be tested against a fixture.
    """
    entries = []
    chunks = re.split(r'<li class="arxiv-result">', page_html)[1:]
    for chunk in chunks:
        block = chunk.split('<li class="arxiv-result">')[0]
        id_match = re.search(r'arxiv\.org/abs/(\d{4}\.\d{4,5})(?:v\d+)?', block)
        if not id_match:
            id_match = re.search(r'arXiv:(\d{4}\.\d{4,5})(?:v\d+)?', block)
        if not id_match:
            continue
        paper_id = id_match.group(1)

        title_match = re.search(
            r'<p class="title is-5 mathjax">(.*?)</p>', block, re.DOTALL)
        title = _clean_fragment(title_match.group(1)) if title_match else "Untitled"

        summary = ""
        full_match = re.search(
            r'<span class="abstract-full[^"]*"[^>]*>(.*?)(?:<a class="is-size-7"|</p>)',
            block, re.DOTALL)
        if full_match:
            summary = _clean_fragment(full_match.group(1))
        if not summary:
            short_match = re.search(
                r'<span class="abstract-short[^"]*"[^>]*>(.*?)(?:<a class="is-size-7"|</span>)',
                block, re.DOTALL)
            if short_match:
                summary = _clean_fragment(short_match.group(1))
        summary = re.sub(r'^Abstract:\s*', '', summary, flags=re.IGNORECASE)

        authors = []
        authors_match = re.search(
            r'<p class="authors">(.*?)</p>', block, re.DOTALL)
        if authors_match:
            authors = [_clean_fragment(a) for a in re.findall(
                r'<a [^>]*>(.*?)</a>', authors_match.group(1), re.DOTALL)]
            authors = [a for a in authors if a]

        category = ""
        cat_match = re.search(
            r'<span class="tag[^"]*"[^>]*>\s*([A-Za-z-]+(?:\.[A-Za-z-]+)?)\s*</span>',
            block)
        if cat_match:
            category = cat_match.group(1).strip()

        published = fallback_date
        sub_match = re.search(
            r'Submitted</span>\s*(\d{1,2})\s+([A-Za-z]+),?\s+(\d{4})',
            block, re.DOTALL)
        if sub_match:
            month = _MONTHS.get(sub_match.group(2).lower())
            if month:
                try:
                    published = datetime(int(sub_match.group(3)), month,
                                         int(sub_match.group(1))).date()
                except ValueError:
                    pass
        published_parsed = None
        if published:
            published_parsed = time.strptime(str(published), "%Y-%m-%d")

        link = f"{config.ARXIV_ABS_BASE_URL.rstrip('/')}/abs/{paper_id}"
        author_objs = [SimpleNamespace(name=a) for a in authors]
        entry = _HtmlEntry(
            id=link,
            title=title,
            summary=summary,
            link=link,
            arxiv_primary_category={"term": category},
            author=authors[0] if authors else "Unknown",
            authors=author_objs,
            published=(str(published) + "T00:00:00Z") if published else None,
            updated=(str(published) + "T00:00:00Z") if published else None,
            published_parsed=published_parsed,
        )
        entry.source = "arxiv-html"
        entry.arxiv_comment = ""
        entries.append(entry)
    return entries


class _HtmlEntry:
    """Duck-typed entry that mimics the feedparser attributes we consume."""

    def __init__(self, id=None, title=None, summary=None, link=None,
                 arxiv_primary_category=None, author=None, authors=None,
                 published=None, updated=None, published_parsed=None):
        self.id = id
        self.title = title
        self.summary = summary
        self.link = link
        self.arxiv_primary_category = arxiv_primary_category or {"term": ""}
        self.author = author
        self.authors = authors or []
        self.published = published
        self.updated = updated
        self.published_parsed = published_parsed
        self.source = "arxiv-html"
        self.arxiv_comment = ""

    def get(self, key, default=None):
        return getattr(self, key, default)

    def __getitem__(self, key):
        return getattr(self, key)

    def __repr__(self):
        return f"<HtmlEntry id={self.id!r} title={self.title!r}>"


def _search_html_query(query, date_from, date_to, max_results=50):
    """Run one advanced-search query and parse its result page(s).

    Returns ``(entries, state, detail)``.  This is the real fallback: the
    old code used ``/search/?query=("cs.NI") AND submittedDate:[...]``,
    which arXiv rejects with "produced no results" for every category.
    """
    entries = []
    seen = set()
    page_size = min(config.ARXIV_HTML_PAGE_SIZE, max_results)
    any_ok = False
    last_detail = ""

    for start in range(0, max_results, page_size):
        params = {
            "advanced": "",
            "terms-0-operator": "AND",
            "terms-0-term": query,
            "terms-0-field": "all",
            "classification-include_cross_list": "include",
            "date-filter_by": "date_range",
            "date-from_date": f"{date_from:%Y-%m-%d}",
            "date-to_date": f"{date_to:%Y-%m-%d}",
            "date-date_type": "submitted_date",
            "abstracts": "show",
            "size": str(page_size),
            "start": str(start),
            "order": "-announced_date_first",
        }
        url = (f"{config.ARXIV_LIST_BASE_URL.rstrip('/')}/search/advanced?"
               f"{urllib.parse.urlencode(params)}")
        data, status = _fetch_bytes_no_proxy(
            url, timeout=config.ARXIV_REQUEST_TIMEOUT,
            accept="text/html, */*")
        if data is None:
            last_detail = f"HTTP {status or 'timeout'}"
            return entries, (PAGE_PARTIAL if entries else PAGE_FAILED), last_detail

        page_html = data.decode("utf-8", errors="replace")
        lowered = page_html.lower()
        if "rate exceeded" in lowered or "too many requests" in lowered:
            last_detail = "HTTP 429 (HTML search)"
            return entries, (PAGE_PARTIAL if entries else PAGE_FAILED), last_detail
        if "sorry, your query for" in lowered and "produced no results" in lowered:
            return entries, (PAGE_OK if any_ok else PAGE_EMPTY), None

        parsed = _parse_html_results(page_html, fallback_date=date_to)
        if not parsed:
            last_detail = "HTML page had no parseable arxiv-result blocks"
            return entries, (PAGE_PARTIAL if entries else PAGE_FAILED), last_detail
        any_ok = True
        new = 0
        for entry in parsed:
            pid = flt.normalize_arxiv_id(entry.id)
            if pid in seen:
                continue
            seen.add(pid)
            entries.append(entry)
            new += 1

        if new == 0 or len(parsed) < page_size:
            break
        time.sleep(1.2 + random.random() * 0.8)

    return entries, (PAGE_OK if any_ok else PAGE_EMPTY), last_detail


def _search_html_topics(date_from, date_to):
    """Fetch topic-focused results from arxiv.org advanced search.

    Returns ``(entries, meta)``.  Deduplicates across queries, caps the
    total, and never raises: a broken fallback must not hide the fact that
    the API failed.
    """
    all_entries = []
    seen = set()
    ok_queries = 0
    failed_queries = 0
    consecutive_failures = 0
    for query in config.ARXIV_HTML_FALLBACK_QUERIES:
        print(f"  [html] searching {query} ...")
        entries, state, detail = _search_html_query(
            query, date_from, date_to,
            max_results=min(config.ARXIV_HTML_MAX_RESULTS, 50))
        if state in (PAGE_OK, PAGE_EMPTY):
            ok_queries += 1
            consecutive_failures = 0
        else:
            failed_queries += 1
            consecutive_failures += 1
            if detail:
                print(f"  [html] {query}: {detail}")
        for entry in entries:
            pid = flt.normalize_arxiv_id(entry.id)
            if pid in seen:
                continue
            seen.add(pid)
            all_entries.append(entry)
        if len(all_entries) >= config.ARXIV_HTML_MAX_RESULTS:
            break
        if consecutive_failures >= config.ARXIV_HTML_BREAKER_AFTER:
            print(f"  [html] {consecutive_failures} queries failed in a row — "
                  f"stopping HTML fallback.")
            break
        time.sleep(1.2 + random.random() * 0.8)

    if ok_queries == 0:
        state = PAGE_FAILED
    elif failed_queries and all_entries:
        state = PAGE_PARTIAL
    elif all_entries:
        state = PAGE_OK
    else:
        state = PAGE_EMPTY
    print(f"  [html] {len(all_entries)} entries "
          f"({ok_queries} queries ok, {failed_queries} failed)")
    return all_entries, {
        "state": state,
        "entries": len(all_entries),
        "queries_ok": ok_queries,
        "queries_failed": failed_queries,
    }


# ── Public fetch API ───────────────────────────────────────────

def fetch_all(wait_on_429=False, date_from=None, date_to=None, report=None):
    """拉取 config.CATEGORIES 的论文，返回 (entries_by_category, stats)。

    date_from/date_to: 'YYYY-MM-DD'（闭区间）。缺省时拉最近 3 天，并附加
    一个回看窗口（LOOKBACK_FROM_DAYS_AGO ~ LOOKBACK_TO_DAYS_AGO）用于
    carry-over；给定区间时不附加回看窗口。

    report: optional dict, filled with per-category health and fallback info.
    """
    report = report if report is not None else {}
    report.update({
        "date_from": None, "date_to": None, "windows": [],
        "categories": {}, "failed_categories": [], "ok_categories": [],
        "html_fallback": None, "total_entries": 0, "fatal": False,
        "degraded": False,
    })

    # ── Decide windows ────────────────────────────────────────
    if date_from or date_to:
        try:
            d_to = _parse_date_arg(date_to) if date_to else config.bj_now().date()
            d_from = _parse_date_arg(date_from) if date_from else d_to
        except ValueError:
            print("[error] 日期格式应为 YYYY-MM-DD")
            report["fatal"] = True
            report["error"] = "invalid date"
            return _empty_result()
        if d_from > d_to:
            print("[error] --from 不能晚于 --to")
            report["fatal"] = True
            report["error"] = "date_from > date_to"
            return _empty_result()
        windows = list(_iter_date_windows(d_from, d_to))
    else:
        d_to = config.bj_now().date()
        d_from = d_to - timedelta(days=3)
        windows = [(d_from, d_to)]
        if config.DATE_OVERRIDE is None:
            lb_to = d_to - timedelta(days=config.LOOKBACK_TO_DAYS_AGO)
            lb_from = d_to - timedelta(days=config.LOOKBACK_FROM_DAYS_AGO)
            if lb_from <= lb_to:
                windows.append((lb_from, lb_to))

    report["date_from"] = str(d_from)
    report["date_to"] = str(d_to)
    report["windows"] = [[str(a), str(b)] for a, b in windows]
    print(f"  Plan: {len(windows)} window(s), {d_from} → {d_to} "
          f"across {len(config.CATEGORIES)} categories")
    if len(windows) > 1:
        print("  (daily window + lookback for carry-over)")

    # Small jitter; avoids hitting the API at the exact same second every day.
    time.sleep(random.uniform(0.5, min(2.0, config.API_DELAY)))

    entries_by_cat = {cat: [] for cat in config.CATEGORIES}
    for cat in config.CATEGORIES:
        report["categories"].setdefault(cat, {
            "state": None, "pages": 0, "total": None,
            "windows": [], "entries": 0,
        })
    seen_ids = set()
    throttle = {"delay": config.API_DELAY, "strikes": 0}
    first_request = True
    api_breaker = False
    consecutive_failures = 0

    for w_from, w_to in windows:
        print(f"\n  [window] {w_from} → {w_to}")
        failed_this_window = []
        for cat in config.CATEGORIES:
            cat_report = report["categories"][cat]
            if api_breaker:
                cat_report["state"] = PAGE_FAILED
                cat_report["windows"].append(
                    {"from": str(w_from), "to": str(w_to),
                     "state": "skipped-after-failure", "entries": 0})
                failed_this_window.append((cat, "skipped"))
                continue

            if not first_request:
                delay = throttle["delay"] * (0.75 + random.random() * 0.5)
                time.sleep(delay)
            first_request = False

            win_entries, meta = _fetch_window(cat, w_from, w_to, wait_on_429)
            if (cat_report["state"] is None
                    or _STATE_RANK[meta["state"]]
                    > _STATE_RANK.get(cat_report["state"], 2)):
                cat_report["state"] = meta["state"]
            cat_report["pages"] += meta["pages"]
            cat_report["total"] = meta["total"]
            cat_report["windows"].append(
                {"from": str(w_from), "to": str(w_to),
                 "state": meta["state"], "entries": len(win_entries)})

            if meta["state"] in (PAGE_FAILED, PAGE_PARTIAL):
                failed_this_window.append((cat, meta["state"]))
                consecutive_failures += 1
            else:
                consecutive_failures = 0
            if win_entries:
                throttle["strikes"] = max(0, throttle["strikes"] - 1)
            else:
                throttle["strikes"] += 1
            throttle["delay"] = min(
                60.0, config.API_DELAY * (1.5 ** throttle["strikes"]))

            for e in win_entries:
                eid = flt.normalize_arxiv_id(e.id)
                if eid not in seen_ids:
                    seen_ids.add(eid)
                    entries_by_cat[cat].append(e)
                    cat_report["entries"] += 1

            if (consecutive_failures >= config.ARXIV_BREAKER_AFTER
                    and not api_breaker):
                api_breaker = True
                print(f"\n  [breaker] {consecutive_failures} categories failed "
                      f"in a row — skipping the rest of the API calls and "
                      f"using HTML fallback for this run.")

        # ── HTML fallback: once per window, topic-driven ──────
        if failed_this_window and config.ARXIV_HTML_FALLBACK:
            names = ", ".join(c for c, _ in failed_this_window)
            fb_from = w_from - timedelta(
                days=config.ARXIV_HTML_LOOKBACK_DAYS)
            print(f"\n  [html] API failed for {names}; using arxiv.org "
                  f"advanced search as fallback ({fb_from} → {w_to})...")
            html_entries, html_meta = _search_html_topics(fb_from, w_to)
            report["html_fallback"] = report.get("html_fallback") or []
            report["html_fallback"].append({
                "window": [str(fb_from), str(w_to)], **html_meta,
            })
            if html_entries:
                bucket = entries_by_cat.setdefault("arxiv.html", [])
                failed_cats = {c for c, _ in failed_this_window
                               if c in config.CATEGORIES}
                all_failed = len(failed_cats) == len(config.CATEGORIES)
                used = 0
                for e in html_entries:
                    # Only use fallback results for categories that actually
                    # failed.  Otherwise a failed cs.ET lookup would inject
                    # cs.AR/cs.LG papers into an exact-date digest.  If every
                    # category failed, keep all topic hits (including
                    # cross-lists outside config.CATEGORIES).
                    primary = flt.extract_primary_category(e)
                    if (not all_failed and failed_cats and primary
                            and primary not in failed_cats):
                        continue
                    eid = flt.normalize_arxiv_id(e.id)
                    if eid not in seen_ids:
                        seen_ids.add(eid)
                        bucket.append(e)
                        used += 1
                report["html_fallback"][-1]["used"] = used

    # ── Health summary ────────────────────────────────────────
    for cat in config.CATEGORIES:
        state = report["categories"][cat]["state"]
        if state in (PAGE_OK, PAGE_EMPTY):
            report["ok_categories"].append(cat)
        else:
            report["failed_categories"].append(cat)

    total_entries = sum(len(v) for v in entries_by_cat.values())
    report["total_entries"] = total_entries
    report["degraded"] = bool(report["failed_categories"])
    report["fatal"] = (not report["ok_categories"]
                       and not any(entries_by_cat.values()))

    print(f"\n  Summary: {len(report['ok_categories'])}/"
          f"{len(config.CATEGORIES)} categories healthy, "
          f"{total_entries} entries (deduped)")
    if report["failed_categories"]:
        print(f"  Failed/partial: {', '.join(report['failed_categories'])}")
    if report["html_fallback"]:
        n = sum(w.get("used", w.get("entries", 0))
                for w in report["html_fallback"])
        print(f"  HTML fallback recovered {n} entries")

    return _build_result(entries_by_cat)


def _empty_result():
    """无数据时的空结果。"""
    return ([(cat, []) for cat in config.CATEGORIES],
            {cat: {"total": 0, "skipped": 0, "already_seen": 0,
                   "selected": 0, "ocs_selected": 0}
             for cat in config.CATEGORIES})


def _build_result(entries_by_cat):
    """将 {cat: [entries]} 转为 (entries_by_category, stats)，保留额外源。"""
    entries_by_category = []
    stats = {}
    cats = list(config.CATEGORIES) + [
        c for c in entries_by_cat if c not in config.CATEGORIES]
    for cat in cats:
        cat_entries = entries_by_cat.get(cat, [])
        entries_by_category.append((cat, cat_entries))
        stats[cat] = {"total": len(cat_entries), "skipped": 0,
                      "already_seen": 0, "selected": 0, "ocs_selected": 0}
    return entries_by_category, stats
