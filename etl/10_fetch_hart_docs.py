"""Step 10 — fetch HART financial documents from the reports-and-documents page.

The HART website serves documents via a WordPress OutoftheBox plugin backed by
Dropbox. Downloads are via an AJAX action; the file browser requires a
per-session WordPress nonce embedded in the page HTML.

Two documents are fetched with hardcoded known paths (no browsing needed):

* **Recovery Plan** (2022 baseline, FTA Documents folder)
  → data/raw/hart/recovery_plan.pdf
  Primary source for total program capital cost (~$9.2B).

* **FFGA Amended** (2024, FTA Documents folder) used as the five-year-plan proxy
  → data/raw/hart/five_year_plan.pdf
  The 2024 Amended Full Funding Grant Agreement has updated total program budget
  and year-by-year federal capital commitments. No standalone 5-year financial
  plan exists on the HART website as of 2026-05.

A third document (annual_report) is sought by dynamic listing but is optional
(MIN_REQUIRED = 2, so the two hardcoded docs suffice).

Each file is accompanied by a manifest sidecar
(``data/raw/hart/<slug>.pdf.manifest.json``) via ``common/manifest``.

Idempotent: skips download if the PDF + manifest already exist, unless
``--force`` is passed.

Usage::

    python etl/10_fetch_hart_docs.py
    python etl/10_fetch_hart_docs.py --force
"""

from __future__ import annotations

import argparse
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, unquote, quote

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common.http_client import fetch_bytes, fetch_text  # noqa: E402
from common.manifest import write_manifest              # noqa: E402

SCRIPT_NAME  = "etl/10_fetch_hart_docs.py"
REPORTS_URL  = "https://honolulutransit.org/about/reports-and-documents/"
AJAX_URL     = "https://honolulutransit.org/wp-admin/admin-ajax.php"
ACCOUNT_ID   = "dbid:AAB8fd0xcPFdfzX57KFNGkEvwbSfUu_3l3Y"
OUT_DIR      = _ROOT / "data" / "raw" / "hart"

# FTA Documents module's data-source attribute (stable identifier in page HTML)
FTA_SOURCE_ID = "0fc67db660d63be01b01cc0812820a93"

# Hardcoded paths for documents whose locations are stable government records.
# key = slug, value = (module_source_id, exact_dropbox_path)
_KNOWN_PATHS: dict[str, tuple[str, str]] = {
    "recovery_plan": (
        FTA_SOURCE_ID,
        "/FTA Documents/20220603 - HART 2022 Recovery Plan.pdf",
    ),
    # The 2024 Amended FFGA is used as the five_year_plan proxy because HART no
    # longer publishes a standalone Five-Year Financial Plan on their website.
    # It has the updated total program budget and FY26 federal allocations.
    "five_year_plan": (
        FTA_SOURCE_ID,
        "/FTA Documents/20240201 - Amended Full Funding Grant Agreement (FFGA).pdf",
    ),
}

# Doc specs: (slug, output_filename, filename-patterns-for-dynamic-discovery)
# Only annual_report relies on dynamic discovery; the other two use _KNOWN_PATHS.
_DOC_SPECS: list[tuple[str, str, list[str]]] = [
    (
        "recovery_plan",
        "recovery_plan.pdf",
        [r"recovery\s+plan"],
    ),
    (
        "five_year_plan",
        "five_year_plan.pdf",
        [r"five.?year\s+financial\s+plan", r"5.?year\s+financial\s+plan",
         r"five.?year.*plan"],
    ),
    (
        "annual_report",
        "annual_report.pdf",
        [r"annual\s+report.*fy\s*\d+", r"fy\s*\d+.*annual\s+report",
         r"annual\s+financial\s+report"],
    ),
]

MIN_REQUIRED = 2  # recovery_plan + five_year_plan are always available


# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------

class _ModuleParser(HTMLParser):
    """Collect (token, source) pairs from OutoftheBox widget divs."""

    def __init__(self):
        super().__init__()
        self.modules: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = dict(attrs)
        cls = attrs_d.get("class", "") or ""
        if "OutoftheBox" not in cls and "outofthebox" not in cls.lower():
            return
        token  = attrs_d.get("data-token") or attrs_d.get("data-listtoken") or ""
        source = attrs_d.get("data-source") or attrs_d.get("data-account") or ""
        if token:
            self.modules.append({"token": token, "source": source})


