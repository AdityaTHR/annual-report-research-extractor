import csv
import html
import io
import json
import re
import zipfile
from collections import Counter
from copy import deepcopy

import fitz
import pytesseract
from PIL import Image
from docx import Document

PRESETS = {
    "Chairman Message": {
        "aliases": [
            "message from the chairman", "chairman's message", "chairperson's message",
            "message from the chairperson", "letter from the chairman", "chairman's letter",
            "chairman's statement", "chairman statement", "statement from the chairman",
            "executive chairman's message", "message from the executive chairman",
        ],
    },
    "CEO Message": {
        "aliases": [
            "message from the ceo", "ceo message", "ceo's message", "letter from the ceo",
            "chief executive officer's message", "message from the chief executive officer",
            "letter from the chief executive officer",
        ],
    },
    "Managing Director Message": {
        "aliases": [
            "message from the managing director", "managing director's message",
            "md message", "letter from the managing director",
        ],
    },
    "Management Discussion & Analysis": {
        "aliases": [
            "management discussion & analysis report",
            "management discussion and analysis report",
            "management discussion & analysis",
            "management discussion and analysis",
            "management discussion analysis", "md&a",
        ],
    },
    "Business Responsibility & Sustainability Report (BRSR)": {
        "aliases": [
            "business responsibility & sustainability report",
            "business responsibility and sustainability report", "brsr report", "brsr",
        ],
    },
    "Business Responsibility Report (BRR)": {
        "aliases": ["business responsibility report", "business responsibility (br) report", "brr report"],
    },
    "ESG Report": {
        "aliases": ["esg report", "environmental social and governance report", "environment social and governance report"],
    },
    "Sustainability Report": {
        "aliases": ["sustainability report"],
    },
}


FORMAT_EXT = {"TXT": "txt", "JSON": "json", "PDF": "pdf", "DOCX": "docx", "CSV": "csv", "MD": "md"}


def _compact(s):
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _norm_line(s):
    return re.sub(r"\s+", " ", s).strip()


def _ocr(page):
    pix = page.get_pixmap(matrix=fitz.Matrix(1.6, 1.6), colorspace=fitz.csGRAY, alpha=False)
    img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    return pytesseract.image_to_string(img, config="--psm 6")


def _layout_lines(page):
    """Return PyMuPDF lines in native reading order with geometry/font metadata."""
    out = []
    try:
        d = page.get_text("dict")
        order = 0
        for block in d.get("blocks", []):
            for line in block.get("lines", []):
                spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
                if not spans:
                    continue
                text = "".join(s.get("text", "") for s in spans).strip()
                if not text:
                    continue
                x0 = min(s["bbox"][0] for s in spans)
                y0 = min(s["bbox"][1] for s in spans)
                x1 = max(s["bbox"][2] for s in spans)
                y1 = max(s["bbox"][3] for s in spans)
                out.append({
                    "order": order,
                    "text": text,
                    "bbox": [float(x0), float(y0), float(x1), float(y1)],
                    "size": float(max(s.get("size", 0) for s in spans)),
                })
                order += 1
    except Exception:
        return []
    return out


def extract_source(name, data):
    ext = name.rsplit(".", 1)[-1].lower()
    if ext == "pdf":
        doc = fitz.open(stream=data, filetype="pdf")
        pages = []
        structural_tokens = (
            "content", "chairman", "chief executive", "ceo", "managing director",
            "management discussion", "corporate governance", "responsibility",
            "sustainability", "directors' report", "directors’ report", "board's report",
            "boards’ report", "annexure", "financial statements", "clinical governance",
            "corporate social responsibility", "notice", "awards", "company overview",
            "about tcs", "about adani", "performance highlights", "corporate review",
        )
        for i, page in enumerate(doc):
            text = page.get_text("text")
            method = "native"
            low = text.lower()
            # Native geometry is only needed on pages that could contain a structural
            # heading or on the opening pages where Contents/Index lives. This keeps
            # large 500-700 page annual reports fast.
            need_layout = i < 30 or any(tok in low for tok in structural_tokens)
            lines = _layout_lines(page) if need_layout else [
                {"order": j, "text": t, "bbox": None, "size": None}
                for j, t in enumerate(text.splitlines()) if t.strip()
            ]
            if len(text.strip()) < 40:
                text = _ocr(page)
                method = "ocr"
                lines = [
                    {"order": j, "text": t, "bbox": None, "size": None}
                    for j, t in enumerate(text.splitlines()) if t.strip()
                ]
            pages.append({
                "page": i + 1,
                "text": text,
                "method": method,
                "width": float(page.rect.width),
                "height": float(page.rect.height),
                "lines": lines,
            })
        return pages

    if ext in ("txt", "md"):
        text = data.decode("utf-8", errors="ignore")
        parts = re.split(r"(?m)^\s*=====\s*PAGE\s+(\d+)\s*=====\s*$", text)
        if len(parts) > 2:
            result = []
            for i in range(1, len(parts), 2):
                body = parts[i + 1]
                lines = [
                    {"order": j, "text": t, "bbox": None, "size": None}
                    for j, t in enumerate(body.splitlines()) if t.strip()
                ]
                result.append({"page": int(parts[i]), "text": body, "method": "text", "lines": lines})
            return result
        lines = [{"order": j, "text": t, "bbox": None, "size": None} for j, t in enumerate(text.splitlines()) if t.strip()]
        return [{"page": None, "text": text, "method": "text", "lines": lines}]

    if ext == "docx":
        doc = Document(io.BytesIO(data))
        text = "\n".join(p.text for p in doc.paragraphs)
        lines = [{"order": j, "text": t, "bbox": None, "size": None} for j, t in enumerate(text.splitlines()) if t.strip()]
        return [{"page": None, "text": text, "method": "docx", "lines": lines}]

    raise ValueError(f"Unsupported file type: {ext}")


