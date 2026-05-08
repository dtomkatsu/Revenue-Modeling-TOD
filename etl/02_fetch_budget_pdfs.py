"""Step 02 — fetch FY26 Honolulu budget PDFs from DocuShare.

Downloads the FY26 Executive Operating Budget (Volume 1) and Capital Program
and Budget (Volume 2) from the City & County of Honolulu DocuShare collection
``Collection-15858`` to ``data/raw/budget/``. Each PDF gets a sibling
``.manifest.json`` recording its source URL, byte size, and fetched-at
timestamp.

These PDFs are step-03's input — that script extracts road/sewer/water
operating-and-maintenance totals via pdfplumber.

Idempotent: a PDF is skipped if both the file and its manifest already exist.
Pass ``--force`` to refetch (or list specific names).

Usage::

    python etl/02_fetch_budget_pdfs.py
    python etl/02_fetch_budget_pdfs.py --force
    python etl/02_fetch_budget_pdfs.py operating_fy26
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common.http_client import fetch_bytes  # noqa: E402
from common.manifest     import write_manifest  # noqa: E402


SCRIPT_NAME = "etl/02_fetch_budget_pdfs.py"

DOCUSHARE = "https://www4.honolulu.gov/docushare/dsweb/Get"

# FY26 Executive Program & Budget — DocuShare Collection-15858, posted 2025-03.
# Document IDs verified from the collection listing on 2026-05-07.
PDFS: dict[str, tuple[str, str]] = {
    # name -> (docushare document id, human title)
    "operating_fy26": ("Document-349475",
        "Executive Program and Budget FY26 Volume 1 - Operating"),
    "capital_fy26":   ("Document-349476",
        "Executive Program and Budget FY26 Volume 2 - Capital"),
}

OUT_DIR = _ROOT / "data" / "raw" / "budget"


def _cache_paths(name: str) -> tuple[Path, Path]:
    out = OUT_DIR / f"{name}.pdf"
    return out, out.with_suffix(out.suffix + ".manifest.json")


def fetch_one(name: str, doc_id: str, title: str, *, force: bool) -> bool:
    """Download one PDF. Returns True on success or cache hit."""
    out_path, manifest_path = _cache_paths(name)
    if not force and out_path.exists() and manifest_path.exists():
        print(f"[skip] {name} (cached)")
        return True

    url = f"{DOCUSHARE}/{doc_id}/{name}.pdf"
    print(f"[fetch] {name} <- {url}")
    try:
        body = fetch_bytes(url, timeout=180)
    except Exception as e:
        print(f"[error] {name}: fetch failed: {e}")
        return False

    if not body.startswith(b"%PDF"):
        print(f"[error] {name}: response is not a PDF (first bytes: {body[:8]!r})")
        return False

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(body)

    write_manifest(
        out_path,
        source_url=url,
        row_count=0,
        script=SCRIPT_NAME,
        extras={
            "title":        title,
            "docushare_id": doc_id,
            "byte_size":    len(body),
        },
    )
    print(f"[done] {name} ({len(body)/1_048_576:.1f} MiB)")
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true",
                    help="Refetch even if the cache exists.")
    ap.add_argument("names", nargs="*",
                    help="Subset of PDF names to fetch (default: all).")
    args = ap.parse_args(argv)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.names:
        unknown = [n for n in args.names if n not in PDFS]
        if unknown:
            print(f"[error] unknown name(s): {unknown}", file=sys.stderr)
            print(f"        known: {list(PDFS)}",       file=sys.stderr)
            return 2
        wanted = set(args.names)
    else:
        wanted = set(PDFS)

    failures: list[str] = []
    for name, (doc_id, title) in PDFS.items():
        if name not in wanted:
            continue
        if not fetch_one(name, doc_id, title, force=args.force):
            failures.append(name)

    if failures:
        print(f"[fail] {len(failures)} PDF(s) failed: {failures}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
