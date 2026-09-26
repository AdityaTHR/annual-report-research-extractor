"""V15.6 additive section packaging and boundary guard.

The validated V14 engine remains authoritative for detection and its existing
boundaries. This module never changes extractor.py or semantic_v14.py. It only:
1) recovers statutory CSR annexures when a Board/Directors-report reference was
   mistaken for the actual CSR start;
2) repairs CSR overruns at the next visible Annexure or strong peer heading; and
3) prevents obvious duplicate / internal-topic candidates from being packaged as
   peer standalone reports.
"""
from __future__ import annotations

from copy import deepcopy
import re

import extractor as core

V15_PACKAGING_SCHEMA = "v15.6-section-packaging-guard-6"

_RESPONSIBILITY_PRIORITY = {
    "Business Responsibility & Sustainability Report (BRSR)": 4,
    "Business Responsibility Report (BRR)": 3,
    "ESG Report": 2,
    "Sustainability Report": 1,
}

_INTERNAL_TOPIC_CATEGORIES = {
    "Risk Management",
    "Human Resources & Talent",
    "Cybersecurity & IT Governance",
}

_STRONG_PARENTS = {
    "Board's Report",
    "Directors' Report",
    "Management Discussion & Analysis",
}

_ANNEXURE_RE = re.compile(
    r"(?i)^\s*annex(?:ure)?\s*[-–—:]?\s*(?:[A-Z]|\d+|[IVXLCDM]+)\b"
)


_CSR_STATUTORY_TITLE_RE = re.compile(
    r"(?i)\b(?:"
    r"annual\s+report\s+on\s+(?:csr|corporate\s+social\s+responsibility)\s+activities?\s*\d*"
    r"|report\s+on\s+(?:csr|corporate\s+social\s+responsibility)(?:\s*\(csr\))?\s+activities?\s*\d*"
    r"|report\s+on\s+csr\s+activities?\s*\d*"
    r")\b"
)

_STRONG_CSR_PEER_PATTERNS = (
    # Keep this list deliberately narrow. Navigation furniture in integrated
    # reports often contains labels such as "Financial Statements" on every page.
    # These are strong peer transitions rather than ordinary body references.
    ("Corporate Governance", re.compile(r"(?i)^corporate\s+governance(?:\s+report)?$")),
    ("Board of Directors", re.compile(r"(?i)^board\s+of\s+directors$")),
    ("Management Team", re.compile(r"(?i)^(?:management|leadership)\s+team$")),
)


def _rng(payload):
    try:
        a = int(payload.get("start_page"))
        b = int(payload.get("end_page"))
        return (a, b) if a > 0 and b >= a else None
    except Exception:
        return None


def _is_manual(payload):
    return str(payload.get("semantic_match_type", "")).upper() == "MANUAL_OVERRIDE"


def _contains(parent_rng, child_rng):
    return bool(parent_rng and child_rng and parent_rng[0] <= child_rng[0] and child_rng[1] <= parent_rng[1])


def _compact(text):
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def _plain_semantic_text(text):
    """Strip lightweight markup emitted by layout extraction before regex matching."""
    s = re.sub(r"<[^>]+>", " ", str(text or ""))
    s = s.replace("¹", "1").replace("²", "2").replace("³", "3")
    return re.sub(r"\s+", " ", s).strip()


def _line_order(line):
    try:
        return int(line.get("order", 0))
    except Exception:
        return 0


def _anchor_for_heading_on_page(pages, page_num, heading, printed_page=None):
    """Recover the already-selected section start on its known physical page.

    Restricting the lookup to the V14-selected page prevents body references on
    earlier pages from becoming a new start candidate.
    """
    idx = int(page_num) - 1
    if idx < 0 or idx >= len(pages):
        return None
    target = _compact(heading)
    if not target:
        return None
    candidates = []
    for line in pages[idx].get("lines", []) or []:
        text = core._norm_line(line.get("text", ""))
        c = _compact(text)
        if not c:
            continue
        if target in c or c in target:
            # Prefer the closest textual match and a heading-like line.
            layout = str(line.get("layout_class") or "").lower()
            heading_bonus = 1 if ("heading" in layout or "section" in layout) else 0
            candidates.append((heading_bonus, -abs(len(c) - len(target)), -_line_order(line), line, text))
    if not candidates:
        return None
    _, _, _, line, text = max(candidates, key=lambda x: x[:3])

    # Higher-level layout lines can omit font/size while a duplicate native line
    # on the same page retains them. Enrich the anchor from that duplicate so
    # sibling-chapter style matching remains precise.
    style_line = line
    if not line.get("size"):
        same = [
            ln for ln in pages[idx].get("lines", []) or []
            if _compact(ln.get("text", "")) == _compact(text) and ln.get("size")
        ]
        if same:
            style_line = max(same, key=lambda ln: float(ln.get("size") or 0))

    return {
        "index": idx,
        "pdf_page": pages[idx].get("page") or page_num,
        "printed_page": printed_page,
        "line_order": _line_order(line),
        "bbox": line.get("bbox"),
        "matched_text": text,
        "matched_alias": heading,
        "label": heading,
        "score": 80,
        "detection_source": "v15-known-page-heading",
        "size": style_line.get("size"),
        "font": style_line.get("font"),
        "bold": style_line.get("bold", line.get("bold")),
        "layout_class": line.get("layout_class"),
    }


def _annexure_id(text):
    m = _ANNEXURE_RE.search(core._norm_line(text))
    if not m:
        return None
    token = re.search(r"(?i)annex(?:ure)?\s*[-–—:]?\s*([A-Z]|\d+|[IVXLCDM]+)\b", m.group(0))
    return token.group(1).upper() if token else None


