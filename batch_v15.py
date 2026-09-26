#!/usr/bin/env python3
"""V15 batch runner.

V14 remains authoritative for structural section starts/ends.  V15 adds page-level
quality/provenance diagnostics and MDA page/paragraph/sentence research datasets.
"""
from __future__ import annotations

import argparse
import csv
import gc
import json
import re
import shutil
from pathlib import Path

import extractor as core
from semantic_v14 import (
    V14_CACHE_SCHEMA,
    auto_extract_semantic_sections,
    extract_source_cached,
    semantic_manifest_csv,
    semantic_manifest_json,
)
from quality_v15 import V15_QUALITY_SCHEMA, build_quality_pages, simple_quality_manifest
from section_guard_v15 import V15_PACKAGING_SCHEMA, apply_packaging_guard, repair_nested_boundaries
from mda_units_v15 import (
    build_coverage_audit,
    build_mda_page_rows,
    build_paragraph_rows,
    build_review_queue,
    build_sentence_rows,
)

SUPPORTED = {".pdf", ".txt", ".md", ".docx"}


def safe_folder(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(text)).strip("_") or "report"


def section_folder(label, payload):
    if label == "Full Report":
        return "00_Full_Report"
    conf = str(payload.get("semantic_confidence", "HIGH")).upper()
    canonical = payload.get("canonical_category") or label
    if conf == "LOW" or label.startswith("Discovered - "):
        return f"Sections/LOW_Discovered/{safe_folder(payload.get('original_heading') or label)}"
    return f"Sections/{safe_folder(canonical)}"


_EMPTY_CSV_SCHEMAS = {
    "section_packaging_audit.csv": ["section", "start_page", "end_page", "action", "reason"],
    "MDA_page_level.csv": ["report_id", "mda_page_id", "page", "mda_start_page", "mda_end_page", "section_at_page_start", "section_at_page_end", "subsection_headings_on_page", "quality_decision", "text_source", "text"],
    "MDA_paragraph_level.csv": ["paragraph_id", "report_id", "page", "section", "unit_type", "start_line", "end_line", "boundary_reason", "paragraph_word_count", "quality_decision", "text_source", "review_flag", "paragraph_text"],
    "MDA_sentence_level.csv": ["sentence_id", "paragraph_id", "report_id", "page", "section", "quality_decision", "text_source", "sentence_number", "sentence_text"],
    "MDA_review_queue.csv": ["paragraph_id", "report_id", "page", "section", "unit_type", "start_line", "end_line", "boundary_reason", "paragraph_word_count", "quality_decision", "text_source", "review_flag", "paragraph_text"],
    "MDA_coverage_audit.csv": ["report_id", "mda_found", "original_heading", "confidence", "start_page", "end_page", "expected_page_count", "actual_page_count", "continuity_gaps", "quality_review_pages", "status"],
}

