"""V15.3 additive section packaging and nested-boundary guard.

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

V15_PACKAGING_SCHEMA = "v15.4-section-packaging-guard-4"

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
    label = "Corporate Social Responsibility"
    payload = kept.get(label)
    if not payload or _is_manual(payload):
        return kept, audit
    cr = _rng(payload)
    if not cr:
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
                        # Annexure wrapper is the strongest possible end boundary.
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
