"""V15 page-quality / provenance layer.

Additive by design.  The frozen V14 structural engine remains authoritative for
section detection and boundaries.  This module audits the page representation
chosen by the production extractor and selectively compares suspicious pages with
OCR without changing section boundaries.
"""
from __future__ import annotations

import copy
import hashlib
import math
import re
from typing import Dict, List, Tuple

import fitz

import extractor as core

V15_QUALITY_SCHEMA = "v15.1-page-quality-provenance-1"

# Common visible signs of broken character decoding.  This is deliberately a
# screening signal, not a classifier: a flagged page is compared with OCR rather
# than automatically replaced.
_MOJIBAKE_RE = re.compile(r"(?:Ã|Â|â€|â€™|â€œ|â€\x9d|â€“|â€”|ƒ|œ|ž|�)")
_WORD_RE = re.compile(r"\b\w+\b", re.UNICODE)


def _word_count(text: str) -> int:
    return len(_WORD_RE.findall(text or ""))


def _alpha_ratio(text: str) -> float:
    chars = [c for c in (text or "") if not c.isspace()]
    if not chars:
        return 0.0
    return sum(c.isalpha() for c in chars) / len(chars)


def _mojibake_score(text: str) -> float:
    s = text or ""
    if not s:
        return 0.0
    return len(_MOJIBAKE_RE.findall(s)) / max(len(s), 1)


def _replacement_ratio(text: str) -> float:
    s = text or ""
    return (s.count("�") / len(s)) if s else 0.0


def _text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", errors="ignore")).hexdigest()


def _is_suspicious(text: str, baseline_method: str = "") -> Tuple[bool, List[str]]:
    """Return conservative screening signals.  No signal alone replaces text."""
    reasons: List[str] = []
    wc = _word_count(text)
    alpha = _alpha_ratio(text)
    moj = _mojibake_score(text)
    repl = _replacement_ratio(text)
    chars = len((text or "").strip())

    if chars < 40:
        reasons.append("VERY_LOW_TEXT")
    if moj >= 0.0015:
        reasons.append("MOJIBAKE_SIGNAL")
    if repl >= 0.0005:
        reasons.append("REPLACEMENT_CHARACTER_SIGNAL")
    # Numeric/table pages can legitimately have a low alphabetic share.  Low
    # alpha is therefore only supporting evidence when corruption is also seen.
    if wc >= 30 and alpha < 0.12 and (moj > 0 or repl > 0):
        reasons.append("LOW_ALPHA_WITH_CORRUPTION_SIGNAL")
    if baseline_method == "ocr":
        reasons.append("PRODUCTION_OCR_PAGE")

    return bool(reasons), reasons


def _choose_text(baseline_text: str, ocr_text: str, reasons: List[str]) -> Tuple[str, str, str]:
    """Return (decision, selected_text, rationale).

    REVIEW deliberately keeps the production representation.  This prevents a
    cleaner-but-much-shorter OCR result from silently deleting information.
    """
    bw = _word_count(baseline_text)
    ow = _word_count(ocr_text)
    bmoj = _mojibake_score(baseline_text)
    omoj = _mojibake_score(ocr_text)
    ba = _alpha_ratio(baseline_text)
    oa = _alpha_ratio(ocr_text)
    retention = (ow / bw) if bw else (math.inf if ow else 0.0)

    if bw <= 3 and ow >= 8:
        return "OCR", ocr_text, "Production text nearly empty; OCR recovered substantive text."

    corruption_improved = bmoj >= 0.0015 and omoj <= max(0.0002, bmoj * 0.25)
    alpha_improved = oa >= ba + 0.08

    if bw > 0 and corruption_improved and retention >= 0.70:
        return "OCR", ocr_text, "OCR materially reduced corruption while retaining >=70% of production words."

    if bw > 0 and corruption_improved and 0.45 <= retention < 0.70:
        return "REVIEW", baseline_text, "OCR is cleaner but loses substantial content; manual review required."

    if bw > 0 and alpha_improved and retention >= 0.85 and "VERY_LOW_TEXT" in reasons:
        return "OCR", ocr_text, "OCR recovered a fuller plausible text representation."

    if not reasons:
        return "NATIVE", baseline_text, "No page-quality screening signal."

    return "REVIEW", baseline_text, "Page is suspicious, but OCR is not demonstrably safer."


def _ocr_page(page: fitz.Page) -> str:
    # Reuse production OCR settings so comparison is reproducible with the main
    # extractor rather than introducing a second OCR configuration.
    return core._ocr(page)


