"""ArcGIS REST client + Hub dataset-slug resolver.

Two public entry points:

* ``fetch_layer(service_url, layer_id, out_path, where='1=1')`` —
  paginates a FeatureServer ``/query`` endpoint in 2000-row chunks, combines
  the pages into one GeoJSON ``FeatureCollection``, writes it to ``out_path``,
  and returns the dict.

* ``resolve_slug(hub_org_url, slug)`` — looks up a Hub dataset slug
  (e.g. ``cchnl::parcels-tax``) in the org's DCAT-US 1.1 feed and returns
  ``(service_url, layer_id)`` ready to feed to :func:`fetch_layer`.

All HTTP fetches go through :mod:`common.http_client` so retries and timeouts
are uniform with the rest of the pipeline.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from common.http_client import fetch_text


_PAGE_SIZE = 2000

# In-process cache: one DCAT fetch per Hub org per run.
_DCAT_CACHE: dict[str, list[dict[str, Any]]] = {}


def fetch_layer(
    service_url: str,
    layer_id: int,
    out_path: str | Path,
    where: str = "1=1",
    *,
    out_fields: str = "*",
) -> dict[str, Any]:
    """Paginate an ArcGIS FeatureServer / MapServer ``/query`` call.

    Returns the combined ``FeatureCollection`` GeoJSON dict and writes it to
    ``out_path``. ``service_url`` is the FeatureServer/MapServer root (no
    trailing layer id); ``layer_id`` is the integer layer index.
    """
    base = f"{service_url.rstrip('/')}/{int(layer_id)}/query"
    features: list[dict[str, Any]] = []
    crs: dict[str, Any] | None = None
    offset = 0

    while True:
        params = {
            "where":             where,
            "outFields":         out_fields,
            "f":                 "geojson",
            "outSR":             "4326",
            "returnGeometry":    "true",
            "resultOffset":      offset,
            "resultRecordCount": _PAGE_SIZE,
        }
        url = f"{base}?{urlencode(params)}"
        page = json.loads(fetch_text(url))

        if isinstance(page, dict) and "error" in page:
            raise RuntimeError(f"ArcGIS error from {url}: {page['error']}")

        page_feats = page.get("features") or []
        features.extend(page_feats)

        if crs is None and isinstance(page.get("crs"), dict):
            crs = page["crs"]

        # Termination: server says no more, OR we got a short page.
        if not page.get("exceededTransferLimit") and len(page_feats) < _PAGE_SIZE:
            break
        if not page_feats:
            break
        offset += _PAGE_SIZE

    fc: dict[str, Any] = {"type": "FeatureCollection", "features": features}
    if crs is not None:
        fc["crs"] = crs

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(fc))
    return fc


_SERVICE_RE = re.compile(
    r"^(?P<svc>.*?/(?:Feature|Map)Server)/(?P<lyr>\d+)\b",
    re.IGNORECASE,
)


def _split_service_url(access_url: str) -> tuple[str, int]:
    """Pull ``(service_root, layer_id)`` from an Esri REST access URL."""
    cleaned = access_url.split("?", 1)[0].rstrip("/")
    m = _SERVICE_RE.match(cleaned)
    if not m:
        raise ValueError(f"Cannot parse Esri REST URL: {access_url!r}")
    return m.group("svc"), int(m.group("lyr"))


def _load_dcat(hub_org_url: str) -> list[dict[str, Any]]:
    key = hub_org_url.rstrip("/")
    cached = _DCAT_CACHE.get(key)
    if cached is not None:
        return cached
    dcat_url = f"{key}/api/feed/dcat-us/1.1.json"
    catalog = json.loads(fetch_text(dcat_url))
    datasets = catalog.get("dataset") or []
    _DCAT_CACHE[key] = datasets
    return datasets


def _slug_candidates(slug: str) -> list[str]:
    """Return lowercased forms to match against landingPage URLs."""
    s = slug.strip().lower()
    out = [s]
    if "::" in s:
        out.append(s.split("::", 1)[1])
    return out


def _esri_distribution(ds: dict[str, Any]) -> str | None:
    """Return the Esri-REST accessURL from a DCAT dataset, if any."""
    for dist in ds.get("distribution") or []:
        fmt   = (dist.get("format")    or "").lower()
        media = (dist.get("mediaType") or "").lower()
        url   =  dist.get("accessURL") or ""
        if not url:
            continue
        if ("esri rest" in fmt
                or "esri" in media
                or "/FeatureServer/" in url
                or "/MapServer/" in url):
            return url
    return None


def resolve_slug(hub_org_url: str, slug: str) -> tuple[str, int]:
    """Resolve a Hub dataset slug → ``(service_url, layer_id)``.

    *hub_org_url* is the Hub site root (e.g.
    ``https://honolulu-cchnl.opendata.arcgis.com``). *slug* may be qualified
    (``cchnl::parcels-tax``) or unqualified (``parcels-tax``).
    """
    datasets = _load_dcat(hub_org_url)
    candidates = _slug_candidates(slug)

    for ds in datasets:
        landing = (ds.get("landingPage") or "").lower()
        if not landing:
            continue
        tail = landing.rstrip("/").rsplit("/", 1)[-1]
        if any(c == tail or c in landing for c in candidates):
            access = _esri_distribution(ds)
            if access:
                return _split_service_url(access)

    raise LookupError(
        f"Slug {slug!r} not found in DCAT catalog at "
        f"{hub_org_url.rstrip('/')}/api/feed/dcat-us/1.1.json"
    )
