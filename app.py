
import csv
import gc
import io
import os
import re
import tempfile
import zipfile

import streamlit as st
from extractor import *

st.set_page_config(page_title="Annual Report Research Extractor", page_icon="📄", layout="wide")
st.title("Annual Report Research Extractor")
st.caption("Batch extraction and structured research-ready downloads")


def detect_sections(raw_pages, research_pages, leadership, mda, sustainability, custom_heads):
    sections = {}

    if leadership:
        for label in ["Chairman Message", "CEO Message", "Managing Director Message"]:
            sec = extract_preset(raw_pages, research_pages, label)
            if sec:
                sections[label] = sec

        combined = combine_sections(
            raw_pages,
            research_pages,
            sections,
            ["Chairman Message", "CEO Message", "Managing Director Message"],
            "Leadership Messages (Combined)",
        )
        if combined:
            sections = {"Leadership Messages (Combined)": combined, **sections}

    if mda:
        sec = extract_preset(raw_pages, research_pages, "Management Discussion & Analysis")
        if sec:
            sections["Management Discussion & Analysis"] = sec

    if sustainability:
        for label in [
            "Business Responsibility & Sustainability Report (BRSR)",
            "Business Responsibility Report (BRR)",
            "ESG Report",
            "Sustainability Report",
        ]:
            sec = extract_preset(raw_pages, research_pages, label)
            if sec:
                sections[label] = sec
                break

    for heading in custom_heads:
        sec = extract_custom(raw_pages, research_pages, heading)
        if sec:
            sections[heading] = sec

    return sections


def process_one(upload, formats, leadership, mda, sustainability, custom_heads, include_clean_pdf=False):
    raw = upload.getvalue()
    raw_pages = extract_source(upload.name, raw)
    research_pages = clean_pages(raw_pages)
    sections = detect_sections(
        raw_pages, research_pages, leadership, mda, sustainability, custom_heads
    )

    stem = base_stem(upload.name)
    meta = infer_metadata(upload.name, raw_pages)
    full = {"text": raw_full_text(raw_pages)}
    items = {"Full Report": full, **sections}
    source_is_pdf = upload.name.lower().endswith(".pdf")

    generated = {}
    for label, payload in items.items():
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

    if include_clean_pdf and source_is_pdf:
        generated[f"{stem}_Background_Clean.pdf"] = clean_background_pdf(raw, 215)

    return {
        "name": upload.name,
        "stem": stem,
        "meta": meta,
        "raw_pages": raw_pages,
        "search_pages": research_pages,
        "sections": sections,
        "items": items,
        "files": generated,
        "formats": formats,
        "raw": raw if source_is_pdf else None,
        "is_pdf": source_is_pdf,
    }


def manifest_bytes(rows):
    s = io.StringIO()
    fields = [
        "company", "year", "source_file", "section",
        "start_page", "end_page", "formats"
    ]
    writer = csv.DictWriter(s, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return s.getvalue().encode("utf-8-sig")


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

st.subheader("Sections")
a, b, c = st.columns(3)
p1 = a.checkbox("Leadership Messages (Chairman / CEO / MD)", True)
p2 = b.checkbox("Management Discussion & Analysis", True)
p3 = c.checkbox("Sustainability / Responsibility Report", True)

custom = st.text_area(
    "Additional section headings (optional)",
    placeholder="Risk Management\nHuman Resources\nCorporate Governance",
    height=80,
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
                    )

                    meta = result["meta"]
                    folder_base = safe_folder(f"{meta['company']}_{meta['year']}")
                    folder = folder_base
                    n = 2
                    while folder in used_folders:
                        folder = f"{folder_base}_{n}"
                        n += 1
                    used_folders.add(folder)

                    for filename, data in result["files"].items():
                        zout.writestr(f"{folder}/{filename}", data)

                    # Full report manifest row
                    manifest_rows.append({
                        "company": meta["company"],
                        "year": meta["year"],
                        "source_file": result["name"],
                        "section": "Full Report",
                        "start_page": 1 if len(result["raw_pages"]) > 1 else "",
                        "end_page": len(result["raw_pages"]) if len(result["raw_pages"]) > 1 else "",
                        "formats": ", ".join(formats),
                    })

                    for label, sec in result["sections"].items():
                        manifest_rows.append({
                            "company": meta["company"],
                            "year": meta["year"],
                            "source_file": result["name"],
                            "section": label,
                            "start_page": sec.get("start_page", ""),
                            "end_page": sec.get("end_page", ""),
                            "formats": ", ".join(formats),
                        })

                    item_index = {}
                    for label, payload in result["items"].items():
                        if label == "Full Report":
                            location = payload_location(payload, full_count=len(result["raw_pages"]))
                        else:
                            location = payload_location(payload)

                        safe = safe_label(label)
                        fmt_files = {}
                        for fmt in formats:
                            fn = f"{result['stem']}_{safe}.{FORMAT_EXT[fmt]}"
                            if fn in result["files"]:
                                fmt_files[fmt] = f"{folder}/{fn}"

                        item_index[label] = {
                            "location": location,
                            "files": fmt_files,
                        }

                    summary.append({
                        "Company": meta["company"],
                        "Year": meta["year"],
                        "Source file": result["name"],
                        "Pages": len(result["raw_pages"]) if len(result["raw_pages"]) > 1 else "",
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
                upload, formats, p1, p2, p3, custom_heads, include_clean_pdf=False
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
            placeholder="Company, filename or year (e.g. Adani, 2025, 2024-25)",
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
            for n, d in r["files"].items():
                allfiles[f"{folder}/{n}"] = d
        allfiles["research_manifest.csv"] = manifest_csv(results)

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
            manifest_csv(results),
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
                h3.metric("Pages", len(r["raw_pages"]) if len(r["raw_pages"]) > 1 else "-")
                st.caption(r["name"])

                st.download_button(
                    "Download this report - all extracted files",
                    make_zip(r["files"]),
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
                        loc = payload_location(payload, full_count=len(r["raw_pages"]))
                    else:
                        loc = payload_location(payload)
                    cols[1].caption(loc)

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