def _current_annexure_id(pages, start_anchor):
    """Infer the wrapper (e.g. Annexure A) belonging to the current start page."""
    idx = int(start_anchor["index"])
    if idx < 0 or idx >= len(pages):
        return None
    sb = start_anchor.get("bbox")
    sy = sb[1] if isinstance(sb, (list, tuple)) and len(sb) >= 4 else None
    sx = ((sb[0] + sb[2]) / 2.0) if isinstance(sb, (list, tuple)) and len(sb) >= 4 else None
    width = float(pages[idx].get("width") or 0)
    candidates = []
    for line in pages[idx].get("lines", []) or []:
        text = core._norm_line(line.get("text", ""))
        aid = _annexure_id(text)
        if not aid:
            continue
        lb = line.get("bbox")
        if sy is not None and isinstance(lb, (list, tuple)) and len(lb) >= 4:
            ly = lb[1]
            lx = (lb[0] + lb[2]) / 2.0
            same_half = True
            if width and sx is not None:
                same_half = (sx < width / 2) == (lx < width / 2)
            if same_half and ly <= sy + 8:
                candidates.append((sy - ly, aid))
    if candidates:
        candidates.sort(key=lambda x: abs(x[0]))
        return candidates[0][1]
    return None


def _next_annexure_boundary(pages, start_anchor, parent_end_page):
    """Find the next visible Annexure wrapper after an already validated start."""
    start_idx = int(start_anchor["index"])
    start_order = int(start_anchor.get("line_order", 0))
    start_bbox = start_anchor.get("bbox")
    start_y = start_bbox[1] if isinstance(start_bbox, (list, tuple)) and len(start_bbox) >= 4 else None
    current_annexure = _current_annexure_id(pages, start_anchor)
    last_idx = min(len(pages) - 1, int(parent_end_page) - 1)

    for idx in range(start_idx, last_idx + 1):
        lines = sorted(pages[idx].get("lines", []) or [], key=_line_order)
        for line in lines:
            order = _line_order(line)
            text = core._norm_line(line.get("text", ""))
            aid = _annexure_id(text)
            if not text or len(text) > 220 or not aid:
                continue
            if idx == start_idx:
                # Layout reading order can interleave two-up pages, so do not rely
                # on order alone. Skip the current Annexure wrapper and anything
                # visually above the selected section heading.
                if current_annexure and aid == current_annexure:
                    continue
                lb = line.get("bbox")
                if start_y is not None and isinstance(lb, (list, tuple)) and len(lb) >= 4 and lb[1] <= start_y + 8:
                    continue
                if line.get("bbox") is None and order <= start_order:
                    continue
            # A short line beginning with Annexure + identifier is itself a
            # strong structural wrapper. Do not reject it merely because the layout
            # engine called it folio-edge (common in two-up / column reports).
            return {
                "index": idx,
                "pdf_page": pages[idx].get("page") or idx + 1,
                "line_order": order,
                "bbox": line.get("bbox"),
                "matched_text": text,
                "label": "Annexure boundary",
                "score": 82,
                "detection_source": "v15-next-annexure-boundary",
            }
    return None


def _printed_page_for_anchor(page, anchor):
    try:
        fn = getattr(core, "_anchor_printed_page", None)
        if callable(fn):
            return fn(page, anchor)
    except Exception:
        pass
    return None


def _is_heading_evidence(page, line, text, require_page_top=False):
    """Conservative visual evidence for an actual structural heading."""
    layout = str(line.get("layout_class") or "").lower()
    if "heading" in layout or "section" in layout:
        return True
    if not core._looks_like_heading_text(text):
        return False
    if not require_page_top:
        return True
    order = _line_order(line)
    if order <= 8:
        return True
    bb = line.get("bbox")
    height = float(page.get("height") or 0)
    return bool(
        height
        and isinstance(bb, (list, tuple))
        and len(bb) >= 4
        and float(bb[1]) <= 0.28 * height
    )


def _find_statutory_csr_annexure(pages, start_page, end_page):
    """Find an actual CSR statutory annexure, not a Board-report reference.

    A candidate must have a CSR-report title and a visible Annexure wrapper on the
    same page immediately before it. This distinguishes text such as
    "CSR is set out in Annexure II" from the real "Annexure II / Annual Report on
    CSR Activities" section.
    """
    first = max(0, int(start_page) - 1)
    last = min(len(pages) - 1, int(end_page) - 1)
    for idx in range(first, last + 1):
        page = pages[idx]
        lines = sorted(page.get("lines", []) or [], key=_line_order)
        for pos, line in enumerate(lines):
            text = core._norm_line(line.get("text", ""))
            if not text or len(text) > 240:
                continue

            # Statutory titles are frequently broken across two visual lines, e.g.
            # "Report on Corporate Social Responsibility (CSR)" / "activities for...".
            # Match a short continuation window but anchor the section at the first
            # title line so page / geometry alignment stays exact.
            title_text = text
            semantic_title = _plain_semantic_text(title_text)
            matched_title = bool(_CSR_STATUTORY_TITLE_RE.search(semantic_title))
            if not matched_title:
                parts = [text]
                for nxt in lines[pos + 1:pos + 3]:
                    nt = core._norm_line(nxt.get("text", ""))
                    if not nt:
                        continue
                    parts.append(nt)
                    joined = " ".join(parts)
                    if len(joined) > 360:
                        break
                    if _CSR_STATUTORY_TITLE_RE.search(_plain_semantic_text(joined)):
                        title_text = joined
                        matched_title = True
                        break
            if not matched_title:
                continue

            # Require a genuine Annexure wrapper on the same page. Layout engines
            # sometimes emit the visual wrapper after the title in reading order
            # (or classify it as folio-edge), so geometry is more reliable than
            # list order here. Exact semantic title + nearby Annexure wrapper is
            # stronger evidence than generic heading typography.
            wrapper = None
            tb = line.get("bbox")
            ty = tb[1] if isinstance(tb, (list, tuple)) and len(tb) >= 4 else None
            for prev in lines:
                if prev is line:
                    continue
                ptext = core._norm_line(prev.get("text", ""))
                if not _annexure_id(ptext) or len(ptext) > 180:
                    continue
                pb = prev.get("bbox")
                if ty is not None and isinstance(pb, (list, tuple)) and len(pb) >= 4:
                    # Wrapper should sit above / very near the statutory title.
                    if float(pb[1]) > float(ty) + 18:
                        continue
                    if float(ty) - float(pb[1]) > 110:
                        continue
                elif _line_order(prev) > pos + 8:
                    continue
                wrapper = prev
                break
            if wrapper is None:
                continue

            # If the title itself is not classified as a heading, the exact
            # statutory-title regex plus the nearby Annexure wrapper is sufficient.
            if not _is_heading_evidence(page, line, text) and not matched_title:
                continue

            anchor = {
                "index": idx,
                "pdf_page": page.get("page") or idx + 1,
                "line_order": _line_order(line),
                "bbox": line.get("bbox"),
                "matched_text": title_text,
                "matched_alias": title_text,
                "label": "Corporate Social Responsibility",
                "score": 90,
                "detection_source": "v15-statutory-csr-annexure",
            }
            pp = _printed_page_for_anchor(page, anchor)
            if pp is not None:
                anchor["printed_page"] = pp
            return anchor
    return None


