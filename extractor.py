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
            "message from the chairperson", "letter from the chairman",
        ],
        "end": [
            "message from the managing director", "managing director's message",
            "message from the ceo", "ceo message", "about the company",
            "about adani enterprises", "company overview", "company profile",
        ],
    },
    "CEO Message": {
        "aliases": ["message from the ceo", "ceo message", "ceo's message", "letter from the ceo"],
        "end": [
            "message from the managing director", "managing director's message",
            "about the company", "company overview", "company profile", "business portfolio",
        ],
    },
    "Managing Director Message": {
        "aliases": [
            "message from the managing director", "managing director's message",
            "md message", "letter from the managing director",
        ],
        "end": ["about adani enterprises", "about the company", "company overview", "company profile", "business portfolio"],
    },
    "Management Discussion & Analysis": {
        "aliases": ["management discussion & analysis", "management discussion and analysis", "md&a"],
        "end": ["corporate governance report", "corporate governance"],
    },
    "Business Responsibility & Sustainability Report (BRSR)": {
        "aliases": [
            "business responsibility & sustainability report",
            "business responsibility and sustainability report", "brsr report", "brsr",
        ],
        "end": [
            "standalone financial statements", "independent auditor's report",
            "independent auditors' report", "report on the audit of the standalone",
        ],
    },
    "Business Responsibility Report (BRR)": {
        "aliases": ["business responsibility report", "brr report"],
        "end": [
            "standalone financial statements", "independent auditor's report",
            "independent auditors' report", "report on the audit of the standalone",
        ],
    },
    "ESG Report": {
        "aliases": ["esg report", "environmental social and governance report"],
        "end": [
            "standalone financial statements", "independent auditor's report",
            "independent auditors' report", "report on the audit of the standalone",
        ],
    },
    "Sustainability Report": {
        "aliases": ["sustainability report"],
        "end": [
            "standalone financial statements", "independent auditor's report",
            "independent auditors' report", "report on the audit of the standalone",
        ],
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


def extract_source(name, data):
    ext = name.rsplit(".", 1)[-1].lower()
    if ext == "pdf":
        doc = fitz.open(stream=data, filetype="pdf")
        pages = []
        for i, page in enumerate(doc):
            text = page.get_text("text")
            method = "native"
            if len(text.strip()) < 40:
                text = _ocr(page)
                method = "ocr"
            pages.append({"page": i + 1, "text": text, "method": method})
        return pages

    if ext in ("txt", "md"):
        text = data.decode("utf-8", errors="ignore")
        parts = re.split(r"(?m)^\s*=====\s*PAGE\s+(\d+)\s*=====\s*$", text)
        if len(parts) > 2:
            return [
                {"page": int(parts[i]), "text": parts[i + 1], "method": "text"}
                for i in range(1, len(parts), 2)
            ]
        return [{"page": None, "text": text, "method": "text"}]

    if ext == "docx":
        doc = Document(io.BytesIO(data))
        return [{"page": None, "text": "\n".join(p.text for p in doc.paragraphs), "method": "docx"}]

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


SECTION_GROUPS = [
    # Strategic review / narrative sections
    [
        "business model",
        "stakeholder engagement",
        "double materiality",
        "risk and opportunities",
        "strategy",
        "business segment performance review",
        "environment, social and governance",
        "environment social and governance",
    ],
    # ESG narrative sections
    [
        "environment, social and governance",
        "environment social and governance",
        "promoting environmental stewardship",
        "climate action",
        "biodiversity management",
        "our people",
        "occupational health and safety",
        "customer relations",
        "corporate social responsibility",
        "responsible supply chain",
        "corporate governance",
        "board of directors",
        "global tax and other contributions",
        "corporate information",
    ],
    # Statutory / financial sections
    [
        "corporate information",
        "directors' report",
        "director's report",
        "board's report",
        "management discussion & analysis",
        "management discussion and analysis",
        "corporate governance report",
        "business responsibility & sustainability report",
        "business responsibility and sustainability report",
        "business responsibility report",
        "standalone financial statements",
        "consolidated financial statements",
        "notice",
        "gri index",
        "ungc index",
        "abbreviations",
    ],
]

COMMON_SECTION_HEADINGS = list(dict.fromkeys(h for group in SECTION_GROUPS for h in group))

def _toc_like_page(text):
    low = text.lower()
    if "contents" in low:
        return True
    hits = sum(_compact(h) in _compact(text) for h in COMMON_SECTION_HEADINGS)
    return hits >= 5

def _toc_printed_page(pages, heading):
    """Return the printed report page from an early Contents page when available."""
    h = heading.strip()

    variants = [h]
    if "&" in h:
        variants.append(h.replace("&", "and"))
    if " and " in h.lower():
        variants.append(re.sub(r"\band\b", "&", h, flags=re.I))

    # Remove common abbreviations in parentheses for TOC matching.
    variants += [re.sub(r"\s*\([^)]*\)\s*", " ", v).strip() for v in list(variants)]

    for p in pages[:20]:
        text = p["text"]
        if "contents" not in text.lower():
            continue
        flat = re.sub(r"\s+", " ", text)

        for variant in dict.fromkeys(variants):
            words = re.findall(r"[A-Za-z0-9]+", variant)
            if not words:
                continue
            phrase = r"[\s&'’\-]+".join(re.escape(w) for w in words)
            m = re.search(rf"\b(\d{{1,4}})\s+{phrase}\b", flat, re.I)
            if m:
                return int(m.group(1))
    return None


def _contains_printed_page_number(text, number):
    if number is None:
        return False
    lines = [_norm_line(x) for x in text.splitlines() if _norm_line(x)]
    return str(number) in lines[:25] or str(number) in lines[-25:]

def _heading_score(text, heading, max_lines=180):
    """Score whether a phrase is functioning as a page heading, not merely a body reference."""
    target = _compact(heading)
    lines = [_norm_line(x) for x in text.splitlines()[:max_lines] if _norm_line(x)]
    compact = [_compact(x) for x in lines]
    best = 0

    for i in range(len(compact)):
        # Exact one-line heading.
        if compact[i] == target:
            score = 10
            if lines[i].isupper():
                score += 15
            if i < 20:
                score += 8
            best = max(best, score)

        # PDF extraction often splits a heading over 2-3 lines.
        for width in (2, 3):
            if i + width <= len(compact) and "".join(compact[i:i+width]) == target:
                score = 9
                joined_original = " ".join(lines[i:i+width])
                if joined_original.isupper():
                    score += 15
                if i < 20:
                    score += 8
                best = max(best, score)

    return best

def _heading_match(text, heading, max_lines=180):
    return _heading_score(text, heading, max_lines=max_lines) > 0

def _best_heading_page(pages, heading, start_after=-1):
    toc_num = _toc_printed_page(pages, heading)
    candidates = []

    for i in range(start_after + 1, len(pages)):
        if _toc_like_page(pages[i]["text"]):
            continue
        score = _heading_score(pages[i]["text"], heading)
        if score <= 0:
            continue

        if _contains_printed_page_number(pages[i]["text"], toc_num):
            score += 100

        candidates.append((i, score))

    if not candidates:
        return None

    # If TOC mapping identifies the real page, it dominates.
    mapped = [c for c in candidates if c[1] >= 100]
    if mapped:
        return min(mapped, key=lambda x: x[0])[0]

    # Otherwise prefer the earliest heading occurrence. This is safer than a later
    # body/table repeat for arbitrary user-requested sections.
    return min(candidates, key=lambda x: x[0])[0]


def _canonical_custom_heading(heading):
    c = _compact(heading)
    aliases = {
        _compact("BRSR"): _compact("business responsibility & sustainability report"),
        _compact("BRR"): _compact("business responsibility report"),
        _compact("MDA"): _compact("management discussion & analysis"),
        _compact("MD&A"): _compact("management discussion & analysis"),
    }
    return aliases.get(c, c)

def _group_for_heading(heading):
    target = _canonical_custom_heading(heading)
    for group in SECTION_GROUPS:
        comps = [_compact(x) for x in group]
        if target in comps:
            return group, comps.index(target)
    return None, None


def _alias_hit(text, alias):
    return _compact(alias) in _compact(text)


def _best_start(pages, label, aliases):
    best = None
    for i, p in enumerate(pages):
        text = p["text"]
        hits = sum(_alias_hit(text, a) for a in aliases)
        if not hits:
            continue
        low = text.lower()
        score = hits * 4
        if "contents" in low:
            score -= 12
        if label in {"Chairman Message", "CEO Message", "Managing Director Message"} and ("dear stakeholders" in low or "dear shareholders" in low):
            score += 10
        elif label == "Management Discussion & Analysis":
            if "global econom" in low or "economic overview" in low:
                score += 5
        elif label in {"Business Responsibility & Sustainability Report (BRSR)", "Business Responsibility Report (BRR)", "ESG Report", "Sustainability Report"}:
            if "section a" in low or "general disclosures" in low:
                score += 10
        if best is None or score > best[0]:
            best = (score, i)
    return None if best is None else best[1]


def _next_boundary(pages, start, markers):
    markers = [_compact(x) for x in markers]
    for i in range(start + 1, len(pages)):
        # Restrict boundary matching to the top portion to avoid cross-references in body text.
        head = _compact(pages[i]["text"][:2200])
        if any(m in head for m in markers):
            return i - 1
    return len(pages) - 1


def _payload(raw_pages, clean_pages_, start, end):
    raw_text = "\n\n".join(p["text"].strip() for p in raw_pages[start:end + 1]).strip()
    clean_text = "\n\n".join(p["text"].strip() for p in clean_pages_[start:end + 1]).strip()
    return {
        "start_page": raw_pages[start].get("page"),
        "end_page": raw_pages[end].get("page"),
        "text": clean_text,
        "raw_text": raw_text,
    }


def extract_preset(raw_pages, clean_pages_, label):
    cfg = PRESETS[label]
    start = _best_start(clean_pages_, label, cfg["aliases"])
    if start is None:
        return None
    end = _next_boundary(clean_pages_, start, cfg["end"])
    return _payload(raw_pages, clean_pages_, start, end)


def extract_custom(raw_pages, clean_pages_, heading):
    """Extract a named section while avoiding TOC and body-reference false positives."""
    heading = heading.strip()
    if not heading:
        return None

    canon = _canonical_custom_heading(heading)
    search_heading = heading
    if canon == _compact("business responsibility & sustainability report"):
        search_heading = "business responsibility & sustainability report"
    elif canon == _compact("business responsibility report"):
        search_heading = "business responsibility report"
    elif canon == _compact("management discussion & analysis"):
        search_heading = "management discussion & analysis"

    start = _best_heading_page(clean_pages_, search_heading)
    if start is None:
        return None

    group, pos = _group_for_heading(heading)
    if group is not None:
        # The immediate next recognised section in the same ordered family is the boundary.
        for next_heading in group[pos + 1:]:
            nxt = _best_heading_page(clean_pages_, next_heading, start_after=start)
            if nxt is not None:
                return _payload(raw_pages, clean_pages_, start, nxt - 1)

    # Unknown custom heading: use the nearest strong recognised section after it.
    candidates = []
    for known in COMMON_SECTION_HEADINGS:
        nxt = _best_heading_page(clean_pages_, known, start_after=start)
        if nxt is not None:
            candidates.append(nxt)
    if candidates:
        return _payload(raw_pages, clean_pages_, start, min(candidates) - 1)

    return _payload(raw_pages, clean_pages_, start, len(clean_pages_) - 1)


def combine_sections(raw_pages, clean_pages_, sections, labels, combined_label):
    present = [(label, sections[label]) for label in labels if label in sections]
    if not present:
        return None

    starts = [sec["start_page"] for _, sec in present if sec.get("start_page") is not None]
    ends = [sec["end_page"] for _, sec in present if sec.get("end_page") is not None]
    if not starts or not ends:
        return None

    start_page, end_page = min(starts), max(ends)
    page_to_index = {p.get("page"): i for i, p in enumerate(raw_pages)}
    if start_page not in page_to_index or end_page not in page_to_index:
        return None

    return _payload(raw_pages, clean_pages_, page_to_index[start_page], page_to_index[end_page])


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
    joined = " ".join(p.get("text", "")[:5000] for p in pages[:15])
    search_text = f"{joined} {filename}"
    year = None

    patterns = [
        r"(?:financial\s+year|FY)\s*[:\-]?\s*(20\d{2})\s*[-–—/]\s*(\d{2,4})",
        r"(?:integrated\s+)?annual\s+report\s*(?:FY\s*)?(20\d{2})\s*[-–—/]\s*(\d{2,4})",
        r"\b(20\d{2})\s*[-–—/]\s*(\d{2})\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, search_text, re.I)
        if m:
            year = f"{m.group(1)}-{m.group(2)[-2:]}"
            break
    if not year:
        m = re.search(r"\b(20\d{2})\b", filename)
        if m:
            year = m.group(1)

    company = re.sub(r"\s*\(\d+\)\s*$", "", stem)
    company = re.sub(r"[_-]+", " ", company)
    company = re.sub(r"\b(?:Integrated\s+)?Annual\s+Report\b", " ", company, flags=re.I)
    company = re.sub(
        r"\b(?:Full\s*Text|Outputs?|Output|BRSR|BRR|ESG|MDA|"
        r"Management\s+Discussion(?:\s*&\s*Analysis)?|Chairman(?:'s)?\s+Message|"
        r"CEO\s+Message|MD\s+Message)\b",
        " ", company, flags=re.I,
    )
    company = re.sub(r"\b20\d{2}(?:\s*[-–—_/]?\s*\d{2,4})?\b", " ", company)
    company = re.sub(r"\s+", " ", company).strip(" _-") or stem.replace("_", " ").strip()
    return {"company": company, "year": year or "Year not detected", "source_file": filename}


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
