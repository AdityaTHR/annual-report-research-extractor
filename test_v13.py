#!/usr/bin/env python3
import argparse
import re
import sys
import time
import unicodedata
from pathlib import Path

import fitz

import extractor as ex


def rg(a, b):
    return (a, b)

CASES = {
    "TCS 2025": {
        "match": ["tata consultancy services", "2024-25"],
        "sections": {
            "Chairman": ("preset", "Chairman Message", rg(8, 9), rg(11, 12)),
            "CEO": ("preset", "CEO Message", rg(10, 11), rg(13, 14)),
            "Board's Report": ("custom", "Board's Report", rg(67, 78), rg(70, 81)),
            "MDA": ("preset", "Management Discussion & Analysis", rg(79, 94), rg(82, 97)),
            "Corporate Governance": ("custom", "Corporate Governance Report", rg(95, 114), rg(98, 117)),
            "CSR": ("custom", "Corporate social responsibility", rg(115, 126), rg(118, 129)),
            "BRSR": ("preset", "Business Responsibility & Sustainability Report (BRSR)", rg(127, 168), rg(130, 171)),
        },
        "absent": [],
        "metadata": ("Tata Consultancy Services", "2024-25"),
    },
    "TCS 2020": {
        "match": ["tata consultancy services", "2019-20"],
        "sections": {
            "Chairman": ("preset", "Chairman Message", rg(5, 6), rg(9, 10)),
            "CEO": ("preset", "CEO Message", rg(7, 11), rg(11, 15)),
            "MDA": ("preset", "Management Discussion & Analysis", rg(77, 120), rg(81, 124)),
            "BRR": ("preset", "Business Responsibility Report (BRR)", rg(121, 131), rg(125, 135)),
            "Corporate Governance": ("custom", "Corporate Governance Report", rg(132, 161), rg(136, 165)),
        },
        "absent": [("preset", "Managing Director Message", "separate MD message")],
        "metadata": ("Tata Consultancy Services", "2019-20"),
    },
    "BEL 2015": {
        "match": ["bharat electronics", "2014-15"],
        "sections": {
            "Chairman": ("preset", "Chairman Message", rg(1, 3), rg(10, 12)),
            "Board's Report": ("custom", "Board's Report", rg(11, 34), rg(20, 43)),
            "MDA": ("preset", "Management Discussion & Analysis", rg(35, 43), rg(44, 52)),
            "Corporate Governance": ("custom", "Corporate Governance Report", rg(45, 59), rg(54, 68)),
            "Sustainability": ("preset", "Sustainability Report", rg(60, 62), rg(69, 71)),
            "BRR": ("preset", "Business Responsibility Report (BRR)", rg(63, 72), rg(72, 81)),
        },
        "absent": [("custom", "Directors' Report", "duplicate Directors' Report")],
        "metadata": ("Bharat Electronics", "2014-15"),
    },
    "Adani 2025": {
        "match": ["adani enterprises", "2024-25"],
        "sections": {
            "MDA": ("preset", "Management Discussion & Analysis", rg(287, 300), rg(158, 171)),
            "Corporate Governance": ("custom", "Corporate Governance Report", rg(301, 348), rg(172, 219)),
            "BRSR": ("preset", "Business Responsibility & Sustainability Report (BRSR)", rg(349, 399), rg(220, 270)),
        },
        "absent": [],
        "metadata": ("Adani Enterprises", "2024-25"),
    },
    "Apollo 2016": {
        "match": ["apollo hospitals", "2015-16"],
        "sections": {
            "Chairman": ("preset", "Chairman Message", rg(2, 3), None),
            "Directors' Report": ("custom", "Directors' Report", rg(50, 86), None),
            "Corporate Governance": ("custom", "Corporate Governance Report", rg(87, 120), None),
            "MDA": ("preset", "Management Discussion & Analysis", rg(121, 149), None),
            "Clinical Governance": ("custom", "Clinical Governance", rg(150, 154), None),
        },
        "absent": [("preset", "Business Responsibility Report (BRR)", "fabricated BRR")],
        "metadata": ("Apollo Hospitals Enterprise", "2015-16"),
    },
}


def _norm_identity(text):
    text = unicodedata.normalize("NFKC", text).lower()
    text = text.replace("–", "-").replace("—", "-")
    text = re.sub(r"\b(20\d{2})\s*-\s*(20)?(\d{2})\b", lambda m: f"{m.group(1)}-{m.group(3)}", text)
    return re.sub(r"\s+", " ", text)


def quick_identity(path: Path):
    try:
        doc = fitz.open(path)
        # Identity should come from front matter only; 40 pages catches scanned/
        # sparse covers without letting unrelated later mentions dominate.
        text = "\n".join(doc[i].get_text("text") for i in range(min(40, len(doc))))
        doc.close()
        return _norm_identity(text)
    except Exception:
        return ""


def locate_reports(root: Path):
    pdfs = list(root.glob("*.pdf")) + list(root.glob("**/*.pdf"))
    pdfs = list(dict.fromkeys(p.resolve() for p in pdfs if p.is_file()))
    identities = {p: quick_identity(p) for p in pdfs}
    found = {}
    for case, cfg in CASES.items():
        company_token, year_token = cfg["match"]
        company_token = _norm_identity(company_token)
        year_token = _norm_identity(year_token)
        matches = []
        for p, text in identities.items():
            if company_token in text and year_token in text:
                # Prefer a filename hint only after strict content identity succeeds.
                hint = sum(tok in p.name.lower() for tok in re.findall(r"[a-z]+", company_token) if len(tok) > 4)
                matches.append((hint, p))
        if matches:
            found[case] = sorted(matches, key=lambda x: (-x[0], str(x[1])))[0][1]
    return found