def build_quality_pages(pdf_bytes: bytes, baseline_pages: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """Return (quality_selected_pages, page_quality_manifest).

    Performance rule: reuse the production page representation first and open the
    PDF only when a non-OCR page is actually suspicious enough to require an OCR
    comparison.  V14 structural boundaries always continue to use baseline_pages.
    """
    selected = copy.deepcopy(baseline_pages)
    manifest: List[Dict] = []

    # First pass is cheap and uses information V14 already produced.  This avoids
    # reopening / walking a large PDF when every page is already trustworthy.
    page_state = []
    needs_ocr = []
    for i, baseline in enumerate(baseline_pages):
        baseline_text = str(baseline.get("text") or "")
        baseline_method = str(baseline.get("method") or "native").lower()
        native_text = str(baseline.get("native_text") or baseline_text)
        suspicious, reasons = _is_suspicious(baseline_text, baseline_method)
        page_state.append((baseline_text, baseline_method, native_text, suspicious, reasons))
        if suspicious and baseline_method != "ocr":
            needs_ocr.append(i)

    ocr_by_index: Dict[int, str] = {}
    ocr_error_by_index: Dict[int, str] = {}
    if needs_ocr:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            for i in needs_ocr:
                if i >= len(doc):
                    continue
                try:
                    ocr_by_index[i] = _ocr_page(doc[i]) or ""
                except Exception as exc:
                    ocr_error_by_index[i] = f"OCR_ERROR:{type(exc).__name__}"
        finally:
            doc.close()

    for i, baseline in enumerate(baseline_pages):
        baseline_text, baseline_method, native_text, suspicious, reasons = page_state[i]
        ocr_text = ""

        # If production already selected OCR, reuse it exactly and do not OCR twice.
        if baseline_method == "ocr" and baseline_text.strip():
            ocr_text = baseline_text
            decision = "OCR"
            final_text = baseline_text
            rationale = "Production extractor already selected OCR for this page."
        else:
            if i in ocr_error_by_index:
                reasons = reasons + [ocr_error_by_index[i]]
            ocr_text = ocr_by_index.get(i, "")

            if suspicious and ocr_text.strip():
                decision, final_text, rationale = _choose_text(baseline_text, ocr_text, reasons)
            elif suspicious:
                decision, final_text, rationale = (
                    "REVIEW", baseline_text,
                    "Screened as suspicious but OCR returned no usable text.",
                )
            else:
                decision, final_text, rationale = "NATIVE", baseline_text, "No page-quality screening signal."

        if i < len(selected):
            selected[i]["text"] = final_text
            selected[i]["quality_decision"] = decision
            selected[i]["quality_schema"] = V15_QUALITY_SCHEMA
            selected[i]["quality_source_method"] = baseline_method
            if decision == "OCR" and baseline_method != "ocr":
                selected[i]["method"] = "ocr"
                selected[i]["lines"] = [
                    {"order": j, "text": t, "bbox": None, "size": None, "layout_class": "text"}
                    for j, t in enumerate(final_text.splitlines()) if t.strip()
                ]

        bw = _word_count(baseline_text)
        ow = _word_count(ocr_text)
        manifest.append({
            "page": i + 1,
            "baseline_method": baseline_method,
            "native_word_count": _word_count(native_text),
            "baseline_word_count": bw,
            "ocr_word_count": ow,
            "baseline_mojibake_score": round(_mojibake_score(baseline_text), 6),
            "ocr_mojibake_score": round(_mojibake_score(ocr_text), 6) if ocr_text else "",
            "baseline_alpha_ratio": round(_alpha_ratio(baseline_text), 4),
            "ocr_alpha_ratio": round(_alpha_ratio(ocr_text), 4) if ocr_text else "",
            "word_retention": round(ow / bw, 4) if bw and ocr_text else "",
            "screening_reasons": "|".join(dict.fromkeys(reasons)),
            "decision": decision,
            "rationale": rationale,
            "text_source": "ocr" if decision == "OCR" else (
                "production_review" if decision == "REVIEW" else baseline_method
            ),
            "native_text_hash": _text_hash(native_text),
            "baseline_text_hash": _text_hash(baseline_text),
            "ocr_text_hash": _text_hash(ocr_text) if ocr_text else "",
            "final_text_hash": _text_hash(final_text),
        })

    return selected, manifest

def simple_quality_manifest(pages: List[Dict]) -> List[Dict]:
    """QA rows for TXT/MD/DOCX sources where PDF OCR comparison is unavailable."""
    out = []
    for i, p in enumerate(pages):
        text = str(p.get("text") or "")
        method = str(p.get("method") or "text")
        out.append({
            "page": p.get("page") or i + 1,
            "baseline_method": method,
            "native_word_count": _word_count(text),
            "baseline_word_count": _word_count(text),
            "ocr_word_count": "",
            "baseline_mojibake_score": round(_mojibake_score(text), 6),
            "ocr_mojibake_score": "",
            "baseline_alpha_ratio": round(_alpha_ratio(text), 4),
            "ocr_alpha_ratio": "",
            "word_retention": "",
            "screening_reasons": "",
            "decision": "NATIVE",
            "rationale": "Non-PDF source; retained supplied text.",
            "text_source": method,
            "native_text_hash": _text_hash(text),
            "baseline_text_hash": _text_hash(text),
            "ocr_text_hash": "",
            "final_text_hash": _text_hash(text),
        })
    return out
