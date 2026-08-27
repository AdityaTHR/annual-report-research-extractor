#!/usr/bin/env python3
"""Fast wrapper around the existing test_v13.py.

It caches ONLY the expensive raw PDF extraction. The cache key includes the PDF bytes
and the source code of the raw-extraction functions, so boundary/graph/metadata edits
reuse the cache, while changes to the extraction engine automatically invalidate it.

Usage:
  python quick_v13_check.py --dir samples --only "TCS 2020" "BEL 2015" "Adani 2025"
  python quick_v13_check.py --dir samples
  python quick_v13_check.py --clear-cache --dir samples --only "BEL 2015"
"""
import argparse
import gzip
import hashlib
import inspect
import pickle
import shutil
import sys
import time
from pathlib import Path

import extractor as ex
import test_v13 as reg

CACHE_DIR = Path(".v13_cache")

# Functions that materially affect the expensive raw page extraction.
_RAW_FUNCS = [
    "extract_source", "_layout_chunks", "_layout_lines", "_layout_chunk_lines",
    "_md_plain", "_text_quality", "_ocr",
]


def _raw_engine_signature():
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


_ENGINE_SIG = _raw_engine_signature()
_ORIGINAL_EXTRACT_SOURCE = ex.extract_source


def _cache_path(name: str, data: bytes) -> Path:
    h = hashlib.sha256()
    h.update(_ENGINE_SIG.encode())
    h.update(b"\0")
    h.update(name.encode("utf-8", errors="ignore"))
    h.update(b"\0")
    h.update(data)
    key = h.hexdigest()[:24]
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in Path(name).name)
    return CACHE_DIR / f"{safe}.{key}.pkl.gz"


def cached_extract_source(name, data):
    CACHE_DIR.mkdir(exist_ok=True)
    path = _cache_path(name, data)
    if path.exists():
        t0 = time.time()
        try:
            with gzip.open(path, "rb") as f:
                pages = pickle.load(f)
            print(f"[cache] {Path(name).name}: reused raw extraction in {time.time()-t0:.2f}s")
            return pages
        except Exception:
            path.unlink(missing_ok=True)

    print(f"[cache] {Path(name).name}: no valid cache; running extraction once...")
    pages = _ORIGINAL_EXTRACT_SOURCE(name, data)
    try:
        with gzip.open(path, "wb", compresslevel=3) as f:
            pickle.dump(pages, f, protocol=pickle.HIGHEST_PROTOCOL)
        print(f"[cache] saved: {path}")
    except Exception as e:
        print(f"[cache] warning: could not save cache: {e}")
    return pages


def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--clear-cache", action="store_true")
    known, remaining = ap.parse_known_args()

    if known.clear_cache:
        shutil.rmtree(CACHE_DIR, ignore_errors=True)
        print("[cache] cleared .v13_cache")

    # test_v13.py already supports all normal args, including multi-value --only.
    reg.ex.extract_source = cached_extract_source
    sys.argv = [sys.argv[0]] + remaining
    return reg.main()


if __name__ == "__main__":
    raise SystemExit(main())
