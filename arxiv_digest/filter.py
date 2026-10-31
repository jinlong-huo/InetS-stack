#!/usr/bin/env python3
"""论文过滤：文本清洗、关键词打分（主 + OCS）、作者/年份提取、ID 标准化。

打分不是简单的关键词相加：
  * 标题命中 > 摘要命中（TITLE_BOOST）
  * 同一概念的同义词族只计一次（避免 MoE / KV-cache 术语堆分）
  * 综述/教程降权（NEGATIVE_KEYWORDS）
  * “we propose / novel / prototype”等贡献措辞给小额加分（封顶）
  * OCS 分成 software / neutral / hardware 三路，硬件词降权
"""

import re

from arxiv_digest import config


def clean_text(text):
    """归一化文本：合并空白、转小写。"""
    text = re.sub(r"\s+", " ", text)
    return text.lower()


def normalize_arxiv_id(raw_id):
    """从 oai: 或 http:// 格式中提取规范 arXiv ID（如 '2606.16943v1'）。"""
    m = re.search(r"(\d{4}\.\d{4,}(?:v\d+)?)", raw_id or "")
    if m:
        return m.group(1)
    return raw_id or ""


def base_arxiv_id(raw_id):
    """去掉版本号，用于跨源匹配（HF / S2 / arXiv 的 v1/v2 对齐）。"""
    return re.sub(r"v\d+$", "", normalize_arxiv_id(raw_id))


def valid_arxiv_id(raw_id):
    """arXiv 新式 ID 是 YYMM.NNNN 或 YYMM.NNNNN；拒绝 OpenAlex 误解析的长 ID。"""
    return bool(re.match(r"^\d{4}\.\d{4,5}(?:v\d+)?$",
                         normalize_arxiv_id(raw_id)))


def extract_authors(entry):
    """返回作者名列表；兼容 feedparser、HTML 回退和额外源。"""
    authors = getattr(entry, "authors", None) or []
    names = []
    for author in authors:
        if isinstance(author, dict):
            name = author.get("name") or author.get("fullname")
        else:
            name = getattr(author, "name", None)
        if name:
            names.append(str(name).strip())
    if not names:
        raw = getattr(entry, "author", "") or ""
        if raw:
            names = [n.strip() for n in re.split(r",\s*(?=[A-Z])", raw) if n.strip()]
    return names


def extract_author_year(entry):
    """从 entry 提取 (第一作者姓氏, 年份)。"""
    names = extract_authors(entry)
    last_name = names[0].split()[-1] if names else "Unknown"
    year = str(config.bj_now().year)
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        year = str(entry.published_parsed.tm_year)
    return last_name, year


def extract_primary_category(entry):
    """主分类（cs.NI / physics.optics / ...），未知时返回空串。"""
    primary = getattr(entry, "arxiv_primary_category", None)
    if isinstance(primary, dict):
        term = primary.get("term") or primary.get("label")
        if term:
            return str(term).strip()
    tags = getattr(entry, "tags", None) or []
    if tags:
        term = getattr(tags[0], "term", None)
        if term:
            return str(term).strip()
    return ""


def extract_published(entry):
    """提交日期 YYYY-MM-DD（HTML 回退 / API / 额外源都兼容）。"""
    parsed = getattr(entry, "published_parsed", None)
    if parsed:
        try:
            return f"{parsed.tm_year:04d}-{parsed.tm_mon:02d}-{parsed.tm_mday:02d}"
        except (AttributeError, TypeError):
            pass
    raw = str(getattr(entry, "published", "") or "")
    m = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
    return m.group(1) if m else ""



def _kw_match(kw, text):
    """词边界匹配，允许简单复数 — 'optical network' 也匹配 'optical networks'。"""
    return bool(re.search(r'\b' + re.escape(kw) + r'(?:s|es)?\b', text))


def _has_context(text, required):
    """文本是否包含任一上下文词（用于易冲突缩写的门控匹配）。"""
    return any(w in text for w in required)


def _family_index(families):
    return {member: fam for fam, members in families.items() for member in members}


_MAIN_FAMILY_INDEX = _family_index(config.MAIN_KEYWORD_FAMILIES)
_OCS_FAMILY_INDEX = _family_index(config.OCS_KEYWORD_FAMILIES)


