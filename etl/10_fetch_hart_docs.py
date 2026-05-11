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

# data-source attributes for the OutoftheBox modules on the reports page.
# These identify which Dropbox folder a module is rooted at and are stable
# across page loads (the per-session `data-token` is captured at fetch time).
FTA_SOURCE_ID     = "0fc67db660d63be01b01cc0812820a93"   # FTA Documents folder
REPORTS_SOURCE_ID = "d69959bcd5858b64c6016db323462cd1"   # Year-folder browser (Monthly Progress Reports)

# Hardcoded paths for documents whose locations are stable government records.
# key = slug, value = (module_source_id, exact_dropbox_path)
_KNOWN_PATHS: dict[str, tuple[str, str]] = {
    # The 2024 Amended FFGA has the current federal funding schedule used to
    # extract FY26 capital obligations (step 11).
    "ffga_amended": (
        FTA_SOURCE_ID,
        "/FTA Documents/20240201 - Amended Full Funding Grant Agreement (FFGA).pdf",
    ),
    # The 2022 Recovery Plan is retained as a secondary source for funding-mix
    # extraction (Table 3-1). It is NOT used for total program cost since the
    # 2022 EAC has been superseded by the monthly "Current Forecast" column.
    "recovery_plan": (
        FTA_SOURCE_ID,
        "/FTA Documents/20220603 - HART 2022 Recovery Plan.pdf",
    ),
}

# Doc specs: (slug, output_filename, filename-patterns-for-dynamic-discovery)
# The monthly_progress_report is found dynamically (latest YYYYMM in the
# current/previous year folder); others use _KNOWN_PATHS.
_DOC_SPECS: list[tuple[str, str, list[str]]] = [
    (
        "monthly_progress_report",
        "monthly_progress_report.pdf",
        [r"\d{6}.*monthly\s+progress\s+report.*low\s*res"],
    ),
    (
        "ffga_amended",
        "ffga_amended.pdf",
        [r"amended.*ffga", r"amended.*full\s+funding"],
    ),
    (
        "recovery_plan",
        "recovery_plan.pdf",
        [r"recovery\s+plan"],
    ),
]

MIN_REQUIRED = 2  # monthly_progress_report + ffga_amended are required for step 11


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


def _find_latest_monthly_report(
    token: str,
    nonce: str,
) -> tuple[str, str] | None:
    """Locate the most recent Monthly Progress Report PDF.

    Walks the year-folder browser (REPORTS_SOURCE_ID module): tries the current
    year folder, then the previous year if the current year has no reports yet
    (January edge case). Within a year folder, picks the file whose name has
    the greatest YYYYMM prefix and contains "Monthly Progress Report - low res".

    Returns (dropbox_path, filename) or None if nothing found.
    """
    from datetime import date

    year = date.today().year
    pat_low_res = re.compile(r"(\d{6}).*monthly\s+progress\s+report.*low\s*res",
                              re.IGNORECASE)

    for candidate_year in (year, year - 1):
        folder_path = f"/{candidate_year}/"
        entries = _list_folder(token, folder_path, nonce)
        matches: list[tuple[str, str, str]] = []  # (yyyymm, name, dropbox_path)
        for entry in entries:
            if entry["type"] != "file":
                continue
            name = entry["name"]
            m = pat_low_res.search(name)
            if not m:
                continue
            yyyymm = m.group(1)
            dpath = unquote(entry["url"]) if entry["url"] else f"{folder_path}{name}"
            matches.append((yyyymm, name, dpath))
        if matches:
            matches.sort(reverse=True)  # latest YYYYMM first
            yyyymm, name, dpath = matches[0]
            print(f"  [latest] monthly_progress_report → {name!r} (YYYYMM={yyyymm})")
            return dpath, name

    return None


def _discover_docs(
    modules: list[dict[str, str]],
    nonce: str | None,
) -> dict[str, tuple[str, str, str]]:
    """Return {slug: (download_url, dropbox_path, token)}.

    1. Resolves hardcoded _KNOWN_PATHS (ffga_amended, recovery_plan).
    2. Dynamically discovers the latest monthly_progress_report by listing
       the current-year folder in the year-folder browser module.
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

    # Step 2: dynamic discovery of latest monthly progress report
    reports_token = source_to_token.get(REPORTS_SOURCE_ID)
    if not reports_token:
        print(f"  [warn]  Module source={REPORTS_SOURCE_ID!r} not found in page; "
              "cannot locate monthly_progress_report")
    elif not nonce:
        print("  [warn]  No nonce found in page HTML; cannot list year folders")
    else:
        result = _find_latest_monthly_report(reports_token, nonce)
        if result:
            dpath, _name = result
            url = _download_url(reports_token, dpath)
            found["monthly_progress_report"] = (url, dpath, reports_token)
        else:
            print("  [warn]  No monthly progress report found in current or previous year folder")

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
