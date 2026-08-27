
import csv
import gc
import io
import json
import os
import re
import tempfile
import zipfile

import streamlit as st
from extractor import *
from semantic_v14 import (
    SEMANTIC_NORMALIZER,
    V14_CACHE_SCHEMA,
    auto_extract_semantic_sections,
    extract_source_cached,
    semantic_manifest_csv,
    semantic_manifest_json,
)

st.set_page_config(page_title="Annual Report Research Extractor", page_icon="📄", layout="wide")
st.title("Annual Report Research Extractor")
st.caption("V14 • automatic semantic section normalization • batch extraction • research-ready downloads")


def detect_sections(raw_pages, research_pages, leadership, mda, sustainability, custom_heads, include_low_structural=False, include_supplementary=False):
    """V14 zero-input detection with legacy fallbacks and optional manual overrides."""
    sections, semantic_manifest = auto_extract_semantic_sections(
        raw_pages,
        research_pages,
        include_low_structural=include_low_structural,
        include_supplementary=include_supplementary,
    )

    # Preserve the legacy guaranteed categories as fallbacks. In V14 these are
    # normally already found by the semantic layer, so no manual heading entry is needed.
    if leadership:
        for label in ["Chairman Message", "CEO Message", "Managing Director Message"]:
            if label not in sections:
                sec = extract_preset(raw_pages, research_pages, label)
                if sec:
                    sec.update({
                        "original_heading": label,
                        "canonical_category": label,
                        "semantic_confidence": "HIGH",
                        "semantic_match_type": "LEGACY_PRESET_FALLBACK",
                    })
                    sections[label] = sec

        combined = combine_sections(
            raw_pages,
            research_pages,
            sections,
            ["Chairman Message", "CEO Message", "Managing Director Message"],
            "Leadership Messages (Combined)",
        )
        if combined:
            combined.update({
                "original_heading": "Leadership Messages (Combined)",
                "canonical_category": "Leadership Messages (Combined)",
                "semantic_confidence": "HIGH",
                "semantic_match_type": "COMBINED_OUTPUT",
            })
            sections = {"Leadership Messages (Combined)": combined, **sections}

    if mda and "Management Discussion & Analysis" not in sections:
        sec = extract_preset(raw_pages, research_pages, "Management Discussion & Analysis")
        if sec:
            sec.update({
                "original_heading": "Management Discussion & Analysis",
                "canonical_category": "Management Discussion & Analysis",
                "semantic_confidence": "HIGH",
                "semantic_match_type": "LEGACY_PRESET_FALLBACK",
            })
            sections["Management Discussion & Analysis"] = sec

    sustainability_labels = [
        "Business Responsibility & Sustainability Report (BRSR)",
        "Business Responsibility Report (BRR)",
        "ESG Report",
        "Sustainability Report",
    ]
    if sustainability and not any(x in sections for x in sustainability_labels):
        for label in sustainability_labels:
            sec = extract_preset(raw_pages, research_pages, label)
            if sec:
                sec.update({
                    "original_heading": label,
                    "canonical_category": label,
                    "semantic_confidence": "HIGH",
                    "semantic_match_type": "LEGACY_PRESET_FALLBACK",
                })
                sections[label] = sec
                break

    # Advanced manual override remains available for unusual researcher-specific
    # sections, but it is no longer required for ordinary variant wordings.
    for heading in custom_heads:
        norm = SEMANTIC_NORMALIZER.normalize_heading(heading)
        target = norm["canonical_category"] if norm["confidence"] != "LOW" else heading
        if target in sections:
            continue
        sec = extract_custom(raw_pages, research_pages, heading, custom_heads)
        if sec:
            sec.update({
                "original_heading": heading,
                "canonical_category": target,
                "semantic_confidence": "HIGH",
                "semantic_match_type": "MANUAL_OVERRIDE",
            })
            sections[target] = sec
            semantic_manifest.append({
                "original_heading": heading,
                "canonical_category": target,
                "confidence": "HIGH",
                "match_type": "MANUAL_OVERRIDE",
                "pdf_page": sec.get("start_page"),
                "printed_page": sec.get("printed_start_page"),
                "score": "",
                "source": "manual-override",
                "hard_boundary": True,
                "generic": False,
                "selected_for_extraction": True,
            })

    return sections, semantic_manifest