def _same_chapter_heading_style(start_anchor, page, line, text):
    """Strong sibling-chapter evidence from repeated visual typography.

    Integrated reports often give each top-level ESG chapter the same font, size,
    weight and vertical title band.  Matching that style is substantially stronger
    than treating any short bold phrase as a boundary.
    """
    if str(line.get("layout_class") or "").lower() == "folio-edge":
        return False
    if not core._looks_like_heading_text(text):
        return False
    words = re.findall(r"[A-Za-z]+", text)
    if not (2 <= len(words) <= 10) or len(text) > 120:
        return False

    sb = start_anchor.get("bbox")
    lb = line.get("bbox")
    if not (
        isinstance(sb, (list, tuple)) and len(sb) >= 4
        and isinstance(lb, (list, tuple)) and len(lb) >= 4
    ):
        return False

    # Same top-title vertical band. We intentionally do not demand the same x
    # coordinate because two-up / mirrored layouts can place sibling titles on
    # opposite halves of a physical page.
    if abs(float(lb[1]) - float(sb[1])) > 24:
        return False

    # Recover native typography for layout-generated heading lines when needed.
    cand_style = line
    if not line.get("size"):
        same = [
            ln for ln in page.get("lines", []) or []
            if _compact(ln.get("text", "")) == _compact(text) and ln.get("size")
        ]
        if same:
            cand_style = max(same, key=lambda ln: float(ln.get("size") or 0))

    try:
        ss = float(start_anchor.get("size") or 0)
        ls = float(cand_style.get("size") or 0)
    except Exception:
        return False
    if ss <= 0 or ls <= 0 or abs(ss - ls) > 0.35:
        return False

    sfont = str(start_anchor.get("font") or "").strip().lower()
    lfont = str(cand_style.get("font") or "").strip().lower()
    if sfont and lfont and sfont != lfont:
        return False
    if bool(start_anchor.get("bold")) != bool(cand_style.get("bold")):
        return False

    if _compact(text) == _compact(start_anchor.get("matched_text", "")):
        return False
    return True


def _find_strong_csr_peer_boundary(pages, start_anchor, end_page):
    """Find the first strong sibling heading inside an overlong CSR range."""
    start_idx = int(start_anchor.get("index", 0))
    last = min(len(pages) - 1, int(end_page) - 1)
    for idx in range(start_idx + 1, last + 1):
        page = pages[idx]
        lines = sorted(page.get("lines", []) or [], key=_line_order)
        for line in lines:
            text = core._norm_line(line.get("text", ""))
            if not text or len(text) > 120:
                continue

            label = None
            if _same_chapter_heading_style(start_anchor, page, line, text):
                label = text
            else:
                for candidate_label, pattern in _STRONG_CSR_PEER_PATTERNS:
                    if pattern.match(text) and _is_heading_evidence(page, line, text, require_page_top=True):
                        label = candidate_label
                        break
            if not label:
                continue

            anchor = {
                "index": idx,
                "pdf_page": page.get("page") or idx + 1,
                "line_order": _line_order(line),
                "bbox": line.get("bbox"),
                "matched_text": text,
                "label": label,
                "score": 90,
                "detection_source": "v15-style-peer-boundary",
            }
            pp = _printed_page_for_anchor(page, anchor)
            if pp is not None:
                anchor["printed_page"] = pp
            return anchor
    return None


def _merge_repaired_payload(original, repaired):
    for key, value in original.items():
        if key not in {
            "start_page", "end_page", "printed_start_page", "printed_end_page",
            "text", "raw_text", "detection_confidence",
        }:
            repaired[key] = value
    return repaired



# V15.5 generic hardening.
#
# These rules are intentionally category-level rather than company/year-specific:
# - long-form responsibility/CSR sections stop at a new, strong top-level report;
# - common leadership-title variants can be recovered without changing V14;
# - leadership messages are repackaged to the next peer/top-level title rather
#   than being cut at an early signature line.

_LEADERSHIP_TITLE_PATTERNS = {
    "Chairman Message": re.compile(
        r"(?i)^(?:message|letter|statement|review|address)\s+from\s+(?:the\s+)?(?:executive\s+)?chair(?:man|person)"
        r"|^(?:executive\s+)?chair(?:man|person)(?:'s|s)?\s+(?:message|letter|statement|review|address)$"
    ),
    "CEO Message": re.compile(
        r"(?i)^(?:message|letter|statement|review|address)\s+from\s+(?:the\s+)?(?:chief\s+executive(?:\s+officer)?|ceo)"
        r"|^(?:chief\s+executive(?:\s+officer)?|ceo)(?:'s|s)?\s+(?:message|letter|statement|review|address)$"
    ),
    "Managing Director Message": re.compile(
        r"(?i)^(?:message|letter|statement|review|address)\s+from\s+(?:the\s+)?(?:managing\s+director|md)"
        r"|^(?:managing\s+director|md)(?:'s|s)?\s+(?:message|letter|statement|review|address)$"
    ),
}