class _EntryParser(HTMLParser):
    """Extract file entries from OutoftheBox AJAX HTML response."""

    def __init__(self):
        super().__init__()
        self._entries: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = dict(attrs)
        cls = attrs_d.get("class", "") or ""
        if "entry" not in cls:
            return
        entry_type = "file" if "file" in cls else ("folder" if "folder" in cls else None)
        if not entry_type:
            return
        name = attrs_d.get("data-name", "")
        url  = attrs_d.get("data-url", "")
        if name and name != "Previous folder":
            self._entries.append({"type": entry_type, "name": name, "url": url})

    @property
    def entries(self) -> list[dict[str, str]]:
        return self._entries


def _parse_modules(html: str) -> list[dict[str, str]]:
    p = _ModuleParser()
    p.feed(html)
    seen: set[str] = set()
    return [m for m in p.modules if m["token"] not in seen and not seen.add(m["token"])]  # type: ignore[func-returns-value]


def _extract_nonce(html: str) -> str | None:
    m = re.search(r'"refresh_nonce"\s*:\s*"([^"]+)"', html)
    return m.group(1) if m else None


def _parse_entries(html: str) -> list[dict[str, str]]:
    p = _EntryParser()
    p.feed(html)
    return p.entries


# ---------------------------------------------------------------------------
# AJAX calls
# ---------------------------------------------------------------------------

def _download_url(token: str, dropbox_path: str) -> str:
    """Build the outofthebox-download URL for a given (token, path) pair."""
    return AJAX_URL + "?" + urlencode({
        "action":          "outofthebox-download",
        "OutoftheBoxpath": dropbox_path,
        "account_id":      ACCOUNT_ID,
        "listtoken":       token,
    })


