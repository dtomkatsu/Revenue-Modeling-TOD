"""Step 02b — fetch FY26 Board of Water Supply budget PDFs from the public
financial-statements index page.

Scrapes ``https://www.boardofwatersupply.com/department-financial-statements/``
for anchor tags pointing to the four expected PDF types:

* **combined** — "Operating & CIP Budget - FY <year>" (combined Op + CIP book)
* **cip_standalone** — "CIP Budget FY <year>" (CIP volume alone)
* **six_year_cip** — "Capital Improvement Program Fiscal Years <year>-<year>"
* **amendment_<N>** — "Budget FY <year> - Amendment No. <N>" (every numbered
  amendment is downloaded; highest N is recorded as the active amendment)

Each PDF lands in ``data/raw/budget/bws/<slug>.pdf`` with a sibling
``<slug>.pdf.manifest.json`` recording the discovered URL, anchor text,
and (for amendments) the amendment number.

URL discovery is dynamic each run because the BWS CMS rotates per-file hash
segments (e.g. ``/media/ytfd1egf/...``) on every upload. Hardcoded URLs are
banned — see BWS-AUTO-PLAN.md §2.1.

Idempotent: a PDF is skipped if both the file and its manifest exist and
``--force`` is not passed. New amendments (higher N than any cached) are
detected on each run and downloaded automatically without ``--force``.

Hard-fails (exit 1) if fewer than four expected document types are
located on the index page — partial caches are not produced.

Usage::

    python etl/02b_fetch_bws_budget.py
    python etl/02b_fetch_bws_budget.py --force
"""

from __future__ import annotations

import argparse
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common.http_client import fetch_bytes, fetch_text  # noqa: E402
from common.manifest     import read_manifest, write_manifest  # noqa: E402


SCRIPT_NAME = "etl/02b_fetch_bws_budget.py"
INDEX_URL   = "https://www.boardofwatersupply.com/department-financial-statements/"
OUT_DIR     = _ROOT / "data" / "raw" / "budget" / "bws"

# Anchor-text classifiers. Order matters: amendments are checked first
# because their text also contains "Budget FY" which would otherwise match
# the combined pattern. The combined pattern is checked before cip_standalone
# for the same reason (combined text contains "CIP Budget" as a substring).
_AMENDMENT_RE = re.compile(
    r"Budget\s+FY[^A-Za-z]*Amendment\s+(?:No\.?\s*)?(\d+)", re.IGNORECASE,
)
_COMBINED_RE = re.compile(
    r"Operating\s*(?:&|and)\s*CIP\s+Budget", re.IGNORECASE,
)
_CIP_STANDALONE_RE = re.compile(
    r"^\s*CIP\s+Budget\s+FY\s*\d{4}", re.IGNORECASE,
)
_SIX_YEAR_RE = re.compile(
    r"Capital\s+Improvement\s+Program\s+Fiscal\s+Years?\s+\d{4}", re.IGNORECASE,
)

# All four document types are required by the task spec; hard-fail otherwise.
_REQUIRED_TYPES = ("combined", "cip_standalone", "six_year_cip", "amendment")


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

class _PdfLinkParser(HTMLParser):
    """Collect (href, visible_text) pairs for every <a> tag pointing at a PDF."""

    def __init__(self) -> None:
        super().__init__()
        self._links: list[tuple[str, str]] = []
        self._current_href: str | None = None
        self._current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href") or ""
        if ".pdf" in href.lower():
            self._current_href = href
            self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._current_href is not None:
            self._current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._current_href is None:
            return
        text = re.sub(r"\s+", " ", "".join(self._current_text)).strip()
        if text:
            self._links.append((self._current_href, text))
        self._current_href = None
        self._current_text = []

    @property
    def links(self) -> list[tuple[str, str]]:
        return self._links


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _classify(text: str) -> tuple[str, int | None]:
    """Return (slug_kind, amendment_number_or_None) for a link's visible text.

    slug_kind ∈ {"amendment", "combined", "cip_standalone", "six_year_cip", ""}.
    Amendments yield ("amendment", N); others return their kind and None.
    Empty string = no match.
    """
    m = _AMENDMENT_RE.search(text)
    if m:
        try:
            return "amendment", int(m.group(1))
        except ValueError:
            return "amendment", None  # restated/non-numbered
    if _COMBINED_RE.search(text):
        return "combined", None
    if _CIP_STANDALONE_RE.search(text):
        return "cip_standalone", None
    if _SIX_YEAR_RE.search(text):
        return "six_year_cip", None
    return "", None


def _discover(html: str, base_url: str) -> dict[str, list[tuple[str, str, int | None]]]:
    """Walk the index page HTML and bucket PDF links by document type.

    Returns a dict keyed by slug_kind with a list of (absolute_url, text, N).
    Amendments are sorted descending by N; non-numbered amendments at the end.
    """
    parser = _PdfLinkParser()
    parser.feed(html)

    found: dict[str, list[tuple[str, str, int | None]]] = {
        "combined":       [],
        "cip_standalone": [],
        "six_year_cip":   [],
        "amendment":      [],
    }
    for href, text in parser.links:
        kind, n = _classify(text)
        if not kind:
            continue
        abs_url = urljoin(base_url, href)
        found[kind].append((abs_url, text, n))

    # Sort amendments by N descending; None (non-numbered) last.
    found["amendment"].sort(key=lambda t: (t[2] is None, -(t[2] or 0)))
    return found


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def _cache_paths(slug: str) -> tuple[Path, Path]:
    out = OUT_DIR / f"{slug}.pdf"
    return out, out.with_suffix(out.suffix + ".manifest.json")


