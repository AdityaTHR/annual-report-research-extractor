from mda_units_v15 import sentence_split, detect_mda_subsection, build_paragraph_rows, build_coverage_audit
from quality_v15 import _choose_text, _is_suspicious

assert detect_mda_subsection("INDUSTRY OVERVIEW") == "Industry Overview"
assert detect_mda_subsection("Financial Performance") == "Financial Performance"
assert detect_mda_subsection("Revenue increased by 20% during the year.") is None

sentences = sentence_split("Revenue was Rs. 12.5 crore. Outlook remains positive. Q1. performance improved.")
assert len(sentences) >= 2

page_rows = [{
    "report_id": "TEST_2025",
    "page": 10,
    "quality_decision": "NATIVE",
    "text_source": "layout",
    "text": "INDUSTRY OVERVIEW\nDemand improved during the year.\nMargins remained stable.\n• Capacity expanded",
}]
paragraphs = build_paragraph_rows(page_rows)
assert any(x["unit_type"] == "SECTION_HEADING" for x in paragraphs)
assert any(x["unit_type"] == "BULLET" for x in paragraphs)
assert all("quality_decision" in x for x in paragraphs)

choice = _choose_text("Ã Ã broken text word word word word", "clean text word word word word", ["MOJIBAKE_SIGNAL"])
assert choice[0] in {"OCR", "REVIEW"}
flagged, reasons = _is_suspicious("Ã Ã broken text word word word word")
assert flagged and reasons

coverage = build_coverage_audit("TEST", [{"page": 5, "quality_decision": "NATIVE"}, {"page": 6, "quality_decision": "NATIVE"}], {
    "start_page": 5, "end_page": 6, "original_heading": "Management Discussion and Analysis",
    "semantic_confidence": "HIGH",
})
assert coverage[0]["status"] == "PASS"
assert coverage[0]["expected_page_count"] == 2

print("V15 additive tests: PASS")