_LEADERSHIP_ROLE_PATTERNS = {
    "Chairman Message": re.compile(r"(?i)\b(?:executive\s+)?chair(?:man|person)\b"),
    "CEO Message": re.compile(r"(?i)\b(?:chief\s+executive\s+officer|ceo)\b"),
    "Managing Director Message": re.compile(r"(?i)\b(?:managing\s+director|whole[-\s]?time\s+director|\bmd\b)\b"),
}

_LONG_FORM_TERMINATOR_SECTIONS = {
    "Corporate Social Responsibility",
    "Business Responsibility Report (BRR)",
    "Business Responsibility & Sustainability Report (BRSR)",
    "ESG Report",
    "Sustainability Report",
}



_LONG_FORM_EXACT_START_PATTERNS = {
    "Corporate Social Responsibility": (
        re.compile(r"(?i)^corporate\s+social\s+responsibility$"),
        re.compile(r"(?i)^annual\s+report\s+on\s+(?:csr|corporate\s+social\s+responsibility)\s+activities?\d*$"),
    ),
    "Business Responsibility Report (BRR)": (
        re.compile(r"(?i)^business\s+responsibility\s+report(?:\s*\(.*?\))?$"),
    ),
    "Business Responsibility & Sustainability Report (BRSR)": (
        re.compile(r"(?i)^business\s+responsibility\s+(?:and|&)\s+sustainability\s+report(?:\s*\(.*?\))?$"),
    ),
    "ESG Report": (
        re.compile(r"(?i)^(?:environmental,?\s*social\s*(?:and|&)\s*governance|esg)(?:\s+report)?$"),
    ),
    "Sustainability Report": (
        re.compile(r"(?i)^sustainability\s+report$"),
    ),
}

_PAGE_PREFIX_TERMINATORS = (
    ("Independent Auditor's Report", re.compile(
        r"(?i)^(?:\d+\s+)?independent\s+auditor(?:s|[’']s)?[’']?\s+report(?:\s+to\b.*)?$"
    )),
    ("Auditor's Report", re.compile(
        r"(?i)^(?:\d+\s+)?auditor(?:s|[’']s)?[’']?\s+report(?:\s+to\b.*)?$"
    )),
    ("Financial Statements", re.compile(
        r"(?i)^(?:\d+\s+)?(?:standalone\s+|consolidated\s+)?financial\s+statements?$"
    )),
)

_STRONG_TOP_LEVEL_TERMINATORS = (
    ("Independent Auditor's Report", re.compile(
        r"(?i)^independent\s+auditor(?:s|[’']s)?[’']?\s+report$"
    )),
    ("Auditor's Report", re.compile(
        r"(?i)^auditor(?:s|[’']s)?[’']?\s+report$"
    )),
    ("Financial Statements", re.compile(
        r"(?i)^(?:standalone\s+|consolidated\s+)?financial\s+statements?$"
    )),
    ("Board of Directors", re.compile(
        r"(?i)^board\s+of\s+directors$"
    )),
    ("Board's Report", re.compile(
        r"(?i)^board(?:'s|s')\s+report$"
    )),
    ("Directors' Report", re.compile(
        r"(?i)^directors?'?\s+report$"
    )),
    ("Corporate Governance Report", re.compile(
        r"(?i)^corporate\s+governance(?:\s+report)?$"
    )),
    ("Management Discussion & Analysis", re.compile(
        r"(?i)^management(?:'s)?\s+(?:discussion|review)\s*(?:and|&)\s*analysis(?:\s+report)?$"
    )),
    ("Business Responsibility Report", re.compile(
        r"(?i)^business\s+responsibility(?:\s+and\s+sustainability)?\s+report(?:\s*\(.*?\))?$"
    )),
    ("ESG Report", re.compile(
        r"(?i)^(?:environmental,\s*social\s*(?:and|&)\s*governance|esg)(?:\s+report)?$"
    )),
    ("Sustainability Report", re.compile(
        r"(?i)^sustainability\s+report$"
    )),
    ("Awards and Recognitions", re.compile(
        r"(?i)^awards?\s*(?:and|&)\s*recognitions?$"
    )),
    ("Notice", re.compile(
        r"(?i)^notice$"
    )),
)


def _line_top_fraction(page, line):
    bb = line.get("bbox")
    h = float(page.get("height") or 0)
    if not h or not isinstance(bb, (list, tuple)) or len(bb) < 4:
        return None
    return float(bb[1]) / h


def _first_structural_heading(page):
    """Return the first credible heading-like line in visual/page order."""
    candidates = []
    for line in page.get("lines", []) or []:
        text = core._norm_line(line.get("text", ""))
        if not text or len(text) > 220:
            continue
        layout = str(line.get("layout_class") or "").lower()
        if "section" not in layout and "heading" not in layout:
            continue
        bb = line.get("bbox")
        y = float(bb[1]) if isinstance(bb, (list, tuple)) and len(bb) >= 4 else 1e9
        candidates.append((y, _line_order(line), line, text))
    if not candidates:
        return None
    return min(candidates, key=lambda x: (x[0], x[1]))[2:]


def _is_strong_page_title(page, line, text):
    """Require evidence appropriate for a peer/top-level report boundary."""
    if not _is_heading_evidence(page, line, text, require_page_top=True):
        return False

    frac = _line_top_fraction(page, line)
    if frac is not None and frac > 0.30:
        return False

    # If layout reconstruction provides section headings, require this candidate
    # to be the first such heading on the page. This rejects most internal
    # subsections while retaining genuine new report/page titles.
    first = _first_structural_heading(page)
    if first is not None:
        first_line, first_text = first
        if first_line is not line and _compact(first_text) != _compact(text):
            return False
    return True