def _archive_section_folder(label, payload):
    if label == "Full Report":
        return "00_Full_Report"
    conf = str(payload.get("semantic_confidence", "HIGH")).upper()
    canonical = payload.get("canonical_category") or label
    if conf == "LOW" or label.startswith("Discovered - "):
        return f"Sections/LOW_Discovered/{safe_folder(payload.get('original_heading') or label)}"
    return f"Sections/{safe_folder(canonical)}"


def process_one(upload, formats, leadership, mda, sustainability, custom_heads, include_clean_pdf=False, include_low_structural=False, include_supplementary=False):
    raw = upload.getvalue()
    raw_pages = extract_source_cached(upload.name, raw)
    research_pages = clean_pages(raw_pages)
    sections, semantic_manifest = detect_sections(
        raw_pages,
        research_pages,
        leadership,
        mda,
        sustainability,
        custom_heads,
        include_low_structural=include_low_structural,
        include_supplementary=include_supplementary,
    )

    stem = base_stem(upload.name)
    meta = infer_metadata(upload.name, raw_pages)
    full = {
        "text": raw_full_text(raw_pages),
        "original_heading": "Full Report",
        "canonical_category": "Full Report",
        "semantic_confidence": "HIGH",
        "semantic_match_type": "SOURCE_DOCUMENT",
    }
    items = {"Full Report": full, **sections}
    source_is_pdf = upload.name.lower().endswith(".pdf")

    generated = {}
    archive_files = {}
    archive_lookup = {}
    for label, payload in items.items():
        archive_lookup[label] = {}
        section_folder = _archive_section_folder(label, payload)
        for fmt in formats:
            name, data = format_file(
                stem,
                label,
                payload,
                fmt,
                raw_pages,
                source_bytes=raw,
                source_is_pdf=source_is_pdf,
                all_sections=sections,
            )
            generated[name] = data
            archive_path = f"{section_folder}/{name}"
            archive_files[archive_path] = data
            archive_lookup[label][fmt] = archive_path

    # Traceability metadata is always included in ZIPs, independent of chosen text formats.
    sem_csv = semantic_manifest_csv(semantic_manifest)
    sem_json = semantic_manifest_json(semantic_manifest)
    report_meta = {
        **meta,
        "source_file": upload.name,
        "page_count": len(raw_pages),
        "v14_cache_schema": V14_CACHE_SCHEMA,
        "sections_extracted": len(sections),
    }
    archive_files["Metadata/semantic_heading_manifest.csv"] = sem_csv
    archive_files["Metadata/semantic_heading_manifest.json"] = sem_json
    archive_files["Metadata/report_metadata.json"] = json.dumps(
        report_meta, ensure_ascii=False, indent=2
    ).encode("utf-8")

    if include_clean_pdf and source_is_pdf:
        clean_name = f"{stem}_Background_Clean.pdf"
        clean_data = clean_background_pdf(raw, 215)
        generated[clean_name] = clean_data
        archive_files[f"PDF_Cleanup/{clean_name}"] = clean_data

    page_count = len(raw_pages)
    # Keep only page text for interactive search.  Full layout dictionaries can be
    # very large and were causing unnecessary Streamlit session-memory pressure.
    search_pages_light = [
        {"page": p.get("page"), "text": p.get("text", "")}
        for p in research_pages
    ]

    return {
        "name": upload.name,
        "stem": stem,
        "meta": meta,
        "page_count": page_count,
        "search_pages": search_pages_light,
        "sections": sections,
        "semantic_manifest": semantic_manifest,
        "semantic_manifest_csv": sem_csv,
        "semantic_manifest_json": sem_json,
        "items": items,
        "files": generated,
        "archive_files": archive_files,
        "archive_lookup": archive_lookup,
        "formats": formats,
        "raw": raw if source_is_pdf else None,
        "is_pdf": source_is_pdf,
    }

def manifest_bytes(rows):
    s = io.StringIO()
    fields = [
        "company", "year", "source_file", "section", "original_heading",
        "canonical_category", "confidence", "match_type", "start_page", "end_page",
        "printed_start_page", "printed_end_page", "formats"
    ]
    writer = csv.DictWriter(s, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in fields})
    return s.getvalue().encode("utf-8-sig")

