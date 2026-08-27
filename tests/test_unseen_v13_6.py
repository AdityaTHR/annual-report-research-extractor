#!/usr/bin/env python3
"""V13.6 unseen-PDF structural validation with extraction cache.

This is intentionally stricter than the old V13.5 harness:
- validates only HARD generic boundaries as truncation-capable graph nodes;
- flags canonical section starts that look like body references;
- supports multi-value --only;
- caches raw extraction in .v13_unseen_cache.

Examples:
  python test_unseen_v13_6.py --dir unseen_samples --only "2022.pdf" "2025 (1).pdf" \
      --custom "Risk Management" --custom "Cybersecurity" --custom "Segment Performance"
  python test_unseen_v13_6.py --dir unseen_samples
"""
import argparse
import csv
import gzip
import hashlib
import inspect
import json
import pickle
import re
import time
from pathlib import Path

import extractor as ex

CACHE_DIR = Path(".v13_unseen_cache")
ROLE_WORDS = (
    "registrar", "transfer agent", "auditor", "chartered accountants",
    "depository", "banker", "trustee", "assurance provider",
)

_RAW_FUNCS = [
    "extract_source", "_layout_chunks", "_layout_lines", "_layout_chunk_lines",
    "_md_plain", "_text_quality", "_ocr",
]
_ORIGINAL_EXTRACT_SOURCE = ex.extract_source


def _engine_sig():
    parts = []
    for name in _RAW_FUNCS:
        fn = getattr(ex, name, None)
        if fn is None:
            continue
        try:
            parts.append(name + "\n" + inspect.getsource(fn))
        except Exception:
            parts.append(name + ":unavailable")
    return hashlib.sha256("\n\n".join(parts).encode("utf-8", errors="ignore")).hexdigest()


_ENGINE_SIG = _engine_sig()


def _cache_path(path: Path, data: bytes) -> Path:
    h = hashlib.sha256()
    h.update(_ENGINE_SIG.encode())
    h.update(b"\0")
    h.update(path.name.encode("utf-8", errors="ignore"))
    h.update(b"\0")
    h.update(data)
    key = h.hexdigest()[:24]
    safe = "".join(c if c.isalnum() or c in "._-()" else "_" for c in path.name)
    return CACHE_DIR / f"{safe}.{key}.pkl.gz"


def cached_extract(path: Path):
    data = path.read_bytes()
    CACHE_DIR.mkdir(exist_ok=True)
    cp = _cache_path(path, data)
    if cp.exists():
        t0 = time.time()
        try:
            with gzip.open(cp, "rb") as f:
                pages = pickle.load(f)
            print(f"[cache] reused raw extraction in {time.time()-t0:.2f}s")
            return pages
        except Exception:
            cp.unlink(missing_ok=True)

    print("[cache] no valid cache; extracting once...")
    pages = _ORIGINAL_EXTRACT_SOURCE(path.name, data)
    try:
        with gzip.open(cp, "wb", compresslevel=3) as f:
            pickle.dump(pages, f, protocol=pickle.HIGHEST_PROTOCOL)
        print(f"[cache] saved {cp}")
    except Exception as e:
        print(f"[cache] warning: {e}")
    return pages


def pos(node):
    return (node.get("index", -1), node.get("line_order", 0))


def preservation_ratio(raw_pages):
    native = preserved = 0
    for p in raw_pages:
        nt = str(p.get("native_text") or "")
        if len(nt.strip()) < 20 or ex._text_quality(nt) < 0.45:
            continue
        native += len(nt.encode("utf-8", errors="ignore"))
        preserved += len(ex._preserved_page_text(p).encode("utf-8", errors="ignore"))
    return preserved / native if native else 1.0


def _trivial_hard_generic(node):
    if not node.get("generic_boundary") or not node.get("hard_boundary"):
        return False
    t = ex._norm_line(node.get("matched_text", ""))
    c = ex._compact(t)
    if not t:
        return True
    if c in {"na","nil","and","or","on","in","for","the","a","an","sr","it","we","share","capital","rate","age"}:
        return True
    if re.fullmatch(r"\(?[a-zivxlcdm]{1,3}\)?[.)]?", t.strip().lower()):
        return True
    if re.search(r"\b(?:integrated\s+)?annual\s+report\b", t, re.I) and re.search(r"20\d{2}", t):
        return True
    return False


def _canonical_body_reference_suspect(node):
    if node.get("generic_boundary"):
        return False
    src = str(node.get("detection_source") or "")
    if src not in {"native", "layout"}:
        return False
    label = ex._canonical_request_label(node.get("label"))
    if label in {"Annexure I","Annexure II","Annexure III"}:
        return False
    t = ex._norm_line(node.get("matched_text", ""))
    if not t:
        return False
    lc = ex._compact(label or "")
    tc = ex._compact(t)
    if not lc or lc == tc:
        return False
    # Long sentence-like matches at modest score are likely references rather than headings.
    words = re.findall(r"[A-Za-z]+", t)
    sentence_like = len(words) >= 7 or t.endswith((".", ";", ":")) or t.startswith(("•","-","–","—"))
    return sentence_like and node.get("score", 0) < 72 and len(tc) > len(lc) + 14