def _exact_long_form_anchor_on_page(pages, page_num, label):
    """Recover a long-form report title on an immediately adjacent page.

    This is deliberately exact-title only. It addresses PDF reading-order/layout
    cases where the visible report title is emitted late on the correct page and
    V14 consequently starts the packaged section one page too late.
    """
    patterns = _LONG_FORM_EXACT_START_PATTERNS.get(label)
    if not patterns:
        return None
    idx = int(page_num) - 1
    if idx < 0 or idx >= len(pages):
        return None
    candidates = []
    for line in pages[idx].get("lines", []) or []:
        text = _plain_semantic_text(core._norm_line(line.get("text", ""))).replace("’", "'")
        if not text or len(text) > 180:
            continue
        if any(pat.match(text) for pat in patterns):
            candidates.append((_line_order(line), line, text))
    if not candidates:
        return None
    order, line, text = min(candidates, key=lambda x: x[0])
    anchor = {
        "index": idx,
        "pdf_page": pages[idx].get("page") or page_num,
        "line_order": order,
        "bbox": line.get("bbox"),
        "matched_text": text,
        "matched_alias": text,
        "label": label,
        "score": 93,
        "detection_source": "v15.6-adjacent-exact-title",
    }
    pp = _printed_page_for_anchor(pages[idx], anchor)
    if pp is not None:
        anchor["printed_page"] = pp
    return anchor


def _page_prefix_terminator_anchor(pages, start_page, end_page, current_label=None):
    """Fallback for unmistakable report titles when layout metadata is weak.

    We only inspect the first 12 non-empty extracted lines of each page and only
    accept a very small exact-title vocabulary. This catches pages such as
    ``78 Independent Auditor's Report`` without turning body mentions into
    section boundaries.
    """
    first = max(0, int(start_page))
    last = min(len(pages) - 1, int(end_page) - 1)
    for idx in range(first, last + 1):
        page = pages[idx]
        seen = 0
        for line in sorted(page.get("lines", []) or [], key=_line_order):
            text = _plain_semantic_text(core._norm_line(line.get("text", ""))).replace("’", "'")
            if not text:
                continue
            seen += 1
            if seen > 12:
                break
            for label, pattern in _PAGE_PREFIX_TERMINATORS:
                if current_label and label == current_label:
                    continue
                if pattern.match(text):
                    anchor = {
                        "index": idx,
                        "pdf_page": page.get("page") or idx + 1,
                        "line_order": _line_order(line),
                        "bbox": line.get("bbox"),
                        "matched_text": text,
                        "label": label,
                        "score": 95,
                        "detection_source": "v15.6-page-prefix-terminator",
                    }
                    pp = _printed_page_for_anchor(page, anchor)
                    if pp is not None:
                        anchor["printed_page"] = pp
                    return anchor
    return None


def _find_strong_top_level_terminator(pages, start_page, end_page, current_label=None):
    """Find a strong new peer report inside an existing overlong range."""
    first = max(0, int(start_page))  # page after section start
    last = min(len(pages) - 1, int(end_page) - 1)
    for idx in range(first, last + 1):
        page = pages[idx]
        for line in sorted(page.get("lines", []) or [], key=_line_order):
            text = _plain_semantic_text(core._norm_line(line.get("text", "")))
            if not text or len(text) > 180:
                continue
            for label, pattern in _STRONG_TOP_LEVEL_TERMINATORS:
                if current_label and label == current_label:
                    continue
                if pattern.match(text) and _is_strong_page_title(page, line, text):
                    anchor = {
                        "index": idx,
                        "pdf_page": page.get("page") or idx + 1,
                        "line_order": _line_order(line),
                        "bbox": line.get("bbox"),
                        "matched_text": text,
                        "label": label,
                        "score": 92,
                        "detection_source": "v15.5-strong-top-level-terminator",
                    }
                    pp = _printed_page_for_anchor(page, anchor)
                    if pp is not None:
                        anchor["printed_page"] = pp
                    return anchor
    return None


def _leadership_candidate_anchor(pages, label):
    """Recover exact leadership-title variants using strong page-title evidence."""
    pattern = _LEADERSHIP_TITLE_PATTERNS.get(label)
    if pattern is None:
        return None
    candidates = []
    for idx, page in enumerate(pages):
        for line in page.get("lines", []) or []:
            text = _plain_semantic_text(core._norm_line(line.get("text", ""))).replace("’", "'")
            if not text or len(text) > 150 or not pattern.match(text):
                continue

            layout = str(line.get("layout_class") or "").lower()
            frac = _line_top_fraction(page, line)
            size = float(line.get("size") or 0)
            strong = (
                "section" in layout
                or "heading" in layout
                or (frac is not None and frac <= 0.25 and (bool(line.get("bold")) or size >= 9.0))
            )
            if not strong:
                continue

            # Enrich style from a duplicate native/layout line when available.
            style_line = line
            if not line.get("size"):
                same = [
                    ln for ln in page.get("lines", []) or []
                    if _compact(ln.get("text", "")) == _compact(text) and ln.get("size")
                ]
                if same:
                    style_line = max(same, key=lambda ln: float(ln.get("size") or 0))

            anchor = {
                "index": idx,
                "pdf_page": page.get("page") or idx + 1,
                "line_order": _line_order(line),
                "bbox": line.get("bbox"),
                "matched_text": text,
                "matched_alias": text,
                "label": label,
                "score": 94,
                "detection_source": "v15.5-leadership-title",
                "size": style_line.get("size"),
                "font": style_line.get("font"),
                "bold": style_line.get("bold", line.get("bold")),
                "layout_class": line.get("layout_class"),
            }
            pp = _printed_page_for_anchor(page, anchor)
            if pp is not None:
                anchor["printed_page"] = pp
            candidates.append(anchor)

    return min(candidates, key=lambda a: (a["index"], a.get("line_order", 0))) if candidates else None


