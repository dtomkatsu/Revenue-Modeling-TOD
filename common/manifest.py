"""Manifest helper — every ETL output file gets a sibling ``<name>.manifest.json``
recording where it came from, when it was fetched, and how many rows it has.

Used by ``pipeline_run.py --check`` for freshness audits.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def write_manifest(
    output_path: str | Path,
    *,
    source_url: str,
    row_count: int,
    script: str,
    extras: dict[str, Any] | None = None,
) -> Path:
    """Write a manifest sidecar next to *output_path*.

    Returns the manifest file path.
    """
    output_path = Path(output_path)
    manifest_path = output_path.with_suffix(output_path.suffix + ".manifest.json")
    payload = {
        "output":      str(output_path.name),
        "source_url":  source_url,
        "row_count":   row_count,
        "script":      script,
        "fetched_at":  datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if extras:
        payload.update(extras)
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n")
    return manifest_path


def read_manifest(output_path: str | Path) -> dict[str, Any] | None:
    """Read the manifest for *output_path*, or ``None`` if it doesn't exist."""
    manifest_path = Path(output_path).with_suffix(Path(output_path).suffix + ".manifest.json")
    if not manifest_path.exists():
        return None
    return json.loads(manifest_path.read_text())


def manifest_age_days(output_path: str | Path) -> float | None:
    """Return how many days old the manifest's ``fetched_at`` is, or ``None``
    if no manifest exists."""
    m = read_manifest(output_path)
    if not m:
        return None
    fetched = datetime.fromisoformat(m["fetched_at"])
    return (datetime.now(timezone.utc) - fetched).total_seconds() / 86400