def clean_pages(pages):
    """Research-safe cleanup.

    Removes only recurring TEXTUAL headers/footers when they occur at page edges.
    Numeric/table values are preserved. Raw pages are never modified.
    """
    out = deepcopy(pages)
    if len(out) <= 1:
        out[0]["text"] = "\n".join(
            _norm_line(x) for x in out[0]["text"].splitlines() if _norm_line(x)
        )
        return out

    edge = Counter()
    n = len(out)

    for p in out:
        lines = [_norm_line(x) for x in p["text"].splitlines() if _norm_line(x)]
        edge_lines = lines[:7] + lines[-7:]
        for x in set(edge_lines):
            words = re.findall(r"[A-Za-z]+", x)
            # Never classify pure numbers / short table values as boilerplate.
            if 4 < len(x) <= 140 and len(words) >= 2:
                edge[x] += 1

    repeated = {x for x, c in edge.items() if c >= max(3, int(0.20 * n))}

    for p in out:
        original = [_norm_line(x) for x in p["text"].splitlines() if _norm_line(x)]
        cleaned = []
        last = len(original) - 1
        for i, x in enumerate(original):
            at_edge = i < 7 or i > last - 7
            if at_edge and x in repeated:
                continue
            cleaned.append(x)
        p["text"] = "\n".join(cleaned)

    return out


COMMON_SECTION_HEADINGS = [
    "chairman's message", "chairman's letter", "letter from the chairman",
    "letter from the ceo", "managing director's message",
    "directors' report", "board's report",
    "management discussion and analysis", "management discussion & analysis",
    "corporate governance report", "corporate social responsibility",
    "business responsibility report", "business responsibility and sustainability report",
    "standalone financial statements", "consolidated financial statements",
    "independent auditor's report", "clinical governance", "notice", "gri content index",
]
COMMON_SECTION_COMPACTS = [_compact(h) for h in COMMON_SECTION_HEADINGS]

# Boundaries are document structure, not requested outputs. Keeping this list compact is
# deliberate: it covers recurring annual-report sections while Annexure boundaries are
# discovered generically below.
BOUNDARY_SPECS = [
    ("Chairman Message", PRESETS["Chairman Message"]["aliases"]),
    ("CEO Message", PRESETS["CEO Message"]["aliases"]),
    ("Managing Director Message", PRESETS["Managing Director Message"]["aliases"]),
    ("Board's Report", ["board's report", "boards' report", "boards’ report"]),
    ("Directors' Report", ["directors' report", "directors’ report", "director's report", "directors' report to the shareholders", "directors’ report to the shareholders"]),
    ("Management Discussion & Analysis", PRESETS["Management Discussion & Analysis"]["aliases"]),
    ("Corporate Governance Report", ["corporate governance report", "report on corporate governance"]),
    ("Corporate Social Responsibility", ["corporate social responsibility", "corporate social responsibility report", "csr report"]),
    ("BRSR", PRESETS["Business Responsibility & Sustainability Report (BRSR)"]["aliases"]),
    ("BRR", PRESETS["Business Responsibility Report (BRR)"]["aliases"]),
    ("ESG Report", PRESETS["ESG Report"]["aliases"]),
    ("Sustainability Report", PRESETS["Sustainability Report"]["aliases"]),
    ("Clinical Governance", ["clinical governance"]),
    ("Standalone Financial Statements", ["standalone financial statements", "standalone financials"]),
    ("Consolidated Financial Statements", ["consolidated financial statements", "consolidated financials"]),
    ("Independent Auditor's Report", ["independent auditor's report", "independent auditors' report", "independent auditor’s report", "independent auditors’ report", "report on the audit of the standalone financial statements"]),
    ("Notice", ["notice", "notice of annual general meeting"]),
    ("GRI Index", ["gri index", "gri content index"]),
    # Front-matter boundaries keep leadership letters from swallowing later narrative pages.
    ("About Company", ["about the company", "about tcs", "about adani enterprises limited", "company overview", "company profile"]),
    ("Board of Directors", ["board of directors"]),
    ("Management Team", ["management team"]),
    ("The Year Gone By", ["the year gone by"]),
    ("Performance Highlights", ["performance highlights", "financial & operational highlights", "financial and operational highlights"]),
    ("Corporate Review", ["corporate review"]),
    ("Awards", ["awards", "awards and accolades"]),
]

_SECTION_MAP_CACHE = {}
_TOC_CACHE = {}