def _find_same_style_peer_boundary(pages, start_anchor, max_pages=12):
    start_idx = int(start_anchor["index"])
    last = min(len(pages) - 1, start_idx + max_pages)
    for idx in range(start_idx + 1, last + 1):
        page = pages[idx]
        for line in sorted(page.get("lines", []) or [], key=_line_order):
            text = core._norm_line(line.get("text", ""))
            if not text or len(text) > 140:
                continue
            style_match = _same_chapter_heading_style(start_anchor, page, line, text)
            if not style_match:
                # Leadership page titles are sometimes emitted only as folio-edge
                # native lines. Permit those only with very strong typography:
                # same title band, same font size/font/weight, and a substantial
                # title size. This is narrower than the generic sibling matcher.
                sb = start_anchor.get("bbox")
                lb = line.get("bbox")
                try:
                    ss = float(start_anchor.get("size") or 0)
                    ls = float(line.get("size") or 0)
                except Exception:
                    ss = ls = 0
                sfont = str(start_anchor.get("font") or "").strip().lower()
                lfont = str(line.get("font") or "").strip().lower()
                style_match = bool(
                    ss >= 14 and ls >= 14 and abs(ss - ls) <= 0.35
                    and isinstance(sb, (list, tuple)) and len(sb) >= 4
                    and isinstance(lb, (list, tuple)) and len(lb) >= 4
                    and abs(float(lb[1]) - float(sb[1])) <= 24
                    and (not sfont or not lfont or sfont == lfont)
                    and bool(start_anchor.get("bold")) == bool(line.get("bold"))
                    and _compact(text) != _compact(start_anchor.get("matched_text", ""))
                    and 1 <= len(re.findall(r"[A-Za-z]+", text)) <= 10
                )
            if style_match:
                # Same-style peer must also occupy the page-title band.
                frac = _line_top_fraction(page, line)
                if frac is not None and frac > 0.30:
                    continue
                anchor = {
                    "index": idx,
                    "pdf_page": page.get("page") or idx + 1,
                    "line_order": _line_order(line),
                    "bbox": line.get("bbox"),
                    "matched_text": text,
                    "label": text,
                    "score": 90,
                    "detection_source": "v15.5-leadership-style-peer",
                }
                pp = _printed_page_for_anchor(page, anchor)
                if pp is not None:
                    anchor["printed_page"] = pp
                return anchor
    return None


def _find_late_signature_boundary(pages, start_anchor, label, max_pages=8):
    """Fallback for leadership reviews whose next page title uses another style.

    We only use a role-bearing signature on a page *after* the start page and in
    the lower half of the page. This avoids the common layout where a signature
    is printed on page 1 while the message continues onto page 2.
    """
    pattern = _LEADERSHIP_ROLE_PATTERNS.get(label)
    if pattern is None:
        return None
    start_idx = int(start_anchor["index"])
    last = min(len(pages) - 1, start_idx + max_pages)
    signature_idx = None

    for idx in range(start_idx + 1, last + 1):
        page = pages[idx]
        h = float(page.get("height") or 0)
        for line in page.get("lines", []) or []:
            text = core._norm_line(line.get("text", ""))
            if not text or not pattern.search(text):
                continue
            bb = line.get("bbox")
            if h and isinstance(bb, (list, tuple)) and len(bb) >= 4:
                if float(bb[1]) < 0.52 * h:
                    continue
            else:
                if _line_order(line) < 8:
                    continue
            signature_idx = idx

    if signature_idx is None or signature_idx + 1 >= len(pages):
        return None

    ni = signature_idx + 1
    return {
        "index": ni,
        "pdf_page": pages[ni].get("page") or ni + 1,
        "line_order": 0,
        "bbox": None,
        "matched_text": "Leadership signature end",
        "label": "Leadership signature end",
        "score": 88,
        "detection_source": "v15.5-late-signature-end",
    }


def _repair_leadership_sections(sections, raw_pages, clean_pages):
    kept = deepcopy(sections)
    audit = []

    # Recover exact leadership-title variants that V14 did not package.
    anchors = {}
    for label in _LEADERSHIP_TITLE_PATTERNS:
        payload = kept.get(label)
        if payload and _rng(payload):
            heading = payload.get("original_heading") or label
            anchor = _anchor_for_heading_on_page(
                clean_pages, _rng(payload)[0], heading, payload.get("printed_start_page")
            )
            if anchor is None and heading != label:
                anchor = _anchor_for_heading_on_page(
                    clean_pages, _rng(payload)[0], label, payload.get("printed_start_page")
                )
        else:
            anchor = _leadership_candidate_anchor(clean_pages, label)
        if anchor is not None:
            anchors[label] = anchor

    # Add missing categories only from exact title variants.
    for label, anchor in anchors.items():
        if label in kept:
            continue
        kept[label] = {
            "start_page": anchor["pdf_page"],
            "end_page": anchor["pdf_page"],
            "text": "",
            "raw_text": "",
            "original_heading": anchor.get("matched_text") or label,
            "canonical_category": label,
            "semantic_confidence": "HIGH",
            "semantic_match_type": "V15_5_LEADERSHIP_VARIANT",
        }
        audit.append({
            "section": label,
            "start_page": anchor["pdf_page"],
            "end_page": anchor["pdf_page"],
            "action": "RECOVERED_LEADERSHIP_VARIANT",
            "reason": anchor.get("matched_text") or label,
        })

    # Repackage each leadership message to the strongest next peer boundary.
    starts = sorted(
        (a["index"], label, a)
        for label, a in anchors.items()
        if label in kept and not _is_manual(kept[label])
    )

    for _, label, start_anchor in starts:
        payload = kept.get(label)
        if not payload:
            continue
        cr = _rng(payload)

        next_leader = None
        for idx2, label2, anchor2 in starts:
            if idx2 > start_anchor["index"]:
                next_leader = anchor2
                break

        style_peer = _find_same_style_peer_boundary(clean_pages, start_anchor)
        signature_boundary = _find_late_signature_boundary(clean_pages, start_anchor, label)

        boundary_candidates = [
            a for a in (next_leader, style_peer, signature_boundary)
            if a is not None and a["index"] > start_anchor["index"]
        ]
        if not boundary_candidates:
            continue
        boundary = min(boundary_candidates, key=lambda a: (a["index"], a.get("line_order", 0)))

        repaired = core._payload_from_anchors(raw_pages, clean_pages, start_anchor, boundary)
        rr = _rng(repaired) if repaired else None
        if not repaired or not rr:
            continue

        old = cr
        if old == rr:
            continue

        repaired = _merge_repaired_payload(payload, repaired)
        repaired["original_heading"] = payload.get("original_heading") or start_anchor.get("matched_text") or label
        repaired["canonical_category"] = payload.get("canonical_category") or label
        repaired["semantic_confidence"] = payload.get("semantic_confidence") or "HIGH"
        repaired["semantic_match_type"] = payload.get("semantic_match_type") or "V15_5_LEADERSHIP_VARIANT"
        kept[label] = repaired

        audit.append({
            "section": label,
            "start_page": rr[0],
            "end_page": rr[1],
            "action": "REPAIRED_LEADERSHIP_BOUNDARY",
            "reason": f"NEXT_PEER_{boundary.get('label')}",
        })

    return kept, audit