def _write_csv(path: Path, rows):
    rows = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        fields = _EMPTY_CSV_SCHEMAS.get(path.name, [])
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            if fields:
                csv.DictWriter(f, fieldnames=fields).writeheader()
        return
    fields = []
    seen = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def process_file(path: Path, out_root: Path, formats, include_low=False, include_supplementary=False, cache_dir=None):
    data = path.read_bytes()

    # ------------------------- FROZEN V14 PATH -------------------------
    # Structural detection runs on the exact production baseline pages.  V15 QA
    # is intentionally NOT allowed to change section boundaries.
    baseline_pages = extract_source_cached(path.name, data, cache_dir=cache_dir)
    boundary_clean_pages = core.clean_pages(baseline_pages)
    sections, semantic_manifest = auto_extract_semantic_sections(
        baseline_pages,
        boundary_clean_pages,
        include_low_structural=include_low,
        include_supplementary=include_supplementary,
    )
    sections, repair_audit = repair_nested_boundaries(sections, baseline_pages, boundary_clean_pages)
    sections, packaging_audit = apply_packaging_guard(sections)
    packaging_audit = repair_audit + packaging_audit

    # ------------------------- V15 QA PATH -----------------------------
    # Separate downstream representation used for text analytics only.
    if path.suffix.lower() == ".pdf":
        analysis_pages, quality_rows = build_quality_pages(data, baseline_pages)
    else:
        analysis_pages = baseline_pages
        quality_rows = simple_quality_manifest(baseline_pages)
    analysis_clean_pages = core.clean_pages(analysis_pages)

    meta = core.infer_metadata(path.name, baseline_pages)
    stem = core.base_stem(path.name)
    report_id = safe_folder(f"{meta['company']}_{meta['year']}_{stem}")
    folder = out_root / report_id
    folder.mkdir(parents=True, exist_ok=True)

    # Normal section outputs intentionally remain the frozen V14 representation.
    full = {
        "text": core.raw_full_text(baseline_pages),
        "original_heading": "Full Report",
        "canonical_category": "Full Report",
        "semantic_confidence": "HIGH",
        "semantic_match_type": "SOURCE_DOCUMENT",
    }
    items = {"Full Report": full, **sections}
    is_pdf = path.suffix.lower() == ".pdf"

    rows = []
    for label, payload in items.items():
        target = folder / section_folder(label, payload)
        target.mkdir(parents=True, exist_ok=True)
        for fmt in formats:
            name, blob = core.format_file(
                stem,
                label,
                payload,
                fmt,
                baseline_pages,
                source_bytes=data,
                source_is_pdf=is_pdf,
                all_sections=sections,
            )
            (target / name).write_bytes(blob)

        rows.append({
            "company": meta["company"],
            "year": meta["year"],
            "source_file": path.name,
            "section": label,
            "original_heading": payload.get("original_heading", label),
            "canonical_category": payload.get("canonical_category", label),
            "confidence": payload.get("semantic_confidence", "HIGH"),
            "match_type": payload.get("semantic_match_type", ""),
            "start_page": payload.get("start_page", 1 if label == "Full Report" else ""),
            "end_page": payload.get("end_page", len(baseline_pages) if label == "Full Report" else ""),
            "printed_start_page": payload.get("printed_start_page", ""),
            "printed_end_page": payload.get("printed_end_page", ""),
            "formats": ", ".join(formats),
        })

    # ------------------------- TRACEABILITY ----------------------------
    metadata_dir = folder / "Metadata"
    metadata_dir.mkdir(exist_ok=True)
    (metadata_dir / "semantic_heading_manifest.csv").write_bytes(semantic_manifest_csv(semantic_manifest))
    (metadata_dir / "semantic_heading_manifest.json").write_bytes(semantic_manifest_json(semantic_manifest))
    _write_csv(metadata_dir / "page_quality_manifest.csv", quality_rows)
    _write_csv(metadata_dir / "section_packaging_audit.csv", packaging_audit)

    quality_summary = {
        "NATIVE": sum(r.get("decision") == "NATIVE" for r in quality_rows),
        "OCR": sum(r.get("decision") == "OCR" for r in quality_rows),
        "REVIEW": sum(r.get("decision") == "REVIEW" for r in quality_rows),
    }
    qa_summary = {
        "page_count": len(baseline_pages),
        "page_quality_decisions": quality_summary,
        "v14_cache_schema": V14_CACHE_SCHEMA,
        "v15_quality_schema": V15_QUALITY_SCHEMA,
        "v15_packaging_schema": V15_PACKAGING_SCHEMA,
        "boundary_engine": "V14 frozen structural engine",
        "quality_layer_changes_boundaries": False,
    }
    (metadata_dir / "QA_summary.json").write_text(
        json.dumps(qa_summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (metadata_dir / "report_metadata.json").write_text(
        json.dumps({
            **meta,
            "page_count": len(baseline_pages),
            "v14_cache_schema": V14_CACHE_SCHEMA,
            "v15_quality_schema": V15_QUALITY_SCHEMA,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # ------------------------- MDA RESEARCH DATA -----------------------
    quality_by_page = {
        int(r["page"]): r for r in quality_rows
        if str(r.get("page", "")).isdigit()
    }
    page_rows, mda_payload = build_mda_page_rows(
        report_id,
        analysis_clean_pages,
        sections,
        quality_by_page,
    )
    paragraph_rows = build_paragraph_rows(page_rows)
    sentence_rows = build_sentence_rows(paragraph_rows)
    review_rows = build_review_queue(paragraph_rows)
    coverage_rows = build_coverage_audit(report_id, page_rows, mda_payload)

    mda_dir = folder / "MDA_Research_Data"
    _write_csv(mda_dir / "MDA_page_level.csv", page_rows)
    _write_csv(mda_dir / "MDA_paragraph_level.csv", paragraph_rows)
    _write_csv(mda_dir / "MDA_sentence_level.csv", sentence_rows)
    _write_csv(mda_dir / "MDA_review_queue.csv", review_rows)
    _write_csv(mda_dir / "MDA_coverage_audit.csv", coverage_rows)

    return rows


def main():
    ap = argparse.ArgumentParser(description="V15 annual-report batch extractor")
    ap.add_argument("--input", required=True, help="Folder containing annual reports")
    ap.add_argument("--output", default="v15_output", help="Output folder")
    ap.add_argument("--formats", nargs="+", default=["TXT", "JSON"], choices=list(core.FORMAT_EXT))
    ap.add_argument("--include-low", action="store_true")
    ap.add_argument("--include-supplementary", action="store_true")
    ap.add_argument("--cache-dir", default=".v14_cache")
    ap.add_argument("--pdf-only", action="store_true", help="Process PDF files only")
    ap.add_argument("--limit", type=int, default=0, help="Maximum number of reports; 0 = all")
    ap.add_argument("--zip", action="store_true")
    args = ap.parse_args()

    src = Path(args.input)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    allowed = {".pdf"} if args.pdf_only else SUPPORTED
    files = sorted(p for p in src.rglob("*") if p.is_file() and p.suffix.lower() in allowed)
    if args.limit > 0:
        files = files[:args.limit]
    if not files:
        raise SystemExit("No supported reports found.")

    all_rows = []
    failed = 0
    for i, path in enumerate(files, 1):
        print(f"[{i}/{len(files)}] {path}", flush=True)
        try:
            all_rows.extend(process_file(
                path,
                out,
                args.formats,
                include_low=args.include_low,
                include_supplementary=args.include_supplementary,
                cache_dir=args.cache_dir,
            ))
        except Exception as exc:
            failed += 1
            print(f"  ERROR: {type(exc).__name__}: {exc}", flush=True)
        gc.collect()

    fields = [
        "company", "year", "source_file", "section", "original_heading",
        "canonical_category", "confidence", "match_type", "start_page", "end_page",
        "printed_start_page", "printed_end_page", "formats",
    ]
    with (out / "research_manifest.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(all_rows)

    if args.zip:
        shutil.make_archive(str(out), "zip", root_dir=out)
        print(f"ZIP: {out}.zip")
    print(f"Done: {len(files) - failed}/{len(files)} reports → {out}; failed={failed}")


if __name__ == "__main__":
    main()