def _doc_key(pages):
    if not pages:
        return (0,)
    first = pages[0].get("text", "")[:700]
    middle = pages[len(pages) // 2].get("text", "")[:400]
    last = pages[-1].get("text", "")[-700:]
    return (len(pages), hash(first), hash(middle), hash(last))


def _page_lines(page):
    lines = page.get("lines") or []
    if lines:
        return lines
    return [
        {"order": i, "text": t, "bbox": None, "size": None}
        for i, t in enumerate(page.get("text", "").splitlines()) if _norm_line(t)
    ]


def _toc_like_page(page):
    text = page.get("text", "")
    low = text.lower()
    if re.search(r"(?im)^\s*contents?\s*$", text) or "co n te n ts" in low:
        return True
    compact_text = _compact(text)
    hits = sum(h in compact_text for h in COMMON_SECTION_COMPACTS)
    return hits >= 5


def _heading_variants(heading):
    raw = list(heading) if isinstance(heading, (list, tuple, set)) else [heading]
    out, seen = [], set()
    for h in raw:
        if not h:
            continue
        h = _norm_line(str(h)).replace("’", "'")
        variants = [h, re.sub(r"\s*\([^)]*\)\s*", " ", h).strip()]
        if "&" in h:
            variants.append(h.replace("&", "and"))
        if re.search(r"\band\b", h, re.I):
            variants.append(re.sub(r"\band\b", "&", h, flags=re.I))
        for v in variants:
            c = _compact(v)
            if c and c not in seen:
                seen.add(c)
                out.append(v)
    return out


def _window_candidates(page, aliases, max_width=4):
    """Find heading-like alias windows in native PDF reading order."""
    aliases = _heading_variants(aliases)
    ac = [(_compact(a), a) for a in aliases]
    lines = _page_lines(page)
    if not lines:
        return []

    sizes = [l.get("size") for l in lines if isinstance(l.get("size"), (int, float)) and 5 <= l.get("size") <= 15]
    body = sorted(sizes)[len(sizes) // 2] if sizes else 9.0
    height = float(page.get("height") or 800.0)
    out = []

    for i in range(len(lines)):
        for width in range(1, min(max_width, len(lines) - i) + 1):
            group = lines[i:i + width]
            text = " ".join(_norm_line(x.get("text", "")) for x in group).strip()
            wc = _compact(text)
            if not wc:
                continue
            for target, alias in ac:
                if not target:
                    continue
                exact = wc == target
                contained = target in wc and len(wc) <= len(target) + 30
                # Allow a short Annexure prefix/suffix around a genuine section title.
                annexure_contained = target in wc and "annexure" in wc and len(wc) <= len(target) + 45
                if not (exact or contained or annexure_contained):
                    continue

                score = 48 if exact else 42
                if annexure_contained:
                    score += 8

                max_size = max((x.get("size") or 0) for x in group)
                delta = max_size - body
                if max_size >= 16:
                    score += 18
                elif delta >= 3:
                    score += 14
                elif delta >= 1.5:
                    score += 8

                bboxes = [x.get("bbox") for x in group if x.get("bbox")]
                if bboxes:
                    x0 = min(b[0] for b in bboxes); y0 = min(b[1] for b in bboxes)
                    x1 = max(b[2] for b in bboxes); y1 = max(b[3] for b in bboxes)
                    if y0 / max(height, 1) < 0.18:
                        score += 8
                    elif y0 / max(height, 1) < 0.35:
                        score += 4
                    bbox = [x0, y0, x1, y1]
                else:
                    bbox = None
                    if i < 12:
                        score += 5

                letters = re.sub(r"[^A-Za-z]+", "", text)
                if letters and text.upper() == text and len(letters) >= 8:
                    score += 3

                out.append({
                    "score": score,
                    "line_order": group[0].get("order", i),
                    "line_end": group[-1].get("order", i + width - 1),
                    "bbox": bbox,
                    "matched_alias": alias,
                    "matched_text": text,
                })
    # De-duplicate equivalent windows.
    best = {}
    for c in out:
        k = (c["line_order"], c["matched_alias"])
        if k not in best or c["score"] > best[k]["score"]:
            best[k] = c
    return list(best.values())


def _edge_folios(page):
    """Likely printed report folios with x/y position; supports two-page spreads."""
    width = float(page.get("width") or 600.0)
    height = float(page.get("height") or 800.0)
    out = []
    for line in _page_lines(page):
        text = _norm_line(line.get("text", ""))
        bb = line.get("bbox")
        if not bb:
            continue
        x0, y0, x1, y1 = bb
        strict_edge = y0 < 0.06 * height or y1 > 0.94 * height
        header_footer = y0 < 0.12 * height or y1 > 0.90 * height
        if not header_footer:
            continue
        nums = []
        m = re.fullmatch(r"0*(\d{1,4})", text)
        if m and strict_edge:
            nums = [int(m.group(1))]
        elif len(text) <= 150 and header_footer:
            m = re.search(r"(?:\bI\s+|\|\s*|\s)0*(\d{1,4})\s*$", text)
            if m:
                nums = [int(m.group(1))]
        for n in nums:
            if 0 <= n <= 1500 and not (1900 <= n <= 2099):
                out.append({"number": n, "x": (x0 + x1) / 2, "y": (y0 + y1) / 2})
    return out


def _explicit_toc_page(page):
    text = page.get("text", "")
    low = re.sub(r"\s+", " ", text.lower())
    return bool(
        re.search(r"(?im)^\s*contents?\s*$", text)
        or "co n te n ts" in low
        or re.search(r"(?im)^\s*index\s*$", text)
    )


def _toc_printed_page(pages, aliases):
    key = (_doc_key(pages), tuple(_compact(x) for x in _heading_variants(aliases)))
    if key in _TOC_CACHE:
        return _TOC_CACHE[key]

    targets = [_compact(x) for x in _heading_variants(aliases)]
    answer = None
    for page in pages[:30]:
        if not _explicit_toc_page(page):
            continue
        lines = _page_lines(page)
        for i, line in enumerate(lines):
            text = _norm_line(line.get("text", "")).replace("’", "'")
            c = _compact(text)
            matched = None
            for target in targets:
                if target and (target in c) and len(c) <= len(target) + 40:
                    matched = target
                    break
            if not matched:
                continue

            # Same-line number: "267 Directors' Report" or "Chairman's Message 2".
            nums = [int(x) for x in re.findall(r"\b(\d{1,4})\b", text) if not (1900 <= int(x) <= 2099)]
            if nums:
                # Prefer a number at either edge of the entry.
                m1 = re.match(r"^\s*0*(\d{1,4})\b", text)
                m2 = re.search(r"\b0*(\d{1,4})\s*$", text)
                if m1 and not (1900 <= int(m1.group(1)) <= 2099):
                    answer = int(m1.group(1)); break
                if m2 and not (1900 <= int(m2.group(1)) <= 2099):
                    answer = int(m2.group(1)); break

            bb = line.get("bbox")
            if bb:
                yc = (bb[1] + bb[3]) / 2
                numeric = []
                for nline in lines:
                    nt = _norm_line(nline.get("text", ""))
                    nb = nline.get("bbox")
                    mm = re.fullmatch(r"0*(\d{1,4})", nt)
                    if not (mm and nb):
                        continue
                    num = int(mm.group(1))
                    if 1900 <= num <= 2099:
                        continue
                    ny = (nb[1] + nb[3]) / 2
                    dist = abs(ny - yc)
                    if dist <= 18:
                        numeric.append((dist, num))
                if numeric:
                    answer = min(numeric)[1]
                    break
        if answer is not None:
            break

    if len(_TOC_CACHE) > 200:
        _TOC_CACHE.clear()
    _TOC_CACHE[key] = answer
    return answer


def _anchor_printed_page(page, anchor, toc_page=None):
    if toc_page is not None:
        return toc_page
    folios = _edge_folios(page)
    if not folios:
        return None
    height = float(page.get("height") or 800.0)
    edge_dist = lambda f: min(f["y"], max(0.0, height - f["y"]))
    best_edge = min(edge_dist(f) for f in folios)
    edge_folios = [f for f in folios if edge_dist(f) <= best_edge + 12]
    if anchor.get("bbox"):
        x = (anchor["bbox"][0] + anchor["bbox"][2]) / 2
        return min(edge_folios, key=lambda f: abs(f["x"] - x))["number"]
    return edge_folios[0]["number"]


def _detect_section_anchor(pages, aliases, min_score=54):
    aliases = _heading_variants(aliases)
    toc_page = _toc_printed_page(pages, aliases)
    best = None
    target_compacts = [_compact(a) for a in aliases if _compact(a)]
    for idx, page in enumerate(pages):
        if _toc_like_page(page):
            continue
        page_compact = _compact(page.get("text", ""))
        if not any(t in page_compact for t in target_compacts):
            continue
        folios = {f["number"] for f in _edge_folios(page)}
        for cand in _window_candidates(page, aliases):
            score = cand["score"]
            if toc_page is not None and toc_page in folios:
                score += 30
            item = dict(cand)
            item.update({
                "index": idx,
                "pdf_page": page.get("page"),
                "score": score,
            })
            item["printed_page"] = _anchor_printed_page(page, item, toc_page=toc_page if toc_page in folios else None)
            rank = (score, -idx, -item["line_order"])
            if best is None or rank > best[0]:
                best = (rank, item)
    if not best or best[1]["score"] < min_score:
        return None
    # If TOC provided a page but the actual page could not expose its folio, retain the
    # TOC page only when the heading itself is visually strong.
    if best[1].get("printed_page") is None and toc_page is not None and best[1]["score"] >= 64:
        best[1]["printed_page"] = toc_page
    return best[1]


def _discover_annexure_anchors(pages):
    out = []
    pat = re.compile(r"^\s*annexure\s*[-:]?\s*([ivxlcdm]+|\d{1,2})\b", re.I)
    for idx, page in enumerate(pages):
        if _toc_like_page(page):
            continue
        height = float(page.get("height") or 800.0)
        lines = _page_lines(page)
        sizes = [l.get("size") for l in lines if isinstance(l.get("size"), (int, float)) and 5 <= l.get("size") <= 15]
        body = sorted(sizes)[len(sizes)//2] if sizes else 9.0
        for line in lines[:40]:
            text = _norm_line(line.get("text", ""))
            m = pat.match(text)
            if not m:
                continue
            bb = line.get("bbox")
            size = line.get("size") or body
            y0 = bb[1] if bb else 0
            if bb and y0 > 0.30 * height and size < body + 1.5:
                continue
            anchor = {
                "label": f"Annexure {m.group(1)}",
                "aliases": [text],
                "index": idx,
                "pdf_page": page.get("page"),
                "line_order": line.get("order", 0),
                "line_end": line.get("order", 0),
                "bbox": bb,
                "score": 70,
                "matched_alias": text,
                "matched_text": text,
            }
            anchor["printed_page"] = _anchor_printed_page(page, anchor)
            out.append(anchor)
            break
    return out


def _anchor_key(a):
    return (a["index"], a.get("line_order", 0))


def build_section_map(pages):
    key = _doc_key(pages)
    if key in _SECTION_MAP_CACHE:
        return _SECTION_MAP_CACHE[key]

    found = []
    for label, aliases in BOUNDARY_SPECS:
        min_score = 64 if label in {"Corporate Social Responsibility"} else 54
        a = _detect_section_anchor(pages, aliases, min_score=min_score)
        if a:
            a = dict(a); a["label"] = label; a["aliases"] = aliases
            found.append(a)
    found.extend(_discover_annexure_anchors(pages))

    # Merge duplicate/overlapping anchors created by nested names such as
    # "Sustainability Report" inside "Business Responsibility & Sustainability Report".
    merged = []
    for item in sorted(found, key=_anchor_key):
        replaced = False
        for j, old in enumerate(merged):
            if item["index"] != old["index"]:
                continue
            a0, a1 = item.get("line_order", 0), item.get("line_end", item.get("line_order", 0))
            b0, b1 = old.get("line_order", 0), old.get("line_end", old.get("line_order", 0))
            overlap = not (a1 < b0 - 1 or b1 < a0 - 1)
            mt = _compact(item.get("matched_text", "")); ot = _compact(old.get("matched_text", ""))
            related = mt in ot or ot in mt
            if overlap and related:
                rank_item = (item.get("score", 0), len(mt), -a0)
                rank_old = (old.get("score", 0), len(ot), -b0)
                if rank_item > rank_old:
                    merged[j] = item
                replaced = True
                break
        if not replaced:
            merged.append(item)
    result = sorted(merged, key=_anchor_key)

    if len(_SECTION_MAP_CACHE) >= 24:
        _SECTION_MAP_CACHE.clear()
    _SECTION_MAP_CACHE[key] = result
    return result


def _next_boundary_anchor(pages, start_anchor, requested_label=None):
    sk = _anchor_key(start_anchor)
    for item in build_section_map(pages):
        if _anchor_key(item) <= sk:
            continue
        # Board/Directors reports commonly contain several annexures. Treat the next
        # named major section as the boundary, not Annexure 1 merely because it exists.
        if requested_label in {"Board's Report", "Directors' Report"} and str(item.get("label", "")).lower().startswith("annexure"):
            continue
        return item
    return None


def _leadership_end_anchor(pages, start_anchor, boundary_anchor, label):
    """Use the last signature-role occurrence before the next major section.

    This handles reports whose next page is font-corrupted (common in older PDFs),
    while avoiding false MD extraction from a CEO title.
    """
    role_terms = {
        "Chairman Message": ["chairman", "chairperson"],
        "CEO Message": ["chief executive officer", "ceo"],
        "Managing Director Message": ["managing director"],
    }.get(label, [])
    if not role_terms:
        return boundary_anchor

    stop_idx = boundary_anchor["index"] if boundary_anchor else min(len(pages), start_anchor["index"] + 12)
    last_signature_idx = None
    for idx in range(start_anchor["index"], min(stop_idx, start_anchor["index"] + 12)):
        lines = [_norm_line(x.get("text", "")) for x in _page_lines(pages[idx]) if _norm_line(x.get("text", ""))]
        if not lines:
            continue
        tail = " ".join(lines[-30:]).lower().replace("’", "'")
        if any(term in tail for term in role_terms):
            last_signature_idx = idx
    if last_signature_idx is None:
        return boundary_anchor

    # Synthetic boundary at start of the physical page after the signature page.
    ni = last_signature_idx + 1
    if boundary_anchor and ni > boundary_anchor["index"]:
        return boundary_anchor
    if ni >= len(pages):
        return None
    return {
        "label": "Leadership signature end",
        "index": ni,
        "pdf_page": pages[ni].get("page"),
        "line_order": 0,
        "printed_page": None,
        "score": 100,
    }


def _selected_line_text(page, start_order=None, end_order=None, clean_page=None):
    lines = _page_lines(page)
    if not lines:
        return page.get("text", "").strip()
    clean_allowed = None
    if clean_page is not None:
        clean_allowed = Counter(_norm_line(x) for x in clean_page.get("text", "").splitlines() if _norm_line(x))
    chosen = []
    for line in lines:
        order = line.get("order", 0)
        if start_order is not None and order < start_order:
            continue
        if end_order is not None and order >= end_order:
            continue
        text = _norm_line(line.get("text", ""))
        if not text:
            continue
        if clean_allowed is not None:
            if clean_allowed[text] <= 0:
                continue
            clean_allowed[text] -= 1
        chosen.append(text)
    return "\n".join(chosen).strip()


def _payload_from_anchors(raw_pages, clean_pages_, start_anchor, boundary_anchor=None):
    start_idx = start_anchor["index"]
    boundary_idx = boundary_anchor["index"] if boundary_anchor else len(raw_pages)
    raw_parts, clean_parts = [], []
    used_pages = []

    include_boundary_page = False
    if boundary_anchor and boundary_idx < len(raw_pages):
        ps = start_anchor.get("printed_page")
        bp = boundary_anchor.get("printed_page")
        if ps is not None and bp is not None and bp > ps:
            folios = {f["number"] for f in _edge_folios(raw_pages[boundary_idx])}
            # A physical spread may contain both the last page of this section and
            # the first page of the next one (Apollo-style two-up PDFs).
            include_boundary_page = (bp - 1) in folios and bp in folios
    last_idx = boundary_idx if (boundary_anchor and include_boundary_page) else (boundary_idx - 1 if boundary_anchor else len(raw_pages) - 1)
    for idx in range(start_idx, last_idx + 1):
        if idx >= len(raw_pages):
            break
        start_order = start_anchor.get("line_order") if idx == start_idx else None
        end_order = None
        if boundary_anchor and include_boundary_page and idx == boundary_idx:
            end_order = boundary_anchor.get("line_order")
        if boundary_anchor and idx > boundary_idx:
            break

        r = _selected_line_text(raw_pages[idx], start_order=start_order, end_order=end_order)
        c = _selected_line_text(raw_pages[idx], start_order=start_order, end_order=end_order, clean_page=clean_pages_[idx])
        if r.strip():
            raw_parts.append(r.strip())
            clean_parts.append(c.strip() if c.strip() else r.strip())
            used_pages.append(raw_pages[idx].get("page"))

    if not used_pages:
        return None

    payload = {
        "start_page": min(p for p in used_pages if p is not None) if any(p is not None for p in used_pages) else None,
        "end_page": max(p for p in used_pages if p is not None) if any(p is not None for p in used_pages) else None,
        "text": "\n\n".join(clean_parts).strip(),
        "raw_text": "\n\n".join(raw_parts).strip(),
        "detection_confidence": "high" if start_anchor.get("score", 0) >= 64 else "medium",
    }
    ps = start_anchor.get("printed_page")
    if ps is not None:
        payload["printed_start_page"] = ps
    if boundary_anchor and ps is not None and boundary_anchor.get("printed_page") is not None and boundary_anchor["printed_page"] > ps:
        payload["printed_end_page"] = boundary_anchor["printed_page"] - 1
    return payload


def extract_preset(raw_pages, clean_pages_, label):
    cfg = PRESETS[label]
    start = _detect_section_anchor(clean_pages_, cfg["aliases"])
    if not start:
        return None
    boundary = _next_boundary_anchor(clean_pages_, start, requested_label=label)
    if label in {"Chairman Message", "CEO Message", "Managing Director Message"}:
        boundary = _leadership_end_anchor(clean_pages_, start, boundary, label)
    return _payload_from_anchors(raw_pages, clean_pages_, start, boundary)


def extract_custom(raw_pages, clean_pages_, heading):
    heading = _norm_line(heading)
    if not heading:
        return None

    # Common custom labels get their precise aliases; this prevents a requested
    # "Directors' Report" from silently returning a different "Board's Report".
    alias_map = {
        _compact("Directors' Report"): ["directors' report", "directors’ report", "director's report", "directors' report to the shareholders", "directors’ report to the shareholders"],
        _compact("Board's Report"): ["board's report", "boards' report", "boards’ report"],
        _compact("Corporate Governance Report"): ["corporate governance report", "report on corporate governance"],
        _compact("Corporate social responsibility"): ["corporate social responsibility", "corporate social responsibility report", "csr report"],
    }
    aliases = alias_map.get(_compact(heading), [heading])
    start = _detect_section_anchor(clean_pages_, aliases)
    if not start:
        return None
    requested = "Board's Report" if _compact(heading) == _compact("Board's Report") else ("Directors' Report" if _compact(heading) == _compact("Directors' Report") else heading)
    boundary = _next_boundary_anchor(clean_pages_, start, requested_label=requested)
    return _payload_from_anchors(raw_pages, clean_pages_, start, boundary)


def combine_sections(raw_pages, clean_pages_, sections, labels, combined_label):
    present = [(label, sections[label]) for label in labels if label in sections]
    if not present:
        return None
    clean_parts, raw_parts, page_ranges, printed_ranges = [], [], [], []
    for label, sec in present:
        if sec.get("text", "").strip():
            clean_parts.append(sec["text"].strip())
            raw_parts.append(sec.get("raw_text", sec["text"]).strip())
        if sec.get("start_page") is not None:
            page_ranges.append({"label": label, "start_page": sec.get("start_page"), "end_page": sec.get("end_page")})
        if sec.get("printed_start_page") is not None:
            printed_ranges.append({"label": label, "start_page": sec.get("printed_start_page"), "end_page": sec.get("printed_end_page", sec.get("printed_start_page"))})
    payload = {
        "start_page": min((r["start_page"] for r in page_ranges), default=None),
        "end_page": max((r["end_page"] for r in page_ranges), default=None),
        "page_ranges": page_ranges,
        "text": "\n\n".join(clean_parts).strip(),
        "raw_text": "\n\n".join(raw_parts).strip(),
        "combined_from": [label for label, _ in present],
    }
    if printed_ranges:
        payload["printed_page_ranges"] = printed_ranges
        payload["printed_start_page"] = min(r["start_page"] for r in printed_ranges)
        payload["printed_end_page"] = max(r["end_page"] for r in printed_ranges)
    return payload


def raw_full_text(pages):
    if len(pages) == 1 and pages[0].get("page") is None:
        return pages[0]["text"].strip()
    return "\n\n".join(f"===== PAGE {p['page']} =====\n\n{p['text'].strip()}" for p in pages).strip()


def search_pages(pages, query, limit=50):
    query = query.strip()
    if not query:
        return []
    pat = re.compile(re.escape(query), re.I)
    hits = []
    for p in pages:
        for m in pat.finditer(p["text"]):
            a = max(0, m.start() - 90)
            b = min(len(p["text"]), m.end() + 150)
            hits.append({
                "page": p.get("page"),
                "snippet": re.sub(r"\s+", " ", p["text"][a:b]).strip(),
            })
            if len(hits) >= limit:
                return hits
    return hits


def base_stem(filename):
    stem = filename.rsplit(".", 1)[0]
    stem = re.sub(r"(?:_Annual_Report)?_Full_Text$", "", stem, flags=re.I)
    return stem


def infer_metadata(filename, pages):
    stem = base_stem(filename)
    front = "\n".join(p.get("text", "")[:12000] for p in pages[:12])
    year = None

    explicit_patterns = [
        r"(?:integrated\s+)?annual\s+report(?:\s*&\s*accounts)?(?:\s+for\s+the\s+year)?\s*[:\-]?\s*(20\d{2})\s*[-–—/]\s*(20\d{2}|\d{2})",
        r"annual\s+report.{0,100}?(20\d{2})\s*[-–—/]\s*(20\d{2}|\d{2})",
        r"(?:financial\s+year|FY)\s*[:\-]?\s*(20\d{2})\s*[-–—/]\s*(20\d{2}|\d{2})",
    ]
    for pattern in explicit_patterns:
        m = re.search(pattern, front, re.I | re.S)
        if m:
            year = f"{m.group(1)}-{m.group(2)[-2:]}"
            break
    if not year:
        m = re.search(r"year\s+ended\s+(?:on\s+)?31(?:st)?\s+March[,]?\s+(20\d{2})", front, re.I)
        if m:
            end_year = int(m.group(1)); year = f"{end_year - 1}-{str(end_year)[-2:]}"
    if not year:
        m = re.search(r"\b(20\d{2})\s*[-–—_/]\s*(\d{2,4})\b", filename)
        if m:
            year = f"{m.group(1)}-{m.group(2)[-2:]}"
        else:
            m = re.search(r"\b(20\d{2})\b", filename)
            if m:
                year = m.group(1)

    # Filename company is trusted only when it contains meaningful alphabetic content.
    company = re.sub(r"\s*\(\d+\)\s*$", "", stem)
    company = re.sub(r"[_-]+", " ", company)
    company = re.sub(r"\b(?:Integrated\s+)?Annual\s+Report\b", " ", company, flags=re.I)
    company = re.sub(r"\b20\d{2}(?:\s*[-–—_/]?\s*\d{2,4})?\b", " ", company)
    company = re.sub(r"\s+", " ", company).strip(" _-")
    generic = not company or not re.search(r"[A-Za-z]{3,}", company) or company.lower() in {"report", "document", "annual report"}

    if generic:
        sample = "\n".join(p.get("text", "") for p in pages[:50])
        # Strong labelled fields first.
        labelled = re.search(
            r"(?is)(?:name\s+of\s+(?:the\s+)?(?:company|listed\s+entity)|company\s+name)\s*[:\-]?\s*([A-Z][A-Za-z0-9&.,'()\- ]{2,120}?(?:Limited|Ltd\.?))",
            sample,
        )
        if labelled:
            company = labelled.group(1)
        else:
            candidates = re.findall(r"(?im)^\s*(?:For\s+)?([A-Z][A-Za-z0-9&.,'()\- ]{2,110}?(?:Limited|Ltd\.?))\s*$", sample)
            blocked_terms = [
                "stock exchange", "bse limited", "depository", "securities", "auditor", "assurance",
                "chartered accountants", "registrar", "trustee", "bank limited",
            ]
            cleaned = []
            for c in candidates:
                c = re.sub(r"^For\s+", "", c, flags=re.I)
                c = re.sub(r"\s+", " ", c).strip(" ,.-")
                if any(b in c.lower() for b in blocked_terms):
                    continue
                cleaned.append(c)
            if cleaned:
                company = Counter(cleaned).most_common(1)[0][0]

    company = re.sub(r"^For\s+", "", company or "", flags=re.I)
    company = re.sub(r"\s+", " ", company).strip(" ,.-")
    company = re.sub(r"\s+Limited$", "", company, flags=re.I).strip()
    return {"company": company or stem.replace("_", " ").strip(), "year": year or "Year not detected", "source_file": filename}



def _docx_bytes(title, text):
    d = Document()
    d.add_heading(title, 0)
    for block in text.split("\n\n"):
        if block.strip():
            d.add_paragraph(block.strip())
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


def _csv_bytes(rows):
    s = io.StringIO()
    w = csv.DictWriter(s, fieldnames=rows[0].keys())
    w.writeheader()
    w.writerows(rows)
    return s.getvalue().encode("utf-8-sig")


def _pdf_bytes(title, text):
    """Create a searchable text PDF using PyMuPDF Story (supports multi-page Unicode text)."""
    buf = io.BytesIO()
    writer = fitz.DocumentWriter(buf)
    mediabox = fitz.paper_rect("a4")
    rect = fitz.Rect(42, 42, mediabox.width - 42, mediabox.height - 42)
    body = html.escape(text).replace("\n", "<br>")
    story = fitz.Story(
        html=f"<h2>{html.escape(title)}</h2><div>{body}</div>",
        user_css="body {font-family: sans-serif; font-size: 9pt; line-height: 1.25;} h2 {font-size: 14pt;}"
    )
    more = True
    while more:
        dev = writer.begin_page(mediabox)
        more, _ = story.place(rect)
        story.draw(dev)
        writer.end_page()
    writer.close()
    return buf.getvalue()


def safe_label(label):
    return {
        "Full Report": "Annual_Report_Full_Text",
        "Leadership Messages (Combined)": "Chairman_CEO_MD_Messages",
        "Chairman Message": "Chairman_Message",
        "CEO Message": "CEO_Message",
        "Managing Director Message": "Managing_Director_Message",
        "Management Discussion & Analysis": "MDA",
        "Business Responsibility & Sustainability Report (BRSR)": "BRSR",
        "Business Responsibility Report (BRR)": "BRR",
        "ESG Report": "ESG_Report",
        "Sustainability Report": "Sustainability_Report",
    }.get(label, re.sub(r"[^A-Za-z0-9_-]+", "_", label).strip("_"))

# Backward-compatible internal alias.
_safe_label = safe_label


def format_file(stem, label, payload, fmt, raw_pages, source_bytes=None, source_is_pdf=False, all_sections=None):
    safe = safe_label(label)
    name = f"{stem}_{safe}"
    text = payload["text"]

    if fmt == "TXT":
        return name + ".txt", text.encode("utf-8")
    if fmt == "MD":
        return name + ".md", (f"# {label}\n\n{text}\n").encode("utf-8")
    if fmt == "DOCX":
        return name + ".docx", _docx_bytes(label, text)
    if fmt == "PDF":
        if label == "Full Report" and source_is_pdf and source_bytes is not None:
            return name + ".pdf", source_bytes
        return name + ".pdf", _pdf_bytes(label, text)
    if fmt == "JSON":
        if label == "Full Report":
            obj = {
                "file": stem,
                "page_count": len(raw_pages) if raw_pages and raw_pages[0].get("page") is not None else None,
                "pages": raw_pages,
                "text": text,
                "sections": {
                    k: {"start_page": v.get("start_page"), "end_page": v.get("end_page")}
                    for k, v in (all_sections or {}).items()
                },
            }
        else:
            obj = payload
        return name + ".json", json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
    if fmt == "CSV":
        if label == "Full Report":
            rows = [{"page": p.get("page"), "text": p["text"]} for p in raw_pages]
        else:
            rows = [{
                "section": label,
                "start_page": payload.get("start_page"),
                "end_page": payload.get("end_page"),
                "text": text,
            }]
        return name + ".csv", _csv_bytes(rows)
    raise ValueError(fmt)


def make_zip(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return b.getvalue()


def manifest_csv(results):
    s = io.StringIO()
    fields = ["company", "year", "source_file", "section", "start_page", "end_page", "formats"]
    w = csv.DictWriter(s, fieldnames=fields)
    w.writeheader()
    for r in results:
        w.writerow({
            "company": r["meta"]["company"], "year": r["meta"]["year"],
            "source_file": r["name"], "section": "Full Report",
            "start_page": 1 if len(r["raw_pages"]) > 1 else "",
            "end_page": len(r["raw_pages"]) if len(r["raw_pages"]) > 1 else "",
            "formats": ", ".join(r["formats"]),
        })
        for label, sec in r["sections"].items():
            w.writerow({
                "company": r["meta"]["company"], "year": r["meta"]["year"],
                "source_file": r["name"], "section": label,
                "start_page": sec.get("start_page", ""), "end_page": sec.get("end_page", ""),
                "formats": ", ".join(r["formats"]),
            })
    return s.getvalue().encode("utf-8-sig")


def clean_background_pdf(pdf_bytes, threshold=215):
    """Create a separate grayscale background-clean copy; original is never modified."""
    src = fitz.open(stream=pdf_bytes, filetype="pdf")
    out = fitz.open()
    lut = [x if x < threshold else 255 for x in range(256)]
    for p in src:
        pix = p.get_pixmap(matrix=fitz.Matrix(1.25, 1.25), colorspace=fitz.csGRAY, alpha=False)
        img = Image.frombytes("L", (pix.width, pix.height), pix.samples).point(lut)
        b = io.BytesIO()
        img.save(b, "PNG")
        q = out.new_page(width=p.rect.width, height=p.rect.height)
        q.insert_image(q.rect, stream=b.getvalue())
    return out.tobytes(deflate=True)
