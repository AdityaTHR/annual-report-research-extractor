import re
import streamlit as st
from extractor import *

st.set_page_config(page_title="Annual Report Research Extractor", page_icon="📄", layout="wide")
st.title("Annual Report Research Extractor")
st.caption("Batch extraction and structured research-ready downloads")

uploads = st.file_uploader(
    "Upload reports",
    type=["pdf", "txt", "docx", "md"],
    accept_multiple_files=True,
    help="Select one or many PDF, TXT, DOCX or Markdown files.",
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

if uploads:
    x, y = st.columns(2)
    x.metric("Files selected", len(uploads))
    y.metric("Total size", f"{sum(f.size for f in uploads)/(1024**2):.1f} MB")

if "results" not in st.session_state:
    st.session_state.results = []

if st.button("Process", type="primary", disabled=not uploads or not formats, use_container_width=True):
    wanted = []
    if p1:
        wanted.extend(["Chairman Message", "CEO Message", "Managing Director Message"])
    if p2:
        wanted.append("Management Discussion & Analysis")
    custom_heads = [x.strip() for x in custom.splitlines() if x.strip()]

    st.session_state.results = []
    bar = st.progress(0)
    status = st.empty()

    for i, f in enumerate(uploads):
        status.write(f"Processing {i+1}/{len(uploads)} - {f.name}")
        raw = f.getvalue()
        raw_pages = extract_source(f.name, raw)
        research_pages = clean_pages(raw_pages)
        sections = {}

        for label in wanted:
            sec = extract_preset(raw_pages, research_pages, label)
            if sec:
                sections[label] = sec

        if p1:
            combined = combine_sections(
                raw_pages, research_pages, sections,
                ["Chairman Message", "CEO Message", "Managing Director Message"],
                "Leadership Messages (Combined)",
            )
            if combined:
                # Put the professor-style combined leadership file first.
                sections = {"Leadership Messages (Combined)": combined, **sections}

        if p3:
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

        stem = base_stem(f.name)
        full = {"text": raw_full_text(raw_pages)}
        items = {"Full Report": full, **sections}
        generated = {}
        source_is_pdf = f.name.lower().endswith(".pdf")

        for label, payload in items.items():
            for fmt in formats:
                name, data = format_file(
                    stem, label, payload, fmt, raw_pages,
                    source_bytes=raw, source_is_pdf=source_is_pdf, all_sections=sections,
                )
                generated[name] = data

        st.session_state.results.append({
            "name": f.name,
            "stem": stem,
            "meta": infer_metadata(f.name, raw_pages),
            "raw_pages": raw_pages,
            "search_pages": research_pages,
            "sections": sections,
            "items": items,
            "files": generated,
            "formats": formats,
            "raw": raw if source_is_pdf else None,
            "is_pdf": source_is_pdf,
        })
        bar.progress((i + 1) / len(uploads))

    status.success(f"Processed {len(uploads)} report(s)")

results = st.session_state.results
if results:
    st.divider()
    st.subheader("Research Library")

    q = st.text_input("Find report", placeholder="Company, filename or year (e.g. Adani, 2025, 2024-25)")
    valid_years = sorted({r["meta"]["year"] for r in results if r["meta"]["year"] != "Year not detected"})
    year = st.selectbox("Year", ["All"] + valid_years)

    filtered = []
    qn = q.lower().strip()
    for r in results:
        searchable = f"{r['meta']['company']} {r['meta']['year']} {r['name']}".lower()
        if (not qn or qn in searchable) and (year == "All" or r["meta"]["year"] == year):
            filtered.append(r)

    allfiles = {}
    for r in results:
        folder = f"{r['meta']['company']}_{r['meta']['year']}".replace(" ", "_")
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
            st.caption("Extracted sections are subsets of the full report, so their page counts are not expected to add up to the total report pages.")

            for label, payload in r["items"].items():
                cols = st.columns([3, 1.4] + [1] * len(r["formats"]))
                cols[0].markdown(f"**{label}**")
                if label == "Full Report":
                    loc = f"{len(r['raw_pages'])} pages" if len(r["raw_pages"]) > 1 else "Complete"
                else:
                    s, e = payload.get("start_page"), payload.get("end_page")
                    loc = f"Pages {s}-{e}" if s is not None else "Detected"
                cols[1].caption(loc)

                safe = safe_label(label)
                for j, fmt in enumerate(r["formats"], start=2):
                    fn = f"{r['stem']}_{safe}.{FORMAT_EXT[fmt]}"
                    if fn in r["files"]:
                        cols[j].download_button(fmt, r["files"][fn], fn, key=f"{r['name']}_{label}_{fmt}")

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
                    strength = st.slider("Cleanup strength", 190, 235, 215, key="strength_" + r["name"])
                    st.caption("Creates a separate cleaned copy. The original PDF is never changed.")
                    clean_key = "clean_pdf_" + r["name"]
                    if st.button("Create cleaned PDF", key="make_" + r["name"]):
                        with st.spinner("Cleaning PDF..."):
                            st.session_state[clean_key] = clean_background_pdf(r["raw"], strength)
                    if clean_key in st.session_state:
                        st.download_button(
                            "Download cleaned PDF",
                            st.session_state[clean_key],
                            f"{r['stem']}_Background_Clean.pdf",
                            "application/pdf",
                            key="download_clean_" + r["name"],
                        )