def _boosted(weight, in_title):
    """标题命中额外加分；负权重（惩罚）不加成。"""
    if in_title and weight > 0:
        return weight + max(1, int(round(weight * config.TITLE_BOOST)))
    return weight


def _collect_hits(sources, text, title_text, family_index):
    """收集关键词命中并按同义词族去重。

    sources: [(terms_dict, kind)]，同一关键词只处理一次。
    返回 [(keyword, weight, kind, in_title)]，按权重降序。
    """
    raw = []
    seen = set()
    for terms, kind in sources:
        for kw, base in terms.items():
            if kw in seen:
                continue
            seen.add(kw)
            if not _kw_match(kw, text):
                continue
            in_title = _kw_match(kw, title_text)
            raw.append([kw, _boosted(base, in_title), kind, in_title])

    kept = []
    best_family = {}
    for item in raw:
        kw, weight, _, _ = item
        fam = family_index.get(kw)
        if fam is None:
            kept.append(item)
        elif fam not in best_family or weight > best_family[fam][1]:
            best_family[fam] = item

    kept.extend(best_family.values())
    kept.sort(key=lambda x: x[1], reverse=True)
    return kept


def _negative_score(text, table):
    return sum(value for kw, value in table.items() if _kw_match(kw, text))


def _innovation_score(text):
    """贡献措辞加分；返回 (bonus, matched_keywords)。"""
    matched = [(kw, value) for kw, value in config.INNOVATION_KEYWORDS.items()
               if _kw_match(kw, text)]
    matched.sort(key=lambda x: x[1], reverse=True)
    bonus = min(sum(value for _, value in matched), config.INNOVATION_BONUS_CAP)
    return bonus, [kw for kw, _ in matched]


def _detail(score, matched, max_single=0, **extra):
    out = {
        "score": max(0, int(score)),
        "matched": matched,
        "max_single": int(max_single),
    }
    out.update(extra)
    return out


def _hit_details(hits):
    """可读的命中证据：[{keyword, weight, in_title}]，供 digest 的 Why 行。"""
    return [{"keyword": item[0], "weight": int(item[1]),
             "in_title": bool(item[3])} for item in hits]


def score_main_detail(title, summary):
    """主过滤器完整打分。返回 dict（score/core_score/rank_score/...）。"""
    text = clean_text(title + " " + summary)
    title_text = clean_text(title)

    sources = [(config.KEYWORDS, "main")]
    if _has_context(text, config.MAIN_CONTEXT_REQUIRED):
        sources.append((config.MAIN_CONTEXT_KEYWORDS, "main"))

    hits = _collect_hits(sources, text, title_text, _MAIN_FAMILY_INDEX)
    base_score = sum(item[1] for item in hits)
    max_single = max((item[1] for item in hits), default=0)

    innovation, innovation_matched = _innovation_score(text)
    negative = _negative_score(text, config.NEGATIVE_KEYWORDS)

    score = max(0, base_score + innovation + negative)
    title_score = sum(item[1] for item in hits if item[3])
    core_anchor = any(_kw_match(kw, title_text) for kw in config.CORE_MAIN_KEYWORDS)
    core_score = max(0, title_score + innovation + negative) if core_anchor else 0
    rank_score = score + config.CORE_RANK_WEIGHT * core_score

    return _detail(
        score,
        [item[0] for item in hits],
        max_single=max_single,
        base_score=base_score,
        title_score=title_score,
        core_score=core_score,
        rank_score=rank_score,
        innovation_bonus=innovation,
        innovation_matched=innovation_matched,
        negative_score=negative,
        core_anchor=core_anchor,
        matched_detail=_hit_details(hits),
        core_pick=(core_anchor and core_score >= config.CORE_PICK_MIN
                   and score >= config.MIN_SCORE),
    )


def score_main(title, summary):
    """主关键词打分。返回 (score, max_single, matched_keywords)。"""
    detail = score_main_detail(title, summary)
    return detail["score"], detail["max_single"], detail["matched"]


def _ocs_kind(keyword):
    if keyword in config.OCS_SOFTWARE_TERMS:
        return "software"
    if keyword in config.OCS_HARDWARE_TERMS:
        return "hardware"
    return "neutral"