def _repair_long_form_terminators(sections, raw_pages, clean_pages):
    kept = deepcopy(sections)
    audit = []
    for label in _LONG_FORM_TERMINATOR_SECTIONS:
        payload = kept.get(label)
        if not payload or _is_manual(payload):
            continue
        cr = _rng(payload)
        if not cr or cr[1] <= cr[0]:
            continue

        boundary = _find_strong_top_level_terminator(
            clean_pages, cr[0], cr[1], current_label=label
        )
        if boundary is None:
            boundary = _page_prefix_terminator_anchor(
                clean_pages, cr[0], cr[1], current_label=label
            )

        original_heading = payload.get("original_heading") or label
        start_anchor = _anchor_for_heading_on_page(
            clean_pages, cr[0], original_heading, payload.get("printed_start_page")
        )
        if start_anchor is None and original_heading != label:
            start_anchor = _anchor_for_heading_on_page(
                clean_pages, cr[0], label, payload.get("printed_start_page")
            )

        # V15.6: if an exact long-form title exists on the immediately previous
        # physical page, prefer it. This is a narrowly-scoped correction for
        # reading-order/layout cases that shift the packaged start by one page.
        if cr[0] > 1:
            prev_anchor = _exact_long_form_anchor_on_page(clean_pages, cr[0] - 1, label)
            if prev_anchor is not None:
                start_anchor = prev_anchor

        if start_anchor is None or boundary is None:
            continue

        repaired = core._payload_from_anchors(raw_pages, clean_pages, start_anchor, boundary)
        rr = _rng(repaired) if repaired else None
        if not repaired or not rr:
            continue
        if rr[0] not in (cr[0], cr[0] - 1):
            continue
        if rr[1] > cr[1]:
            continue
        if rr == cr:
            continue

        repaired = _merge_repaired_payload(payload, repaired)
        kept[label] = repaired
        reason_prefix = (
            "PAGE_PREFIX"
            if boundary.get("detection_source") == "v15.6-page-prefix-terminator"
            else "STRONG_TOP_LEVEL"
        )
        audit.append({
            "section": label,
            "start_page": rr[0],
            "end_page": rr[1],
            "action": "REPAIRED_BOUNDARY",
            "reason": f"{reason_prefix}_{boundary.get('label')}",
        })
    return kept, audit



def repair_nested_boundaries(sections, raw_pages, clean_pages):
    """Recover / repair CSR boundaries using only strong structural evidence.

    Two conservative repairs are allowed:
    1) If a CSR candidate sits inside a Board/Directors report, prefer a later
       Annexure wrapper + statutory CSR-report title and end it at the next Annexure.
       This fixes Board-report references that were mistaken for the actual annexure.
    2) If a non-nested CSR range runs across a strong peer heading such as
       "Board of Directors", stop immediately before that peer.

    Manual overrides are never changed.
    """
    kept = deepcopy(sections)
    audit = []

    # V15.5 generic leadership recovery/repackaging first. Existing CSR
    # statutory/style repair remains stronger; generic long-form terminators run
    # afterwards so the same CSR range is not repaired twice.
    kept, leadership_audit = _repair_leadership_sections(kept, raw_pages, clean_pages)
    audit.extend(leadership_audit)

    label = "Corporate Social Responsibility"
    payload = kept.get(label)
    if not payload or _is_manual(payload):
        kept, long_form_audit = _repair_long_form_terminators(kept, raw_pages, clean_pages)
        audit.extend(long_form_audit)
        return kept, audit
    cr = _rng(payload)
    if not cr:
        kept, long_form_audit = _repair_long_form_terminators(kept, raw_pages, clean_pages)
        audit.extend(long_form_audit)
        return kept, audit

    # --- A. Statutory CSR annexure recovery inside Board/Directors report ---
    parents = []
    for parent in ("Board's Report", "Directors' Report"):
        pp = kept.get(parent)
        pr = _rng(pp) if pp else None
        if pr and pr[0] <= cr[0] <= pr[1]:
            parents.append((parent, pr))

    if parents:
        parent, pr = min(parents, key=lambda x: x[1][1] - x[1][0])
        actual_start = _find_statutory_csr_annexure(clean_pages, cr[0], pr[1])
        if actual_start is not None:
            boundary = _next_annexure_boundary(clean_pages, actual_start, pr[1])
            if boundary is not None:
                repaired = core._payload_from_anchors(raw_pages, clean_pages, actual_start, boundary)
                rr = _rng(repaired) if repaired else None
                if repaired and rr and pr[0] <= rr[0] <= rr[1] <= pr[1]:
                    changed = rr != cr
                    if changed:
                        repaired = _merge_repaired_payload(payload, repaired)
                        kept[label] = repaired
                        audit.append({
                            "section": label,
                            "start_page": rr[0],
                            "end_page": rr[1],
                            "action": "RECOVERED_STATUTORY_ANNEXURE",
                            "reason": f"CSR_ANNEXURE_INSIDE_{parent}",
                        })
                        # Once a statutory Annexure has been recovered, its next
                        # Annexure wrapper is the strongest possible CSR end boundary.
                        kept, long_form_audit = _repair_long_form_terminators(kept, raw_pages, clean_pages)
                        audit.extend(long_form_audit)
                        return kept, audit

    # --- B. Strong peer terminator inside an overlong narrative CSR section ---
    original_heading = payload.get("original_heading") or label
    start_anchor = _anchor_for_heading_on_page(
        clean_pages, cr[0], original_heading, payload.get("printed_start_page")
    )
    if start_anchor is None and original_heading != label:
        start_anchor = _anchor_for_heading_on_page(
            clean_pages, cr[0], label, payload.get("printed_start_page")
        )
    boundary = _find_strong_csr_peer_boundary(clean_pages, start_anchor, cr[1]) if start_anchor else None
    if boundary is not None:
        if start_anchor is not None:
            repaired = core._payload_from_anchors(raw_pages, clean_pages, start_anchor, boundary)
            rr = _rng(repaired) if repaired else None
            if repaired and rr and rr[0] == cr[0] and rr[1] < cr[1]:
                repaired = _merge_repaired_payload(payload, repaired)
                kept[label] = repaired
                audit.append({
                    "section": label,
                    "start_page": rr[0],
                    "end_page": rr[1],
                    "action": "REPAIRED_BOUNDARY",
                    "reason": f"STRONG_PEER_{boundary.get('label')}",
                })

    kept, long_form_audit = _repair_long_form_terminators(kept, raw_pages, clean_pages)
    audit.extend(long_form_audit)
    return kept, audit