def results_manifest_bytes(results):
    rows = []
    for r in results:
        rows.append({
            "company": r["meta"]["company"],
            "year": r["meta"]["year"],
            "source_file": r["name"],
            "section": "Full Report",
            "original_heading": "",
            "canonical_category": "Full Report",
            "confidence": "HIGH",
            "match_type": "SOURCE_DOCUMENT",
            "start_page": 1 if r["page_count"] > 1 else "",
            "end_page": r["page_count"] if r["page_count"] > 1 else "",
            "printed_start_page": "",
            "printed_end_page": "",
            "formats": ", ".join(r["formats"]),
        })
        for label, sec in r["sections"].items():
            rows.append({
                "company": r["meta"]["company"],
                "year": r["meta"]["year"],
                "source_file": r["name"],
                "section": label,
                "original_heading": sec.get("original_heading", label),
                "canonical_category": sec.get("canonical_category", label),
                "confidence": sec.get("semantic_confidence", sec.get("detection_confidence", "")),
                "match_type": sec.get("semantic_match_type", ""),
                "start_page": sec.get("start_page", ""),
                "end_page": sec.get("end_page", ""),
                "printed_start_page": sec.get("printed_start_page", ""),
                "printed_end_page": sec.get("printed_end_page", ""),
                "formats": ", ".join(r["formats"]),
            })
    return manifest_bytes(rows)


