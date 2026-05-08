"""Step 09 — build self-hosted vector basemap tiles for Hawaii.

Downloads the Geofabrik Hawaii OSM extract, runs Planetiler with the default
OpenMapTiles schema, and writes ``data/honolulu_basemap.pmtiles`` — a single
file the static site serves directly. The frontend's MapLibre style points at
this via the ``pmtiles://`` protocol shim.

We bake the result instead of using OpenFreeMap's CDN because (1) tessellation
lag on first pan/zoom is annoying with CDN tiles, (2) future transportation
overlays will want to repaint roads/landcover and that requires owning the
basemap. See METHODOLOGY §Basemap.

Hard requires Java 21+ on the build host (Planetiler is a Java tool). On
macOS: ``brew install openjdk@21``. On other systems install OpenJDK 21
from your package manager. The script checks the Java version and exits
with install instructions if missing/too old.

Idempotent: skipped if the .pmtiles and its manifest already exist. Pass
``--force`` to rebuild. Pass ``--bbox`` to clip to a sub-extent (useful if
the full-Hawaii build comes in too large to commit).

Usage::

    python etl/09_build_basemap_tiles.py
    python etl/09_build_basemap_tiles.py --force
    python etl/09_build_basemap_tiles.py --bbox=-160.3,18.9,-154.7,22.3
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common.http_client import fetch_bytes  # noqa: E402
from common.manifest     import write_manifest  # noqa: E402


SCRIPT_NAME = "etl/09_build_basemap_tiles.py"

# Geofabrik publishes a daily OSM extract for Hawaii at this stable URL.
OSM_URL = "https://download.geofabrik.de/north-america/us/hawaii-latest.osm.pbf"
OSM_PATH = _ROOT / "data" / "raw" / "osm" / "hawaii-latest.osm.pbf"

# Planetiler v0.10.2 — pinned for reproducibility. JAR is ~93 MB. Bump
# carefully: a new major can change CLI flags and OpenMapTiles schema.
PLANETILER_VERSION = "0.10.2"
PLANETILER_URL = (
    f"https://github.com/onthegomap/planetiler/releases/download/"
    f"v{PLANETILER_VERSION}/planetiler.jar"
)
PLANETILER_JAR = _ROOT / "data" / "cache" / "planetiler.jar"

# Output lives at the top of data/ so it's committed to git and served by
# the static frontend (same convention as parcels_tod.geojson).
OUTPUT_PMTILES = _ROOT / "data" / "honolulu_basemap.pmtiles"

JAVA_MIN_MAJOR = 21


def _check_java() -> str:
    """Return the Java version string, or exit with install instructions."""
    try:
        proc = subprocess.run(
            ["java", "-version"], capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        print(
            "[error] `java` not found on PATH. Planetiler requires Java 21+.\n"
            "        macOS:   brew install openjdk@21 && \\\n"
            "                 sudo ln -sfn $(brew --prefix)/opt/openjdk@21/libexec/openjdk.jdk \\\n"
            "                              /Library/Java/JavaVirtualMachines/openjdk-21.jdk\n"
            "        Linux:   sudo apt-get install openjdk-21-jdk\n",
            file=sys.stderr,
        )
        sys.exit(2)

    # `java -version` prints to stderr.
    txt = proc.stderr or proc.stdout
    m = re.search(r'version "(\d+)(?:\.(\d+))?', txt)
    if not m:
        print(f"[error] could not parse Java version from: {txt!r}", file=sys.stderr)
        sys.exit(2)
    major = int(m.group(1))
    if major < JAVA_MIN_MAJOR:
        print(
            f"[error] Java {major} found, but Planetiler needs >= {JAVA_MIN_MAJOR}.\n"
            f"        macOS: brew install openjdk@21\n",
            file=sys.stderr,
        )
        sys.exit(2)
    return txt.strip().splitlines()[0]


def _download_if_missing(url: str, path: Path, label: str) -> None:
    if path.exists() and path.stat().st_size > 0:
        size_mb = path.stat().st_size / 1_048_576
        print(f"[skip] {label} (cached, {size_mb:.1f} MiB)")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[fetch] {label} <- {url}")
    body = fetch_bytes(url, timeout=600)  # JAR is ~93 MB; PBF ~24 MB
    path.write_bytes(body)
    print(f"[done] {label} ({len(body)/1_048_576:.1f} MiB)")


def _read_pbf_timestamp(pbf: Path) -> str | None:
    """Best-effort extraction of OSM snapshot timestamp from PBF header.

    Geofabrik embeds a ``osmosis_replication_timestamp`` (ISO-8601) string in
    the PBF header block. Parsing the protobuf properly needs a dependency
    we don't have, so we just scan the first few KB for the marker.
    """
    try:
        head = pbf.read_bytes()[:4096]
    except OSError:
        return None
    m = re.search(rb'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z?)', head)
    return m.group(1).decode() if m else None


def _build_planetiler_cmd(
    *, jar: Path, osm: Path, output: Path, bbox: str | None,
) -> list[str]:
    cmd = [
        "java",
        "-Xmx4g",
        "-jar", str(jar),
        f"--osm_path={osm}",
        f"--output={output}",
        "--force",  # planetiler refuses to overwrite by default
        # OpenMapTiles needs auxiliary sources (lake_centerlines,
        # water_polygons, natural_earth). Let Planetiler fetch any that
        # aren't already cached under data/sources/.
        "--download",
    ]
    if bbox:
        # Planetiler's --bounds takes minlon,minlat,maxlon,maxlat
        cmd.append(f"--bounds={bbox}")
    return cmd


def build(*, force: bool, bbox: str | None) -> int:
    manifest_path = OUTPUT_PMTILES.with_suffix(
        OUTPUT_PMTILES.suffix + ".manifest.json"
    )
    if not force and OUTPUT_PMTILES.exists() and manifest_path.exists():
        size_mb = OUTPUT_PMTILES.stat().st_size / 1_048_576
        print(f"[skip] {OUTPUT_PMTILES.name} (cached, {size_mb:.1f} MiB). "
              "Pass --force to rebuild.")
        return 0

    java_version = _check_java()
    print(f"[java] {java_version}")

    _download_if_missing(OSM_URL,        OSM_PATH,       "hawaii-latest.osm.pbf")
    _download_if_missing(PLANETILER_URL, PLANETILER_JAR, f"planetiler {PLANETILER_VERSION}")

    osm_snapshot = _read_pbf_timestamp(OSM_PATH)
    print(f"[osm]  snapshot: {osm_snapshot or 'unknown'}")

    OUTPUT_PMTILES.parent.mkdir(parents=True, exist_ok=True)
    cmd = _build_planetiler_cmd(
        jar=PLANETILER_JAR, osm=OSM_PATH, output=OUTPUT_PMTILES, bbox=bbox,
    )
    print(f"[run]  {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=_ROOT)
    if proc.returncode != 0:
        print(f"[error] planetiler exited {proc.returncode}", file=sys.stderr)
        return proc.returncode

    if not OUTPUT_PMTILES.exists():
        print(f"[error] planetiler completed but {OUTPUT_PMTILES} is missing",
              file=sys.stderr)
        return 1

    byte_size = OUTPUT_PMTILES.stat().st_size
    write_manifest(
        OUTPUT_PMTILES,
        source_url=OSM_URL,
        row_count=0,  # not row-shaped; use byte_size instead
        script=SCRIPT_NAME,
        extras={
            "osm_snapshot":      osm_snapshot,
            "planetiler_version": PLANETILER_VERSION,
            "byte_size":         byte_size,
            "bbox":              bbox or "full-extract",
            "schema":            "openmaptiles",
        },
    )
    print(f"[done] {OUTPUT_PMTILES.relative_to(_ROOT)} "
          f"({byte_size/1_048_576:.1f} MiB)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    ap.add_argument("--force", action="store_true",
                    help="Rebuild even if the cache exists.")
    ap.add_argument("--bbox", default=None, metavar="MINLON,MINLAT,MAXLON,MAXLAT",
                    help="Clip output to a bounding box "
                         "(e.g. -160.3,18.9,-154.7,22.3 for inhabited islands). "
                         "Use this if the full-Hawaii build is too large.")
    args = ap.parse_args(argv)
    return build(force=args.force, bbox=args.bbox)


if __name__ == "__main__":
    sys.exit(main())
