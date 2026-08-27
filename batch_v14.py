#!/usr/bin/env python3
"""Offline/folder batch runner for V14.

Designed for large libraries where uploading thousands of reports through a browser
is not practical. Processes one file at a time and writes results directly to disk.
"""

import argparse
import csv
import gc
import json
import os
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


def process_file(path: Path, out_root: Path, formats, include_low=False, include_supplementary=False, cache_dir=None):
    data = path.read_bytes()
    raw_pages = extract_source_cached(path.name, data, cache_dir=cache_dir)
    clean = core.clean_pages(raw_pages)
    sections, sem = auto_extract_semantic_sections(raw_pages, clean, include_low_structural=include_low, include_supplementary=include_supplementary)
    meta = core.infer_metadata(path.name, raw_pages)
    stem = core.base_stem(path.name)
    folder = out_root / safe_folder(f"{meta['company']}_{meta['year']}_{stem}")
    folder.mkdir(parents=True, exist_ok=True)

    full = {
        "text": core.raw_full_text(raw_pages),
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
                raw_pages,
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
            "end_page": payload.get("end_page", len(raw_pages) if label == "Full Report" else ""),
            "printed_start_page": payload.get("printed_start_page", ""),
            "printed_end_page": payload.get("printed_end_page", ""),
            "formats": ", ".join(formats),
        })

    metadata_dir = folder / "Metadata"
    metadata_dir.mkdir(exist_ok=True)
    (metadata_dir / "semantic_heading_manifest.csv").write_bytes(semantic_manifest_csv(sem))
    (metadata_dir / "semantic_heading_manifest.json").write_bytes(semantic_manifest_json(sem))
    (metadata_dir / "report_metadata.json").write_text(
        json.dumps({**meta, "page_count": len(raw_pages), "v14_cache_schema": V14_CACHE_SCHEMA}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="Folder containing annual reports")
    ap.add_argument("--output", default="v14_output", help="Output folder")
    ap.add_argument("--formats", nargs="+", default=["TXT", "JSON"], choices=list(core.FORMAT_EXT))
    ap.add_argument("--include-low", action="store_true", help="Also extract LOW-confidence hard/TOC-backed headings (off by default)")
    ap.add_argument("--include-supplementary", action="store_true", help="Also package supplementary recognized headings (off by default)")
    ap.add_argument("--cache-dir", default=".v14_cache")
    ap.add_argument("--zip", action="store_true", help="Create a ZIP after processing finishes")
    args = ap.parse_args()

    src = Path(args.input)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in src.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED)
    if not files:
        raise SystemExit("No supported reports found.")

    all_rows = []
    for i, path in enumerate(files, 1):
        print(f"[{i}/{len(files)}] {path}", flush=True)
        try:
            all_rows.extend(process_file(path, out, args.formats, include_low=args.include_low, include_supplementary=args.include_supplementary, cache_dir=args.cache_dir))
        except Exception as e:
            print(f"  ERROR: {e}", flush=True)
        gc.collect()

    fields = [
        "company", "year", "source_file", "section", "original_heading",
        "canonical_category", "confidence", "match_type", "start_page", "end_page",
        "printed_start_page", "printed_end_page", "formats",
    ]
    with (out / "research_manifest.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(all_rows)

    if args.zip:
        shutil.make_archive(str(out), "zip", root_dir=out)
        print(f"ZIP: {out}.zip")
    print(f"Done: {len(files)} reports → {out}")


if __name__ == "__main__":
    main()
