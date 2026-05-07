"""Shared HTTP helper for the ETL scripts.

Ported from Housing-Affordability-Tracker. Provides timeout + retry around
``urllib.request.urlopen`` so a flaky CDN can't silently hang or produce
stale-data builds.

Usage::

    from common.http_client import fetch_bytes, fetch_text

    raw = fetch_bytes("https://example.com/data.csv")
    text = fetch_text("https://example.com/page.html")
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from typing import Any

_DEFAULT_UA      = "Mozilla/5.0 (compatible; Revenue-Modeling-TOD/1.0)"
_DEFAULT_TIMEOUT = 60    # generous for ArcGIS paginated GeoJSON responses
_DEFAULT_RETRIES = 2


def fetch_bytes(
    url: str,
    *,
    headers:  dict[str, str] | None = None,
    data:     bytes | None = None,
    timeout:  int = _DEFAULT_TIMEOUT,
    retries:  int = _DEFAULT_RETRIES,
    backoff:  float = 2.0,
    ssl_ctx:  Any = None,
) -> bytes:
    """Fetch *url* and return the raw response body as bytes.

    Total attempts = retries + 1.  Raises ``urllib.error.URLError`` / ``OSError``
    on final failure.
    """
    req_headers = {"User-Agent": _DEFAULT_UA}
    if headers:
        req_headers.update(headers)

    req = urllib.request.Request(url, data=data, headers=req_headers)

    last_exc: Exception = RuntimeError("no attempts made")
    delay = 1.0
    for attempt in range(retries + 1):
        try:
            kwargs: dict[str, Any] = {"timeout": timeout}
            if ssl_ctx is not None:
                kwargs["context"] = ssl_ctx
            with urllib.request.urlopen(req, **kwargs) as resp:
                return resp.read()
        except (urllib.error.URLError, OSError) as exc:
            last_exc = exc
            if attempt < retries:
                print(f"    [http] {url[:80]}… attempt {attempt+1} failed: {exc}. "
                      f"Retrying in {delay:.0f}s…")
                time.sleep(delay)
                delay *= backoff
    raise last_exc


def fetch_text(url: str, *, encoding: str = "utf-8", **kwargs: Any) -> str:
    """Convenience wrapper around :func:`fetch_bytes` that decodes the response."""
    return fetch_bytes(url, **kwargs).decode(encoding, errors="replace")
