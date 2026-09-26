"""MDA page / paragraph / sentence structuring for V15.

Consumes the already-bounded MDA section from V14.  It NEVER rediscovers the MDA
start/end boundary, so classroom/content-unit rules cannot override the validated
structural extractor.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

MDA_CANONICAL = "Management Discussion & Analysis"

SECTION_PATTERNS = [
    ("Economic Environment", [r"economic overview", r"economic environment", r"global economy", r"indian economy", r"macroeconomic"]),
    ("Industry Overview", [r"industry overview", r"industry scenario", r"industry outlook", r"market overview", r"industry trends"]),
    ("Business Overview", [r"business overview", r"business review", r"business model"]),
    ("Business Performance", [r"business performance", r"operational performance", r"operating performance"]),
    ("Financial Performance", [r"financial performance", r"financial review", r"financial highlights", r"results of operations"]),
    ("Risk & Risk Management", [r"risk management", r"risks and concerns", r"principal risks", r"key risks"]),
    ("Internal Controls", [r"internal controls?", r"internal financial controls?", r"internal audit"]),
    ("Human Capital", [r"human resources", r"human capital", r"employee engagement", r"people and culture"]),
    ("Outlook", [r"outlook", r"business outlook", r"future outlook", r"way forward"]),
]

_BULLET_RE = re.compile(r"^\s*(?:[•●▪◦‣·\-–—]|\(?[a-zA-Z0-9ivxIVX]{1,4}[.)])\s+")
_NUMBERING_RE = re.compile(r"^\s*(?:section\s+)?(?:\d+(?:\.\d+)*|[ivxlcdm]+)[.)\-:]?\s+", re.I)

_ABBREVIATIONS = [
    "mr.", "mrs.", "ms.", "dr.", "rs.", "no.", "nos.", "fig.", "figs.",
    "approx.", "etc.", "e.g.", "i.e.", "fy.", "q1.", "q2.", "q3.", "q4.",
    "ltd.", "inc.", "co.", "vs.",
]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text or "", re.UNICODE))


def _is_bullet(text: str) -> bool:
    return bool(_BULLET_RE.match(text or ""))


def _ends_sentence(text: str) -> bool:
    return bool(re.search(r"[.!?][\"'’”)]?$", _norm(text)))


def _headingish(line: str) -> bool:
    x = _norm(line)
    if not x or len(x) > 160 or _word_count(x) > 16:
        return False
    if x.endswith((".", "?", "!")) and _word_count(x) > 5:
        return False
    letters = [c for c in x if c.isalpha()]
    upper_ratio = (sum(c.isupper() for c in letters) / len(letters)) if letters else 0.0
    titleish = x.istitle() or upper_ratio >= 0.65
    return titleish or bool(_NUMBERING_RE.match(x))


def detect_mda_subsection(line: str) -> Optional[str]:
    x = _norm(line)
    if not _headingish(x):
        return None
    stripped = _NUMBERING_RE.sub("", x).lower()
    for label, patterns in SECTION_PATTERNS:
        for pat in patterns:
            if re.fullmatch(rf".*\b(?:{pat})\b.*", stripped, flags=re.I):
                return label
    return None


def _protect_periods(text: str) -> str:
    out = str(text or "")
    for abbr in _ABBREVIATIONS:
        out = re.sub(re.escape(abbr), abbr.replace(".", "<prd>"), out, flags=re.I)
    out = re.sub(r"(?<=\d)\.(?=\d)", "<prd>", out)
    return out


def sentence_split(text: str) -> List[str]:
    protected = _protect_periods(_norm(text))
    if not protected:
        return []
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9₹(])", protected)
    return [p.replace("<prd>", ".").strip() for p in parts if p.strip()]


def find_mda_payload(sections: Dict[str, Dict]) -> Optional[Dict]:
    for label, payload in sections.items():
        if payload.get("canonical_category") == MDA_CANONICAL or label == MDA_CANONICAL:
            return payload
    return None


def build_mda_page_rows(
    report_id: str,
    analysis_pages: List[Dict],
    sections: Dict[str, Dict],
    quality_by_page: Dict[int, Dict] | None = None,
) -> Tuple[List[Dict], Optional[Dict]]:
    """Build page-level MDA rows using V14's fixed start/end range."""
    payload = find_mda_payload(sections)
    if not payload:
        return [], None
    start = int(payload.get("start_page") or 0)
    end = int(payload.get("end_page") or 0)
    if start <= 0 or end < start:
        return [], payload

    rows = []
    current_section = "MDA - General"
    for p in analysis_pages:
        page = p.get("page")
        if not isinstance(page, int) or not (start <= page <= end):
            continue

        section_at_start = current_section
        headings = []
        lines = [_norm(x) for x in (p.get("text") or "").splitlines() if _norm(x)]
        for line in lines:
            sec = detect_mda_subsection(line)
            if sec:
                current_section = sec
                headings.append(sec)

        q = (quality_by_page or {}).get(page, {})
        rows.append({
            "report_id": report_id,
            "mda_page_id": f"{report_id}_MDA_P{page:04d}",
            "page": page,
            "mda_start_page": start,
            "mda_end_page": end,
            "section_at_page_start": section_at_start,
            "section_at_page_end": current_section,
            "subsection_headings_on_page": "|".join(dict.fromkeys(headings)),
            "quality_decision": q.get("decision", ""),
            "text_source": q.get("text_source", p.get("method", "")),
            "text": p.get("text", ""),
        })
    return rows, payload


