import semantic_v14 as sem
from semantic_v14 import normalize_heading

CASES = [
    ("Management Discussion and Analysis", "Management Discussion & Analysis", "HIGH"),
    ("Management Review and Analysis", "Management Discussion & Analysis", "HIGH"),
    ("Discussion and Analysis by Management", "Management Discussion & Analysis", "HIGH"),
    ("Discussion of Management", "Management Discussion & Analysis", "MEDIUM"),
    ("Operating and Financial Review", "Management Discussion & Analysis", "MEDIUM"),
    ("Business Review", "Management Discussion & Analysis", "MEDIUM"),
    ("Report of the Board of Directors", "Board's Report", "HIGH"),
    ("Directors Report", "Directors' Report", "HIGH"),
    ("Report on Corporate Governance", "Corporate Governance Report", "HIGH"),
    ("Business Responsibility and Sustainability Report", "Business Responsibility & Sustainability Report (BRSR)", "HIGH"),
    ("Sustainability Report", "Sustainability Report", "HIGH"),
    ("Enterprise Risk Management", "Risk Management", "HIGH"),
    ("Financial Risk Management Framework", "Financial Risk Management Framework", "LOW"),
    ("Cyber Security and Data Privacy", "Cybersecurity & IT Governance", "HIGH"),
    ("People and Culture", "Human Resources & Talent", "MEDIUM"),
]

for raw, expected, conf in CASES:
    got, got_conf, match_type = normalize_heading(raw)
    assert got == expected, (raw, got, expected, match_type)
    assert got_conf == conf, (raw, got_conf, conf, match_type)

# Boundary-safety regression: canonical statutory extraction must NOT receive
# all semantic headings as custom peers. That was able to truncate a Directors'
# Report to its heading page when a later section name appeared as a styled/body
# reference on that same page.
original_extract_custom = sem.extract_custom
calls = {}

def _fake_extract_custom(raw_pages, clean_pages, heading, custom_headings=None):
    calls["heading"] = heading
    calls["custom_headings"] = list(custom_headings or [])
    return {"text": "ok", "raw_text": "ok", "start_page": 1, "end_page": 2}

try:
    sem.extract_custom = _fake_extract_custom
    payload = sem._v14_extract_with_validated_path(
        [], [], "Directors' Report",
        ["Directors’ Report", "Management Discussion and Analysis", "Corporate Governance Report"],
    )
    assert payload is not None
    assert calls["heading"] == "Directors' Report"
    assert calls["custom_headings"] == ["Directors' Report"], calls
finally:
    sem.extract_custom = original_extract_custom

print(f"V14 semantic normalization + boundary safety: PASS — {len(CASES)}/15 semantic cases + canonical-boundary guard")