def _list_folder(token: str, folder_path: str, nonce: str) -> list[dict[str, str]]:
    """Call outofthebox-get-filelist and return parsed entries.

    Returns [] on any error.
    """
    from urllib.request import urlopen, Request

    # lastpath must be single-URL-encoded; urlencode() will double-encode it,
    # matching what the browser sends (e.g. %252F for root, %252FFTA%2520Documents).
    payload = urlencode({
        "listtoken":   token,
        "account_id":  ACCOUNT_ID,
        "lastpath":    quote(folder_path),
        "sort":        "name:desc",
        "action":      "outofthebox-get-filelist",
        "_ajax_nonce": nonce,
        "mobile":      "false",
        "query":       "",
        "page_url":    REPORTS_URL,
    }).encode()
    req = Request(AJAX_URL, data=payload,
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        import json
        with urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        html_fragment = data.get("html", "")
        return _parse_entries(html_fragment)
    except Exception as exc:
        print(f"  [warn] folder listing failed token={token!r} path={folder_path!r}: {exc}")
        return []


# ---------------------------------------------------------------------------
# Discovery logic
# ---------------------------------------------------------------------------

def _matches_any(name: str, patterns: list[str]) -> bool:
    return any(re.search(pat, name, re.IGNORECASE) for pat in patterns)


def _discover_docs(
    modules: list[dict[str, str]],
    nonce: str | None,
) -> dict[str, tuple[str, str, str]]:
    """Return {slug: (download_url, dropbox_path, token)}.

    First resolves hardcoded _KNOWN_PATHS, then attempts dynamic listing
    for any remaining slugs (annual_report).
    """
    found: dict[str, tuple[str, str, str]] = {}
    source_to_token = {m["source"]: m["token"] for m in modules if m["source"]}

    # Step 1: hardcoded known paths
    for slug, (source_id, dpath) in _KNOWN_PATHS.items():
        token = source_to_token.get(source_id)
        if not token:
            print(f"  [warn]  Module source={source_id!r} not found in page; "
                  f"cannot locate {slug}")
            continue
        url = _download_url(token, dpath)
        found[slug] = (url, dpath, token)
        print(f"  [known] {slug} → {dpath!r}")

    if all(s in found for s, *_ in _DOC_SPECS):
        return found

    # Step 2: dynamic listing for remaining slugs (annual_report)
    if not nonce:
        print("  [warn]  No nonce found in page HTML; skipping dynamic listing")
        return found

    remaining = [(s, fn, pats) for s, fn, pats in _DOC_SPECS if s not in found]
    for module in modules:
        if not remaining:
            break
        token = module["token"]
        entries = _list_folder(token, "/", nonce)
        for entry in entries:
            name = entry["name"]
            url_encoded = entry["url"]
            if not name.lower().endswith(".pdf"):
                continue
            # url_encoded is the URL-encoded dropbox path from data-url attribute
            dpath = unquote(url_encoded)
            for i, (slug, _fn, patterns) in enumerate(remaining):
                if _matches_any(name, patterns):
                    dl_url = _download_url(token, dpath)
                    found[slug] = (dl_url, dpath, token)
                    print(f"  [match] {slug} → {name!r}")
                    remaining.pop(i)
                    break

    return found


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def fetch_docs(*, force: bool) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[fetch] {REPORTS_URL}")
    html = fetch_text(REPORTS_URL, timeout=30)

    modules = _parse_modules(html)
    if not modules:
        print("[error] No OutoftheBox plugin modules found on the page.", file=sys.stderr)
        print(
            "  The page HTML did not contain any elements with class 'OutoftheBox'.\n"
            "  Check that the page is reachable and that HART has not restructured\n"
            "  the reports page. Update the module parser or _KNOWN_PATHS if needed.",
            file=sys.stderr,
        )
        return 1

    print(f"[info]  {len(modules)} plugin module(s) found")
    nonce = _extract_nonce(html)
    if not nonce:
        print("[warn]  refresh_nonce not found in OutoftheBox_vars "
              "— dynamic listing will be skipped")

    found = _discover_docs(modules, nonce)

    missing = [s for s, *_ in _DOC_SPECS if s not in found]
    if len(found) < MIN_REQUIRED:
        print(
            f"[error] Only {len(found)} of {len(_DOC_SPECS)} document types located. "
            f"Missing: {missing}",
            file=sys.stderr,
        )
        print(
            "  If HART reorganized the reports page or the FTA Documents folder,\n"
            "  update _KNOWN_PATHS in this script with the new file paths.",
            file=sys.stderr,
        )
        return 1

    if missing:
        print(f"[warn]  Could not locate: {missing} — proceeding with {len(found)} doc(s)")

    downloaded = 0
    for slug, (url, dpath, token) in found.items():
        filename = next(fn for s, fn, _ in _DOC_SPECS if s == slug)
        out_path = OUT_DIR / filename
        manifest_path = out_path.with_suffix(out_path.suffix + ".manifest.json")

        if not force and out_path.exists() and manifest_path.exists():
            print(f"[skip]  {out_path.relative_to(_ROOT)} (cached)")
            continue

        print(f"[dl]    {slug}  {url}")
        data = fetch_bytes(url, timeout=180)
        if len(data) < 1024:
            print(
                f"[error] {slug}: download returned only {len(data)} bytes — "
                "likely an error page, not a PDF.",
                file=sys.stderr,
            )
            print(f"  URL: {url}", file=sys.stderr)
            return 1

        out_path.write_bytes(data)
        write_manifest(
            out_path,
            source_url=url,
            row_count=1,
            script=SCRIPT_NAME,
            extras={
                "slug":        slug,
                "dropbox_path": dpath,
                "token":       token,
                "size_bytes":  len(data),
                "reports_url": REPORTS_URL,
            },
        )
        print(f"  → {out_path.relative_to(_ROOT)} ({len(data):,} bytes)")
        downloaded += 1

    print(f"[done]  {downloaded} file(s) downloaded to {OUT_DIR.relative_to(_ROOT)}/")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    ap.add_argument("--force", action="store_true",
                    help="Re-download even if cached files exist.")
    args = ap.parse_args(argv)
    return fetch_docs(force=args.force)


if __name__ == "__main__":
    sys.exit(main())