def build_paragraph_rows(page_rows: List[Dict]) -> List[Dict]:
    """Conservative content units; avoids silently merging a whole page."""
    out: List[Dict] = []
    paragraph_number = 0
    current_section = "MDA - General"

    for page_row in page_rows:
        lines = [_norm(x) for x in str(page_row.get("text", "")).splitlines() if _norm(x)]
        buf: List[str] = []
        start_line = 1
        page = int(page_row["page"])
        page_quality = str(page_row.get("quality_decision") or "")
        text_source = str(page_row.get("text_source") or "")

        def flush(end_line: int, reason: str):
            nonlocal buf, start_line, paragraph_number
            if not buf:
                return
            text = _norm(" ".join(buf))
            if not text:
                buf = []
                return
            paragraph_number += 1
            wc = _word_count(text)
            flags = []
            if wc <= 2:
                flags.append("VERY_SHORT_UNIT")
            if wc >= 250:
                flags.append("VERY_LONG_UNIT")
            if len(re.findall(r"\d", text)) > 30:
                flags.append("HIGH_NUMERIC_CONTENT_REVIEW")
            if page_quality == "REVIEW":
                flags.append("PAGE_QUALITY_REVIEW")
            unit_type = "BULLET" if _is_bullet(text) else "PARAGRAPH"
            out.append({
                "paragraph_id": f"{page_row['report_id']}_MDA_P{page:04d}_PAR{paragraph_number:04d}",
                "report_id": page_row["report_id"],
                "page": page,
                "section": current_section,
                "unit_type": unit_type,
                "start_line": start_line,
                "end_line": end_line,
                "boundary_reason": reason,
                "paragraph_word_count": wc,
                "quality_decision": page_quality,
                "text_source": text_source,
                "review_flag": "|".join(dict.fromkeys(flags)),
                "paragraph_text": text,
            })
            buf = []

        for idx, line in enumerate(lines, start=1):
            sec = detect_mda_subsection(line)
            if sec:
                flush(idx - 1, "section_heading")
                current_section = sec
                paragraph_number += 1
                out.append({
                    "paragraph_id": f"{page_row['report_id']}_MDA_P{page:04d}_PAR{paragraph_number:04d}",
                    "report_id": page_row["report_id"],
                    "page": page,
                    "section": current_section,
                    "unit_type": "SECTION_HEADING",
                    "start_line": idx,
                    "end_line": idx,
                    "boundary_reason": "section_heading",
                    "paragraph_word_count": _word_count(line),
                    "quality_decision": page_quality,
                    "text_source": text_source,
                    "review_flag": "PAGE_QUALITY_REVIEW" if page_quality == "REVIEW" else "",
                    "paragraph_text": line,
                })
                start_line = idx + 1
                continue

            if _is_bullet(line):
                flush(idx - 1, "bullet_boundary")
                start_line = idx
                buf = [line]
                flush(idx, "bullet")
                start_line = idx + 1
                continue

            # Many PDF extractors lose blank lines.  Limit pathological units only
            # at a completed sentence boundary, preserving meaning while avoiding
            # 400-600 word page-sized pseudo-paragraphs.
            if buf:
                buffered_words = _word_count(" ".join(buf))
                if _ends_sentence(buf[-1]) and buffered_words >= 90:
                    flush(idx - 1, "long_unit_sentence_boundary")
                    start_line = idx
                elif _ends_sentence(buf[-1]) and _headingish(line):
                    flush(idx - 1, "sentence_or_structural_boundary")
                    start_line = idx
            if not buf:
                start_line = idx
            buf.append(line)

        flush(len(lines), "page_end")

    return out


def build_sentence_rows(paragraph_rows: List[Dict]) -> List[Dict]:
    out = []
    for p in paragraph_rows:
        if p.get("unit_type") == "SECTION_HEADING":
            continue
        for n, sentence in enumerate(sentence_split(p.get("paragraph_text", "")), start=1):
            out.append({
                "sentence_id": f"{p['paragraph_id']}_S{n:03d}",
                "paragraph_id": p["paragraph_id"],
                "report_id": p["report_id"],
                "page": p["page"],
                "section": p["section"],
                "quality_decision": p.get("quality_decision", ""),
                "text_source": p.get("text_source", ""),
                "sentence_number": n,
                "sentence_text": sentence,
            })
    return out


def build_review_queue(paragraph_rows: List[Dict]) -> List[Dict]:
    return [r for r in paragraph_rows if r.get("review_flag")]


def build_coverage_audit(report_id: str, page_rows: List[Dict], payload: Optional[Dict]) -> List[Dict]:
    if not payload:
        return [{
            "report_id": report_id,
            "mda_found": False,
            "original_heading": "",
            "confidence": "",
            "start_page": "",
            "end_page": "",
            "expected_page_count": 0,
            "actual_page_count": 0,
            "continuity_gaps": "",
            "quality_review_pages": "",
            "status": "MDA_NOT_FOUND",
        }]

    start = int(payload.get("start_page") or 0)
    end = int(payload.get("end_page") or 0)
    pages = [int(r["page"]) for r in page_rows]
    gaps = []
    for a, b in zip(pages, pages[1:]):
        if b != a + 1:
            gaps.append(f"{a}->{b}")
    review_pages = [str(r["page"]) for r in page_rows if r.get("quality_decision") == "REVIEW"]
    expected = (end - start + 1) if start > 0 and end >= start else 0
    status = "PASS"
    if gaps or len(pages) != expected or review_pages:
        status = "REVIEW"

    return [{
        "report_id": report_id,
        "mda_found": True,
        "original_heading": payload.get("original_heading", MDA_CANONICAL),
        "confidence": payload.get("semantic_confidence", payload.get("detection_confidence", "")),
        "start_page": start,
        "end_page": end,
        "expected_page_count": expected,
        "actual_page_count": len(pages),
        "continuity_gaps": "|".join(gaps),
        "quality_review_pages": "|".join(review_pages),
        "status": status,
    }]