def safe_folder(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "report"


def payload_location(payload, full_count=None):
    if full_count is not None:
        return f"{full_count} pages" if full_count > 1 else "Complete document"

    printed_ranges = payload.get("printed_page_ranges") or []
    if printed_ranges:
        parts = []
        for r in printed_ranges:
            s, e = r.get("start_page"), r.get("end_page")
            parts.append(f"{s}-{e}" if s != e else f"{s}")
        return "Report pages " + " + ".join(parts)

    ps, pe = payload.get("printed_start_page"), payload.get("printed_end_page")
    s, e = payload.get("start_page"), payload.get("end_page")
    if ps is not None:
        report = f"Report pages {ps}-{pe}" if pe is not None and pe != ps else f"Report page {ps}"
        if s is not None:
            pdf = f"PDF pages {s}-{e}" if e is not None and e != s else f"PDF page {s}"
            return f"{report} • {pdf}"
        return report

    page_ranges = payload.get("page_ranges") or []
    if page_ranges:
        parts = []
        for r in page_ranges:
            a, b = r.get("start_page"), r.get("end_page")
            parts.append(f"{a}-{b}" if a != b else f"{a}")
        return "PDF pages " + " + ".join(parts)

    if s is not None:
        return f"PDF pages {s}-{e}" if e is not None and e != s else f"PDF page {s}"
    return "Detected"


mode = st.radio(
    "Processing mode",
    ["Research Library", "Bulk Processing"],
    horizontal=True,
    help="Use Bulk Processing for large batches. It writes each report directly into one ZIP instead of keeping every extracted document in memory.",
)

uploads = st.file_uploader(
    "Upload reports",
    type=["pdf", "txt", "docx", "md"],
    accept_multiple_files=True,
    help="Select many files together. PDF, TXT, DOCX and Markdown are supported.",
)

formats = st.multiselect(
    "Output formats",
    ["TXT", "JSON", "PDF", "DOCX", "CSV", "MD"],
    default=["TXT", "JSON"],
)

st.subheader("Automatic Section Detection")

with st.expander("Advanced section controls (normally no input needed)"):
    a, b, c = st.columns(3)
    p1 = a.checkbox("Ensure Leadership Messages", True)
    p2 = b.checkbox("Ensure MDA", True)
    p3 = c.checkbox("Ensure Sustainability / Responsibility", True)
    include_supplementary = st.checkbox(
        "Package supplementary recognized headings",
        False,
        help="Optional: Board of Directors, Awards, Notice, financial statements and other non-core recognized headings. All remain visible in the semantic map even when not packaged.",
    )
    include_low_structural = st.checkbox(
        "Package LOW-confidence hard/TOC-backed discovered headings",
        False,
        help=(
            "Off by default for clean research output. LOW candidates remain in the semantic map for traceability."
        ),
    )
    custom = st.text_area(
        "Additional section headings (optional override)",
        placeholder="Only use for a researcher-specific heading not covered automatically",
        height=70,
    )
custom_heads = [x.strip() for x in custom.splitlines() if x.strip()]

bulk_clean = False
if mode == "Bulk Processing":
    with st.expander("Bulk options"):
        bulk_clean = st.checkbox(
            "Include background / watermark-cleaned PDF copies",
            False,
            help="Slower and much larger. Leave off unless you specifically need cleaned PDFs.",
        )
    st.info(
        "Bulk mode processes reports one by one and creates one structured ZIP with a folder for each company/year plus a master research_manifest.csv."
    )
    st.caption(
        "For a library of thousands of massive PDFs, use batch_v14.py on the local/server report folder instead of uploading all 5,000 files through one browser session."
    )

if uploads:
    x, y = st.columns(2)
    x.metric("Files selected", len(uploads))
    y.metric("Total size", f"{sum(f.size for f in uploads)/(1024**2):.1f} MB")


# -------------------- BULK MODE --------------------
if mode == "Bulk Processing":
    if "bulk_zip_path" not in st.session_state:
        st.session_state.bulk_zip_path = None
    if "bulk_manifest" not in st.session_state:
        st.session_state.bulk_manifest = None
    if "bulk_summary" not in st.session_state:
        st.session_state.bulk_summary = []

    if st.button(
        "Process all reports in bulk",
        type="primary",
        disabled=not uploads or not formats,
        use_container_width=True,
    ):
        old = st.session_state.bulk_zip_path
        if old and os.path.exists(old):
            try:
                os.remove(old)
            except OSError:
                pass

        fd, zip_path = tempfile.mkstemp(prefix="annual_report_bulk_", suffix=".zip")
        os.close(fd)

        manifest_rows = []
        summary = []
        used_folders = set()

        bar = st.progress(0)
        status = st.empty()

        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zout:
                for i, upload in enumerate(uploads):
                    status.write(f"Processing {i+1}/{len(uploads)} — {upload.name}")

                    result = process_one(
                        upload,
                        formats,
                        p1,
                        p2,
                        p3,
                        custom_heads,
                        include_clean_pdf=bulk_clean,
                        include_low_structural=include_low_structural,
                        include_supplementary=include_supplementary,
                    )

                    meta = result["meta"]
                    folder_base = safe_folder(f"{meta['company']}_{meta['year']}")
                    folder = folder_base
                    n = 2
                    while folder in used_folders:
                        folder = f"{folder_base}_{n}"
                        n += 1
                    used_folders.add(folder)

                    for filename, data in result["archive_files"].items():
                        zout.writestr(f"{folder}/{filename}", data)

                    # Full report manifest row
                    manifest_rows.append({
                        "company": meta["company"],
                        "year": meta["year"],
                        "source_file": result["name"],
                        "section": "Full Report",
                        "original_heading": "",
                        "canonical_category": "Full Report",
                        "confidence": "HIGH",
                        "match_type": "SOURCE_DOCUMENT",
                        "start_page": 1 if result["page_count"] > 1 else "",
                        "end_page": result["page_count"] if result["page_count"] > 1 else "",
                        "printed_start_page": "",
                        "printed_end_page": "",
                        "formats": ", ".join(formats),
                    })

                    for label, sec in result["sections"].items():
                        manifest_rows.append({
                            "company": meta["company"],
                            "year": meta["year"],
                            "source_file": result["name"],
                            "section": label,
                            "original_heading": sec.get("original_heading", label),
                            "canonical_category": sec.get("canonical_category", label),
                            "confidence": sec.get("semantic_confidence", sec.get("detection_confidence", "")),
                            "match_type": sec.get("semantic_match_type", ""),
                            "start_page": sec.get("start_page", ""),
                            "end_page": sec.get("end_page", ""),
                            "printed_start_page": sec.get("printed_start_page", ""),
                            "printed_end_page": sec.get("printed_end_page", ""),
                            "formats": ", ".join(formats),
                        })

                    item_index = {}
                    for label, payload in result["items"].items():
                        if label == "Full Report":
                            location = payload_location(payload, full_count=result["page_count"])
                        else:
                            location = payload_location(payload)

                        fmt_files = {}
                        for fmt in formats:
                            rel = result["archive_lookup"].get(label, {}).get(fmt)
                            if rel:
                                fmt_files[fmt] = f"{folder}/{rel}"

                        item_index[label] = {
                            "location": location,
                            "files": fmt_files,
                            "original_heading": payload.get("original_heading", label),
                            "canonical_category": payload.get("canonical_category", label),
                            "confidence": payload.get("semantic_confidence", "HIGH"),
                        }

                    summary.append({
                        "Company": meta["company"],
                        "Year": meta["year"],
                        "Source file": result["name"],
                        "Pages": result["page_count"] if result["page_count"] > 1 else "",
                        "Sections found": len(result["sections"]),
                        "_folder": folder,
                        "_items": item_index,
                    })

                    # Release heavy per-report objects before the next file.
                    del result
                    gc.collect()
                    bar.progress((i + 1) / len(uploads))

                manifest = manifest_bytes(manifest_rows)
                zout.writestr("research_manifest.csv", manifest)

            st.session_state.bulk_zip_path = zip_path
            st.session_state.bulk_manifest = manifest
            st.session_state.bulk_summary = summary
            status.success(f"Processed {len(uploads)} report(s).")

        except Exception:
            if os.path.exists(zip_path):
                os.remove(zip_path)
            raise

    if st.session_state.bulk_zip_path and os.path.exists(st.session_state.bulk_zip_path):
        st.divider()
        st.subheader("Bulk output")

        c1, c2 = st.columns(2)
        bulk_file = open(st.session_state.bulk_zip_path, "rb")
        c1.download_button(
            "Download complete bulk research library (.zip)",
            bulk_file,
            "annual_report_bulk_research_library.zip",
            "application/zip",
            use_container_width=True,
        )
        c2.download_button(
            "Download research index (.csv)",
            st.session_state.bulk_manifest,
            "research_manifest.csv",
            "text/csv",
            use_container_width=True,
        )

        q = st.text_input(
            "Find processed report",
            placeholder="Company, year or filename",
            key="bulk_search",
        ).lower().strip()

        rows = st.session_state.bulk_summary
        if q:
            rows = [
                r for r in rows
                if q in f"{r['Company']} {r['Year']} {r['Source file']}".lower()
            ]

        if rows:
            table_rows = [
                {k: v for k, v in r.items() if not k.startswith("_")}
                for r in rows
            ]
            st.dataframe(table_rows, use_container_width=True, hide_index=True)

            st.subheader("Browse extracted files")
            st.caption("Open any report below, choose a section, then download it in the required format.")

            for idx, r in enumerate(rows):
                title = f"{r['Company']} | {r['Year']} | {r['Source file']}"
                with st.expander(title):
                    labels = list(r["_items"].keys())
                    if not labels:
                        st.info("No extracted outputs available.")
                        continue

                    selected = st.selectbox(
                        "Section",
                        labels,
                        key=f"bulk_section_{idx}_{r['_folder']}",
                    )

                    item = r["_items"][selected]
                    st.caption(item["location"])
                    if selected != "Full Report":
                        st.caption(
                            f"Original: {item.get('original_heading', selected)}  •  "
                            f"Canonical: {item.get('canonical_category', selected)}  •  "
                            f"Confidence: {item.get('confidence', 'HIGH')}"
                        )

                    available = item["files"]
                    if not available:
                        st.info("No downloadable format generated for this section.")
                        continue

                    cols = st.columns(max(1, len(available)))
                    with zipfile.ZipFile(st.session_state.bulk_zip_path, "r") as zin:
                        for j, (fmt, archive_name) in enumerate(available.items()):
                            data = zin.read(archive_name)
                            download_name = archive_name.split("/", 1)[-1]
                            cols[j].download_button(
                                fmt,
                                data,
                                download_name,
                                key=f"bulk_dl_{idx}_{selected}_{fmt}_{r['_folder']}",
                                use_container_width=True,
                            )


# -------------------- RESEARCH LIBRARY MODE --------------------
else:
    if "results" not in st.session_state:
        st.session_state.results = []

    if st.button(
        "Process",
        type="primary",
        disabled=not uploads or not formats,
        use_container_width=True,
    ):
        st.session_state.results = []
        bar = st.progress(0)
        status = st.empty()

        for i, upload in enumerate(uploads):
            status.write(f"Processing {i+1}/{len(uploads)} — {upload.name}")
            result = process_one(
                upload, formats, p1, p2, p3, custom_heads, include_clean_pdf=False,
                include_low_structural=include_low_structural,
                include_supplementary=include_supplementary
            )
            st.session_state.results.append(result)
            bar.progress((i + 1) / len(uploads))

        status.success(f"Processed {len(uploads)} report(s)")

    results = st.session_state.results

    if results:
        st.divider()
        st.subheader("Research Library")

        q = st.text_input(
            "Find report",
            placeholder="Company, filename or year (e.g. Company, 2025, 2024-25)",
        )
        valid_years = sorted({
            r["meta"]["year"]
            for r in results
            if r["meta"]["year"] != "Year not detected"
        })
        year = st.selectbox("Year", ["All"] + valid_years)

        filtered = []
        qn = q.lower().strip()
        for r in results:
            searchable = f"{r['meta']['company']} {r['meta']['year']} {r['name']}".lower()
            if (not qn or qn in searchable) and (year == "All" or r["meta"]["year"] == year):
                filtered.append(r)

        allfiles = {}
        for r in results:
            folder = safe_folder(f"{r['meta']['company']}_{r['meta']['year']}")
            for n, d in r["archive_files"].items():
                allfiles[f"{folder}/{n}"] = d
        allfiles["research_manifest.csv"] = results_manifest_bytes(results)

        d1, d2 = st.columns(2)
        d1.download_button(
            "Download complete research library (.zip)",
            make_zip(allfiles),
            "annual_report_research_library.zip",
            "application/zip",
            use_container_width=True,
        )
        d2.download_button(
            "Download research index (.csv)",
            results_manifest_bytes(results),
            "research_manifest.csv",
            "text/csv",
            use_container_width=True,
        )

        st.caption(f"Showing {len(filtered)} of {len(results)} report(s)")

        for r in filtered:
            title = f"{r['meta']['company']}  |  {r['meta']['year']}"
            with st.container(border=True):
                h1, h2, h3 = st.columns([4, 1, 1])
                h1.subheader(title)
                h2.metric("Sections", len(r["sections"]))
                h3.metric("Pages", r["page_count"] if r["page_count"] > 1 else "-")
                st.caption(r["name"])

                st.download_button(
                    "Download this report - all extracted files",
                    make_zip(r["archive_files"]),
                    f"{r['stem']}_outputs.zip",
                    "application/zip",
                    key="reportzip_" + r["name"],
                    use_container_width=True,
                )

                st.caption(
                    "Extracted sections are subsets of the full report, so their page counts are not expected to add up to the total report pages."
                )

                for label, payload in r["items"].items():
                    cols = st.columns([3, 1.4] + [1] * len(r["formats"]))
                    cols[0].markdown(f"**{label}**")

                    if label == "Full Report":
                        loc = payload_location(payload, full_count=r["page_count"])
                    else:
                        loc = payload_location(payload)
                    cols[1].caption(loc)
                    if label != "Full Report":
                        conf = payload.get("semantic_confidence", "HIGH")
                        original = payload.get("original_heading", label)
                        canonical = payload.get("canonical_category", label)
                        cols[0].caption(f"Original: {original} • Canonical: {canonical} • {conf}")

                    safe = safe_label(label)
                    for j, fmt in enumerate(r["formats"], start=2):
                        fn = f"{r['stem']}_{safe}.{FORMAT_EXT[fmt]}"
                        if fn in r["files"]:
                            cols[j].download_button(
                                fmt,
                                r["files"][fn],
                                fn,
                                key=f"{r['name']}_{label}_{fmt}",
                            )

                with st.expander("Advanced diagnostics / Semantic map", expanded=False):
                    st.caption(
                        "Semantic heading diagnostics are kept for traceability but hidden from the normal researcher view."
                    )
                    dsem1, dsem2 = st.columns(2)
                    dsem1.download_button(
                        "Semantic map CSV",
                        r["semantic_manifest_csv"],
                        f"{r['stem']}_semantic_heading_manifest.csv",
                        "text/csv",
                        key="semcsv_" + r["name"],
                        use_container_width=True,
                    )
                    dsem2.download_button(
                        "Semantic map JSON",
                        r["semantic_manifest_json"],
                        f"{r['stem']}_semantic_heading_manifest.json",
                        "application/json",
                        key="semjson_" + r["name"],
                        use_container_width=True,
                    )

                with st.expander("Search inside this report"):
                    sq = st.text_input("Search term", key="search_" + r["name"])
                    if sq:
                        hits = search_pages(r["search_pages"], sq)
                        if not hits:
                            st.info("No matches found.")
                        for hit in hits:
                            loc = f"Page {hit['page']}" if hit["page"] else "Document"
                            st.write(f"**{loc}:** {hit['snippet']}")

                if r["is_pdf"]:
                    with st.expander("PDF background / watermark cleanup"):
                        strength = st.slider(
                            "Cleanup strength",
                            190,
                            235,
                            215,
                            key="strength_" + r["name"],
                        )
                        st.caption(
                            "Creates a separate cleaned copy. The original PDF is never changed."
                        )
                        clean_key = "clean_pdf_" + r["name"]

                        if st.button("Create cleaned PDF", key="make_" + r["name"]):
                            with st.spinner("Cleaning PDF..."):
                                st.session_state[clean_key] = clean_background_pdf(
                                    r["raw"], strength
                                )

                        if clean_key in st.session_state:
                            st.download_button(
                                "Download cleaned PDF",
                                st.session_state[clean_key],
                                f"{r['stem']}_Background_Clean.pdf",
                                "application/pdf",
                                key="download_clean_" + r["name"],
                            )