def score_ocs_detail(title, summary):
    """OCS 过滤器完整打分：software 全额、neutral 全额、hardware 降权。

    返回 dict（score/software_score/hardware_score/focus/rank_score/...）。
    """
    text = clean_text(title + " " + summary)
    title_text = clean_text(title)

    sources = []
    for kind in ("neutral", "software", "hardware"):
        terms = {kw: value for kw, value in config.OCS_KEYWORDS.items()
                 if _ocs_kind(kw) == kind}
        if terms:
            sources.append((terms, kind))
    if _has_context(text, config.OCS_CONTEXT_REQUIRED):
        # cpo/lpo/npo：已是光通信语境，全额计分
        sources.append((config.OCS_CONTEXT_KEYWORDS, "neutral"))
    sources.append((config.OCS_HARDWARE_KEYWORDS, "hardware"))

    # 先看有没有真正的 OCS 词汇；没有则不加通用的 software 词，
    # 否则 "agent / scheduling / optimization" 会把非光论文拉进 OCS 板块。
    base_hits = _collect_hits(sources, text, title_text, _OCS_FAMILY_INDEX)
    if base_hits and _has_context(text, config.OCS_SOFTWARE_CONTEXT_REQUIRED):
        sources.append((config.OCS_SOFTWARE_KEYWORDS, "software"))
    hits = _collect_hits(sources, text, title_text, _OCS_FAMILY_INDEX)
    software = sum(item[1] for item in hits if item[2] == "software")
    hardware = sum(item[1] for item in hits if item[2] == "hardware")
    neutral = sum(item[1] for item in hits if item[2] == "neutral")
    negative = _negative_score(text, config.OCS_NEGATIVE_KEYWORDS)

    raw_score = neutral + software + hardware * config.OCS_HARDWARE_WEIGHT + negative
    score = max(0, int(round(raw_score)))
    if software and hardware:
        # 硬件信号更强时按 hardware 处理（受配额限制），
        # 只有软件信号确实占优才保留为 mixed。
        focus = "mixed" if software > hardware else "hardware"
    elif software:
        focus = "software"
    elif hardware:
        focus = "hardware"
    else:
        focus = "neutral"

    max_single = max((item[1] for item in hits), default=0)
    return _detail(
        score,
        [item[0] for item in hits],
        max_single=max_single,
        software_score=int(round(software)),
        hardware_score=int(round(hardware)),
        neutral_score=int(round(neutral)),
        negative_score=negative,
        focus=focus,
        rank_score=score,
        matched_detail=_hit_details(hits),
    )


def score_ocs(title, summary):
    """OCS / 光交换 关键词打分。返回 (score, matched_keywords)。"""
    detail = score_ocs_detail(title, summary)
    return detail["score"], detail["matched"]


# ── 零关键词建议 ───────────────────────────────────────────────

STOP_WORDS = {
    "a", "an", "the", "and", "or", "of", "in", "on", "to", "for",
    "with", "by", "from", "at", "as", "is", "it", "its", "be",
    "are", "was", "were", "been", "not", "no", "but", "this",
    "that", "these", "those", "can", "will", "may", "has", "have",
    "had", "all", "each", "every", "both", "few", "more", "most",
    "other", "some", "such", "only", "over", "into", "up", "out",
    "if", "then", "so", "than", "too", "very", "just", "about",
    "also", "new", "use", "used", "using", "one", "two", "via",
    "based", "towards", "toward", "we", "you", "he", "she", "they",
    "do", "does", "did", "which", "what", "when", "where", "who",
    "how", "after", "before", "between", "under", "above", "below",
    "through", "during", "while", "though", "although", "without",
    "within", "along", "among", "across", "around", "beyond",
    "enable", "enables", "enabled", "enabling", "well",
}


def suggest_keywords(no_kw_papers, top_n=15):
    """从零关键词论文标题提取高频 bigram，建议新增关键词。"""
    from collections import Counter

    bigrams = Counter()
    for p in no_kw_papers:
        words = clean_text(p["title"]).split()
        # 过滤纯标点 / 单字符 / 停用词
        words = [w.strip("(),;:!?.\"'-") for w in words]
        words = [w for w in words if len(w) > 1 and w not in STOP_WORDS]
        for i in range(len(words) - 1):
            bg = words[i] + " " + words[i + 1]
            bigrams[bg] += 1

    # 取频率 ≥ 3 的 bigram
    candidates = [(bg, cnt) for bg, cnt in bigrams.most_common(top_n * 3) if cnt >= 3]
    return candidates[:top_n]