def _download_one(
    slug: str,
    url: str,
    anchor_text: str,
    amendment_number: int | None,
    *,
    force: bool,
) -> bool:
    """Download a PDF if needed. Returns True on success or cache hit."""
    out_path, manifest_path = _cache_paths(slug)
    if not force and out_path.exists() and manifest_path.exists():
        print(f"[skip] {slug} (cached)")
        return True

    print(f"[fetch] {slug} <- {url}")
    try:
        body = fetch_bytes(url, timeout=180)
    except Exception as e:
        print(f"[error] {slug}: fetch failed: {e}", file=sys.stderr)
        return False

    if not body.startswith(b"%PDF"):
        print(f"[error] {slug}: response is not a PDF "
              f"(first bytes: {body[:8]!r})", file=sys.stderr)
        return False

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(body)

    extras: dict[str, object] = {
        "anchor_text": anchor_text,
        "byte_size":   len(body),
    }
    if amendment_number is not None:
        extras["amendment_number"] = amendment_number

    write_manifest(
        out_path,
        source_url=url,
        row_count=0,
        script=SCRIPT_NAME,
        extras=extras,
    )
    print(f"[done] {slug} ({len(body)/1_048_576:.1f} MiB)")
    return True


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def fetch_bws(*, force: bool) -> int:
    print(f"[index] {INDEX_URL}")
    html = fetch_text(INDEX_URL, timeout=30)

    found = _discover(html, INDEX_URL)

    # Hard-fail if any of the four required doc types is missing.
    missing = [t for t in _REQUIRED_TYPES if not found[t]]
    if missing:
        print(
            f"[error] Only {sum(bool(found[t]) for t in _REQUIRED_TYPES)}/4 "
            f"required doc types located. Missing: {missing}",
            file=sys.stderr,
        )
        print(
            "  The BWS index page layout may have changed. Inspect "
            f"{INDEX_URL} and update the anchor-text regexes in this script.",
            file=sys.stderr,
        )
        return 1

    print(f"[info] discovered: combined={len(found['combined'])}, "
          f"cip_standalone={len(found['cip_standalone'])}, "
          f"six_year_cip={len(found['six_year_cip'])}, "
          f"amendments={len(found['amendment'])}")

    # Establish current cached top-amendment N for "refresh on new amendment".
    cached_top_n: int | None = None
    for path in sorted(OUT_DIR.glob("amendment_*.pdf")):
        manifest = read_manifest(path)
        if manifest and isinstance(manifest.get("amendment_number"), int):
            n = manifest["amendment_number"]
            if cached_top_n is None or n > cached_top_n:
                cached_top_n = n

    # Discovered top-N amendment on the page.
    discovered_top_n = next(
        (n for _u, _t, n in found["amendment"] if n is not None), None,
    )
    new_amendment_available = (
        discovered_top_n is not None
        and (cached_top_n is None or discovered_top_n > cached_top_n)
    )
    if new_amendment_available and cached_top_n is not None:
        print(f"[info] new amendment detected: cached top = #{cached_top_n}, "
              f"page top = #{discovered_top_n} — will fetch new amendments")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Build the download queue. Singleton slugs use the first (and typically
    # only) entry. Amendments fan out to amendment_<N>.pdf, one per N.
    queue: list[tuple[str, str, str, int | None]] = []
    for kind in ("combined", "cip_standalone", "six_year_cip"):
        url, text, _n = found[kind][0]
        if len(found[kind]) > 1:
            print(f"[warn] {kind}: {len(found[kind])} candidates found; "
                  f"using {text!r}")
        queue.append((kind, url, text, None))

    for url, text, n in found["amendment"]:
        if n is None:
            print(f"[warn] amendment: non-numbered link skipped — {text!r}")
            continue
        queue.append((f"amendment_{n}", url, text, n))

    failures: list[str] = []
    for slug, url, text, n in queue:
        # Force-fetch any amendment newer than the previously cached top, even
        # if --force wasn't passed. Other slugs honor --force only.
        per_file_force = force or (
            n is not None and cached_top_n is not None and n > cached_top_n
        )
        if not _download_one(slug, url, text, n, force=per_file_force):
            failures.append(slug)

    if failures:
        print(f"[fail] {len(failures)} PDF(s) failed: {failures}", file=sys.stderr)
        return 1

    if discovered_top_n is not None:
        print(f"[done] active amendment: #{discovered_top_n}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    ap.add_argument("--force", action="store_true",
                    help="Refetch every PDF even if cached.")
    args = ap.parse_args(argv)
    return fetch_bws(force=args.force)


if __name__ == "__main__":
    sys.exit(main())