def inspect_one(path, custom_headings):
    raw = cached_extract(path)
    clean = ex.clean_pages(raw)
    meta = ex.infer_metadata(path.name, raw)
    graph = ex.build_global_section_graph(clean, custom_headings=custom_headings)

    checks, warnings = [], []
    def check(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    positions = [pos(n) for n in graph]
    check("graph_strictly_ordered", positions == sorted(positions), str(positions[:8]))
    check("no_duplicate_node_positions", len(positions) == len(set(positions)), "")

    bad_hard = [
        (n.get("label"), n.get("pdf_page"), n.get("matched_text"))
        for n in graph if _trivial_hard_generic(n)
    ]
    check("hard_generic_boundary_quality", not bad_hard, repr(bad_hard[:8]))

    suspects = [
        (n.get("label"), n.get("pdf_page"), n.get("score"), n.get("matched_text"))
        for n in graph if _canonical_body_reference_suspect(n)
    ]
    check("canonical_body_reference_guard", not suspects, repr(suspects[:8]))

    company_low = str(meta.get("company", "")).lower()
    bad_role = [w for w in ROLE_WORDS if w in company_low]
    check("company_metadata_context", bool(meta.get("company")) and not bad_role, ", ".join(bad_role))
    check("financial_year_format", bool(re.fullmatch(r"\d{4}-\d{2}", str(meta.get("year", "")))), str(meta.get("year")))

    ratio = preservation_ratio(raw)
    check("full_text_preservation_2pct", 0.98 <= ratio <= 1.02, f"ratio={ratio:.4f}")

    hard_generic = [n for n in graph if n.get("generic_boundary") and n.get("hard_boundary")]
    soft_generic = [n for n in graph if n.get("generic_boundary") and not n.get("hard_boundary")]
    if len(soft_generic) > max(40, 3 * max(1, len(hard_generic))):
        warnings.append(f"graph_noise: {len(soft_generic)} soft generic nodes vs {len(hard_generic)} hard generic nodes")

    missing_custom = []
    for h in custom_headings:
        hc = ex._compact(h)
        found = any(
            ex._compact(n.get("label", "")) == hc or
            ex._compact(n.get("matched_text", "")) == hc or
            hc in ex._compact(n.get("matched_text", ""))
            for n in graph
        )
        if not found:
            missing_custom.append(h)

    nodes = []
    for i, n in enumerate(graph):
        nxt = graph[i + 1] if i + 1 < len(graph) else None
        nodes.append({
            "label": n.get("label"),
            "matched_text": n.get("matched_text"),
            "pdf_page": n.get("pdf_page"),
            "printed_page": n.get("printed_page"),
            "score": n.get("score"),
            "source": n.get("detection_source"),
            "generic": bool(n.get("generic_boundary")),
            "hard_boundary": bool(n.get("hard_boundary")),
            "custom": bool(n.get("custom_requested")),
            "next_label": nxt.get("label") if nxt else None,
            "next_pdf_page": nxt.get("pdf_page") if nxt else None,
        })

    return {
        "file": path.name,
        "metadata": meta,
        "pages": len(raw),
        "graph_nodes": nodes,
        "hard_generic_nodes": len(hard_generic),
        "soft_generic_nodes": len(soft_generic),
        "missing_custom": missing_custom,
        "checks": checks,
        "warnings": warnings,
        "pass": all(c["ok"] for c in checks),
    }


def main():
    ap = argparse.ArgumentParser(description="V13.6 strict unseen-PDF structural harness")
    ap.add_argument("--dir", default="unseen_samples")
    ap.add_argument("--custom", action="append", default=[])
    ap.add_argument("--only", nargs="+", default=None, help="Exact filename(s) or case-insensitive substrings")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()

    root = Path(args.dir)
    pdfs = sorted(root.glob("*.pdf"))
    if args.only:
        wanted = [x.lower() for x in args.only]
        pdfs = [p for p in pdfs if any(w == p.name.lower() or w in p.name.lower() for w in wanted)]
    pdfs = pdfs[:args.limit]
    if not pdfs:
        raise SystemExit(f"No matching PDFs found in {root}")

    results = []
    for p in pdfs:
        print(f"\n=== {p.name} ===")
        r = inspect_one(p, args.custom)
        results.append(r)
        print(
            f"company={r['metadata'].get('company')!r} FY={r['metadata'].get('year')!r} "
            f"nodes={len(r['graph_nodes'])} hard_generic={r['hard_generic_nodes']} "
            f"soft_generic={r['soft_generic_nodes']}"
        )
        for c in r["checks"]:
            print(f"  {'PASS' if c['ok'] else 'FAIL'} {c['check']} {c['detail']}")
        for w in r["warnings"]:
            print(f"  WARN {w}")
        if r["missing_custom"]:
            print("  INFO custom absent:", ", ".join(r["missing_custom"]))

    Path("v13_6_unseen_diagnostics.json").write_text(json.dumps(results, indent=2, ensure_ascii=False))
    rows = []
    for r in results:
        for n in r["graph_nodes"]:
            rows.append({"file": r["file"], **n})
    with open("v13_6_graph_nodes.csv", "w", newline="", encoding="utf-8-sig") as f:
        fields = [
            "file","label","matched_text","pdf_page","printed_page","score","source",
            "generic","hard_boundary","custom","next_label","next_pdf_page",
        ]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    failures = sum(1 for r in results if not r["pass"])
    print("\n" + "=" * 72)
    print(f"UNSEEN V13.6: {'PASS' if failures == 0 else 'REVIEW'} — {len(results)-failures}/{len(results)} PDFs passed strict structural checks")
    print("Diagnostics: v13_6_unseen_diagnostics.json, v13_6_graph_nodes.csv")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