def _looks_like_risk_register_card(payload):
    r = _rng(payload)
    if not r or r[0] != r[1]:
        return False
    text = str(payload.get("text") or "").lower()
    markers = (
        "risk description",
        "impact on value",
        "mitigating measure",
        "capital at risk",
        "risk rating",
    )
    return sum(m in text for m in markers) >= 3


def apply_packaging_guard(sections):
    """Return (filtered_sections, audit_rows).

    The guard is deliberately conservative:
    - manual overrides are never suppressed;
    - exact duplicate sustainability/responsibility ranges keep the most specific label;
    - Risk/HR/Cybersecurity headings fully contained in Board/Directors/MDA are
      treated as subsections rather than peer reports;
    - a one-page Cybersecurity candidate formatted as a risk-register card remains
      traceable but is not presented as a standalone Cybersecurity/IT report;
    - BRR/BRSR/ESG/Sustainability candidates wholly inside Board/Directors and
      ending with the parent are treated as references/subheadings, not a separate report.
    """
    original = deepcopy(sections)
    kept = deepcopy(sections)
    audit = []

    # 1) Exact duplicate responsibility/sustainability ranges.
    by_range = {}
    for label, payload in original.items():
        if label not in _RESPONSIBILITY_PRIORITY or _is_manual(payload):
            continue
        r = _rng(payload)
        if r:
            by_range.setdefault(r, []).append(label)

    for r, labels in by_range.items():
        if len(labels) < 2:
            continue
        winner = max(labels, key=lambda x: _RESPONSIBILITY_PRIORITY[x])
        for label in labels:
            if label == winner or label not in kept:
                continue
            kept.pop(label, None)
            audit.append({
                "section": label,
                "start_page": r[0],
                "end_page": r[1],
                "action": "SUPPRESSED_STANDALONE",
                "reason": f"DUPLICATE_RANGE_OF_{winner}",
            })

    parent_ranges = {
        label: _rng(payload)
        for label, payload in original.items()
        if label in _STRONG_PARENTS and _rng(payload)
    }

    # 2) Internal topic headings should remain traceable but not appear as peer reports.
    for label in list(kept):
        if label not in _INTERNAL_TOPIC_CATEGORIES:
            continue
        payload = kept[label]
        if _is_manual(payload):
            continue
        cr = _rng(payload)
        if not cr:
            continue
        suppressed = False
        for parent, pr in parent_ranges.items():
            if parent == label:
                continue
            if _contains(pr, cr):
                kept.pop(label, None)
                audit.append({
                    "section": label,
                    "start_page": cr[0],
                    "end_page": cr[1],
                    "action": "SUPPRESSED_STANDALONE",
                    "reason": f"NESTED_SUBSECTION_OF_{parent}",
                })
                suppressed = True
                break
        if suppressed:
            continue

        # Generic risk-register layouts often contain a visual "Cyber Security"
        # card alongside other individual risks. It is useful diagnostic content,
        # but not a peer Cybersecurity / IT Governance report.
        if label == "Cybersecurity & IT Governance" and _looks_like_risk_register_card(payload):
            kept.pop(label, None)
            audit.append({
                "section": label,
                "start_page": cr[0],
                "end_page": cr[1],
                "action": "SUPPRESSED_STANDALONE",
                "reason": "RISK_REGISTER_CARD_NOT_PEER_REPORT",
            })

    # 3) Responsibility reports referenced inside a Board/Directors report often
    # get a heading-like paragraph but are actually said to be presented elsewhere.
    # The safest mechanical signature is containment plus the same ending boundary.
    for label in list(kept):
        if label not in _RESPONSIBILITY_PRIORITY:
            continue
        payload = kept[label]
        if _is_manual(payload):
            continue
        cr = _rng(payload)
        if not cr:
            continue
        for parent in ("Board's Report", "Directors' Report"):
            pr = parent_ranges.get(parent)
            if pr and _contains(pr, cr) and cr[1] == pr[1]:
                kept.pop(label, None)
                audit.append({
                    "section": label,
                    "start_page": cr[0],
                    "end_page": cr[1],
                    "action": "SUPPRESSED_STANDALONE",
                    "reason": f"REFERENCE_OR_SUBHEADING_INSIDE_{parent}",
                })
                break

    return kept, audit
