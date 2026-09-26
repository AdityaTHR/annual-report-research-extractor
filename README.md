# FINAL V15 — lean production package

This package freezes the validated V14 structural engine and keeps V15 additive only: conservative packaging, selective page-quality QA, and MDA research-unit exports. It contains no company/year/page-specific production rules. Page-quality OCR is lazy: the PDF is reopened only when a non-OCR page is actually flagged as suspicious.

# Annual Report Research Extractor — V15 additive research layer

V15 keeps the validated **V14 / R4.2 structural boundary engine unchanged** and adds:

- Page-level text QA and provenance (`NATIVE / OCR / REVIEW`)
- Selective OCR comparison for suspicious pages
- `page_quality_manifest.csv` + `QA_summary.json`
- MDA page-, paragraph-, and sentence-level research datasets
- MDA review queue + coverage audit

## Important architecture rule

`extractor.py`, `semantic_v14.py`, and `batch_v14.py` remain the validated V14 structural path. V15 quality-selected text is **not** fed back into section-boundary discovery. `batch_v15.py` uses V14 to determine the MDA start/end and only then structures the content inside that range.

## Ubuntu setup

From the repository root:

```bash
chmod +x setup_ubuntu.sh
./setup_ubuntu.sh
```

Then:

```bash
source .venv/bin/activate
streamlit run app.py
```

## Local V15 batch run

```bash
source .venv/bin/activate
PYTHONPATH=. python batch_v15.py \
  --input /path/to/reports \
  --output v15_output \
  --formats TXT JSON \
  --pdf-only \
  --limit 5
```

`--limit 5` is a safe first test. Use `--limit 0` (or omit it) for all files.

## Colab

Keep the existing resumable Colab notebook, but change:

```python
from batch_v14 import process_file, SUPPORTED
```

to:

```python
from batch_v15 import process_file, SUPPORTED
```

All other resume/Drive logic can remain the same.

## Fast validation

```bash
PYTHONPATH=. python tests/test_v14_semantic.py
PYTHONPATH=. python tests/test_v15_addon.py
PYTHONPATH=. python tests/test_v15_packaging_guard.py
```

For the full frozen benchmark suite, place the benchmark PDFs in a samples folder and run:

```bash
PYTHONPATH=. python tests/quick_v13_check.py --dir samples
```

## V15.4 packaging guard

V15.4 keeps the validated V14 boundary engine unchanged and adds a conservative packaging guard. It suppresses obvious duplicate responsibility/sustainability outputs and Risk/HR/Cybersecurity headings that are wholly nested inside Board/Directors/MDA sections. Suppressed items remain traceable in semantic diagnostics and are recorded in `Metadata/section_packaging_audit.csv`. Manual overrides are never suppressed.


### V15.4 final guard

Adds three conservative, generic fixes while leaving the frozen V14 extractor unchanged:

- Recovers the **actual statutory CSR annexure** when a Directors/Board report body reference was previously selected as the CSR start. The recovery requires a visible Annexure wrapper plus a CSR-report title and terminates at the next Annexure wrapper.
- Stops an overlong narrative CSR chapter at the next **strong same-style sibling chapter heading** (for integrated-report layouts), without treating recurring navigation labels as boundaries.
- Writes schema headers even when `section_packaging_audit.csv` or an MDA QA CSV has zero rows, so downstream pandas/CSV readers can open empty audit files safely.

V15.4 continues to suppress duplicate responsibility/sustainability ranges, nested Risk/HR/Cybersecurity pseudo-sections, and one-page cyber-risk register cards. Manual overrides remain untouched.


### V15.4 real-layout hardening

V15.4 adds regression protection for layout quirks observed in the five-report integration set:

- CSR annexure titles containing lightweight layout markup such as `<sup>1</sup>` are normalized before semantic matching.
- Annexure wrappers are resolved by visual geometry, not only reading order, because two-up / layout extraction can emit the wrapper after the title or classify it as `folio-edge`.
- Narrative CSR sibling-boundary matching enriches layout headings with duplicate native font metadata, preventing ordinary CSR subheadings from being mistaken for peer chapters while still detecting genuine peers such as `Responsible supply chain`.

The frozen V14 detector and boundary engine remain unchanged.

## V15.5 generic boundary hardening

V15.5 keeps the frozen V14 structural engine unchanged. The additive packaging layer now:

- stops overlong BRR/BRSR/CSR/ESG/Sustainability packages at a new strong top-level report heading (for example an Independent Auditor's Report or Financial Statements);
- recognizes leadership title variants such as `Chief Executive's Review` as CEO-message candidates when V14 did not package them;
- repackages leadership messages to the next strong peer/top-level title instead of cutting them at an early signature line;
- retains the V15.4 statutory-CSR and real-layout safeguards.

These rules are generic category/visual-structure rules only. There are no company names, years, or fixed page numbers in production logic.

### V15.6 generic long-form boundary hardening

V15.6 adds two narrowly-scoped, layout-robust fallbacks for older annual reports:

- if an exact long-form report title (BRR/BRSR/CSR/ESG/Sustainability) is found on the immediately preceding physical page, the packaged start may be shifted back by one page; and
- an unmistakable Auditor/Financial-Statements title appearing in the first 12 extracted lines of a page can terminate an overlong responsibility/sustainability section even when the layout engine did not classify that line as a heading.

The fallback is exact-title and page-prefix constrained. Body mentions do not qualify.