def extract(mode, label, raw, clean):
    if mode == "preset":
        return ex.extract_preset(raw, clean, label)
    return ex.extract_custom(raw, clean, label)


def got_range(payload, printed=True):
    if not payload:
        return None
    if printed and payload.get("printed_start_page") is not None:
        s = payload.get("printed_start_page")
        e = payload.get("printed_end_page", s)
        return (s, e)
    s = payload.get("start_page")
    e = payload.get("end_page", s)
    return (s, e) if s is not None else None


def fmt(r):
    if r is None:
        return "—"
    return str(r[0]) if r[0] == r[1] else f"{r[0]}-{r[1]}"


def starts_like_heading(payload, label):
    if not payload or not payload.get("text"):
        return False
    head = " ".join(payload["text"].splitlines()[:8]).lower().replace("’", "'")
    keys = {
        "Chairman": ["chairman", "dear shareholder"],
        "CEO": ["ceo", "chief executive", "dear shareholder"],
        "MDA": ["management discussion", "business review"],
        "BRSR": ["business responsibility", "sustainability report"],
        "BRR": ["business responsibility report"],
        "Corporate Governance": ["corporate governance"],
        "Board's Report": ["board's report", "boards' report"],
        "Directors' Report": ["directors' report", "directors’ report"],
        "Clinical Governance": ["clinical governance"],
        "Sustainability": ["sustainability"],
        "CSR": ["corporate social responsibility", "csr"],
    }.get(label, [label.lower()])
    return any(k in head for k in keys)


def run_case(case, path):
    print(f"\n=== {case}: {path.name} ===")
    t0 = time.time()
    raw = ex.extract_source(path.name, path.read_bytes())
    clean = ex.clean_pages(raw)
    print(f"pages={len(raw)}  extraction={time.time()-t0:.1f}s  layout_pages={sum(bool(p.get('layout_engine')) for p in raw)}")
    cfg = CASES[case]
    failures = 0
    cache = {}

    print("Company | Section | Expected report | Detected report | Expected PDF | Detected PDF | Result")
    for display, (mode, label, expected_report, expected_pdf) in cfg["sections"].items():
        p = extract(mode, label, raw, clean)
        cache[display] = p
        gr = got_range(p, printed=True)
        gp = got_range(p, printed=False)
        ok = (gr == expected_report)
        if expected_pdf is not None:
            ok = ok and (gp == expected_pdf)
        # heading assertion guards against body-reference false starts
        heading_ok = starts_like_heading(p, display)
        ok = ok and heading_ok
        if not ok:
            failures += 1
        print(f"{case} | {display} | {fmt(expected_report)} | {fmt(gr)} | {fmt(expected_pdf)} | {fmt(gp)} | {'PASS' if ok else 'FAIL'}")
        if p and not heading_ok:
            print("  ASSERT FAIL: extracted text does not start like the requested section heading")

    for mode, label, why in cfg.get("absent", []):
        p = extract(mode, label, raw, clean)
        ok = p is None
        if not ok:
            failures += 1
        print(f"{case} | ABSENT {label} | — | {fmt(got_range(p, True))} | — | {fmt(got_range(p, False))} | {'PASS' if ok else 'FAIL'} ({why})")

    # Full-text assertion
    full = ex.raw_full_text(raw)
    if not full.strip():
        print("  ASSERT FAIL: full text is empty")
        failures += 1

    # Metadata assertions are now part of the V13.4 regression contract.
    meta = ex.infer_metadata(path.name, raw)
    print(f"metadata: company={meta.get('company')!r}, year={meta.get('year')!r}")
    expected_company, expected_year = cfg.get("metadata", (None, None))
    if expected_company and expected_company.lower() not in str(meta.get("company", "")).lower():
        print(f"  ASSERT FAIL: reporting company should be {expected_company!r}")
        failures += 1
    if expected_year and meta.get("year") != expected_year:
        print(f"  ASSERT FAIL: fiscal year should be {expected_year}")
        failures += 1

    # Additional content-boundary guards
    mda = cache.get("MDA")
    if mda and case == "Apollo 2016" and "clinical governance" in " ".join(mda.get("text", "").lower().splitlines()[-20:]):
        print("  ASSERT FAIL: Apollo MDA appears to include Clinical Governance boundary")
        failures += 1

    return failures


def main():
    ap = argparse.ArgumentParser(description="V13 Annual Report Extractor regression suite")
    ap.add_argument("--dir", default="samples", help="Folder containing the five regression PDFs (default: samples)")
    ap.add_argument("--only", nargs="+", choices=list(CASES), help="Run one or more named cases")
    args = ap.parse_args()

    root = Path(args.dir)
    if not root.exists():
        print(f"ERROR: {root} does not exist. Put the five regression PDFs there (or pass --dir PATH).")
        return 2

    found = locate_reports(root)
    wanted = args.only if args.only else list(CASES)
    missing = [c for c in wanted if c not in found]
    if missing:
        print("Missing regression PDFs:", ", ".join(missing))
        print("Put TCS 2025, TCS 2020, BEL 2015, Adani 2025 and Apollo 2016 PDFs in the test folder.")
        return 2

    print("Resolved regression files:")
    for case in wanted:
        print(f"  {case}: {found[case].name}")

    total_fail = 0
    for case in wanted:
        total_fail += run_case(case, found[case])

    print("\n" + "=" * 72)
    if total_fail == 0:
        print("V13 REGRESSION: PASS — all checked sections matched expected ranges/content guards.")
        return 0
    print(f"V13 REGRESSION: FAIL — {total_fail} assertion(s) failed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
