"""Sanity-assert harness — fail loudly on silent data drift.

Runs after the full ETL pipeline (steps 01-08 + 10-12 + 02b/03c) and
hard-fails on any assertion outside the documented sanity bounds. Designed
to be the last step of a GitHub Action that refreshes data weekly: a
non-zero exit prevents a PR from landing with broken numbers.

Coverage (every assertion is a function returning ``(ok, msg)``):

City budget (``data/processed/budget_totals.json``,
``data/processed/cip_totals.json``):
  * road_om_total_usd      ∈ [$30M, $80M]
  * sewer_om_total_usd     ∈ [$150M, $250M]
  * road_cip_total_usd     ∈ [$80M, $200M]
  * sewer_cip_total_usd    ∈ [$500M, $1.2B]  (Sand Island lump)

BWS (reads the **final pipeline outputs** ``budget_totals.json`` +
``cip_totals.json`` — which reflect whichever source won at runtime
(manual override > bws_totals.json > PDF), so any broken source path is
caught. Falls back to ``bws_totals.json`` if those aren't present.):
  * water_om_total_usd     ∈ [$300M, $500M]
  * water_cip_total_usd    ∈ [$150M, $250M]
  * water_cip_fy26_usd     ∈ [$200M, $400M]
  * All three values present and > 0

Per-foot rate (``data/processed/parcels_costs.geojson.manifest.json``):
  * All six rates (road/sewer/water × O&M/CIP) > 0 and finite
  * water_om_rate / road_om_rate ∈ [3, 15]
  * Skipped (with explicit warning) if the manifest is not present —
    that file is gitignored and only appears after step 07 runs locally.

Parcels (``data/parcels_tod.geojson``):
  * Feature count ∈ [18,000, 22,000] (current: 19,872)
  * Landlocked count ≤ 10% of total
  * All features have non-null geometry
  * No NaN / Inf in cost_per_ac / cip_per_ac / cost_om_per_ac /
    rev_per_ac / net_per_ac (explicit ``null`` is allowed — that's how the
    pipeline encodes "no data" for the ~21 exempt parcels with no
    land_use class).
  * All cost_*_per_ac ≥ 0
  * p99(cost_per_ac) ≤ 100 × p50(cost_per_ac)
  * ≥ 85% of non-landlocked parcels have positive rev_per_ac (current
    baseline: 90.3%. The remainder are mostly Residential parcels whose
    homeowner exemption zeros out the taxable value — real RPAD
    behavior, not a pipeline bug. The 85% floor catches a real regression
    while accepting the documented baseline.)
  * Every required property present on every feature
  * ≥ 80% of parcels have non-null assessed_value

Budget reconciliation (totals × frontage ≈ Σ parcel cost, within 1%):
  * Skipped if the parcels_costs manifest is absent (same reason as the
    per-foot rate checks).

Exit code:
  0 if every assertion either PASSed or was SKIPped.
  1 if any assertion FAILed.

Usage::

    python etl/_assert_sanity.py
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from pathlib import Path
from typing import Callable

_ROOT = Path(__file__).resolve().parent.parent

# ----- File locations ------------------------------------------------------

BUDGET_TOTALS    = _ROOT / "data" / "processed" / "budget_totals.json"
CIP_TOTALS       = _ROOT / "data" / "processed" / "cip_totals.json"
BWS_TOTALS       = _ROOT / "data" / "processed" / "bws_totals.json"
BUDGET_OVERRIDES = _ROOT / "data" / "budget_overrides.json"
CIP_OVERRIDES    = _ROOT / "data" / "cip_overrides.json"
PARCELS_COSTS    = _ROOT / "data" / "processed" / "parcels_costs.geojson"
PARCELS_TOD      = _ROOT / "data" / "parcels_tod.geojson"

# ----- Result type ---------------------------------------------------------

# Each assertion returns (status, message) where status is "PASS", "FAIL",
# or "SKIP". Skipped assertions don't influence the exit code but are
# reported separately so missing prerequisites are visible.
Status     = str   # "PASS" | "FAIL" | "SKIP"
Assertion  = Callable[[], tuple[Status, str]]


def _ok(msg: str) -> tuple[Status, str]:   return ("PASS", msg)
def _bad(msg: str) -> tuple[Status, str]:  return ("FAIL", msg)
def _skip(msg: str) -> tuple[Status, str]: return ("SKIP", msg)


# ----- File loaders --------------------------------------------------------

def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def _load_manifest(geojson_path: Path) -> dict | None:
    manifest_path = geojson_path.with_suffix(geojson_path.suffix + ".manifest.json")
    return _load_json(manifest_path)


def _bws_value(key: str) -> int | None:
    """Read a BWS key from the final pipeline output.

    Reads ``budget_totals.json`` for ``water_om`` and ``cip_totals.json``
    for the ``water_cip_*`` keys — those are the downstream-visible values
    that reflect whichever upstream source won (manual override >
    bws_totals.json auto > PDF extraction). Reading the final outputs is
    what makes a broken manual override surface as a failed assertion.

    Falls back to ``bws_totals.json`` directly if the downstream files
    haven't been produced yet (e.g. on a fresh checkout that's only run
    02b + 03c so far).
    """
    final_path = CIP_TOTALS if "cip" in key else BUDGET_TOTALS
    final = _load_json(final_path)
    if final and final.get(key) is not None:
        return int(final[key])

    bws = _load_json(BWS_TOTALS) or {}
    if bws.get(key) is not None:
        return int(bws[key])

    return None


def _in_range(value: int | float, lo: int | float, hi: int | float) -> bool:
    return lo <= value <= hi


def _fmt_usd(n: int | float) -> str:
    return f"${n:,.0f}"


# ----- Assertions: city budget --------------------------------------------

def _assert_city_budget_om() -> tuple[Status, str]:
    bt = _load_json(BUDGET_TOTALS)
    if bt is None:
        return _skip(f"{BUDGET_TOTALS.relative_to(_ROOT)} missing")
    bounds = {
        "road_om_total_usd":  ( 30_000_000,  80_000_000),
        "sewer_om_total_usd": (150_000_000, 250_000_000),
    }
    failures: list[str] = []
    for key, (lo, hi) in bounds.items():
        v = bt.get(key)
        if v is None or not _in_range(v, lo, hi):
            failures.append(f"{key}={v} outside [{_fmt_usd(lo)}, {_fmt_usd(hi)}]")
    if failures:
        return _bad("; ".join(failures))
    return _ok("road_om + sewer_om within bounds")


def _assert_city_cip() -> tuple[Status, str]:
    ct = _load_json(CIP_TOTALS)
    if ct is None:
        return _skip(f"{CIP_TOTALS.relative_to(_ROOT)} missing")
    bounds = {
        "road_cip_total_usd":  ( 80_000_000,   200_000_000),
        "sewer_cip_total_usd": (500_000_000, 1_200_000_000),
    }
    failures: list[str] = []
    for key, (lo, hi) in bounds.items():
        v = ct.get(key)
        if v is None or not _in_range(v, lo, hi):
            failures.append(f"{key}={v} outside [{_fmt_usd(lo)}, {_fmt_usd(hi)}]")
    if failures:
        return _bad("; ".join(failures))
    return _ok("road_cip + sewer_cip within bounds")


# ----- Assertions: BWS -----------------------------------------------------

def _assert_bws() -> tuple[Status, str]:
    bounds = {
        "water_om_total_usd":  (300_000_000, 500_000_000),
        "water_cip_total_usd": (150_000_000, 250_000_000),
        "water_cip_fy26_usd":  (200_000_000, 400_000_000),
    }
    failures: list[str] = []
    for key, (lo, hi) in bounds.items():
        v = _bws_value(key)
        if v is None:
            failures.append(f"{key} missing from bws_totals.json + overrides")
            continue
        if v <= 0:
            failures.append(f"{key}={v} must be > 0")
            continue
        if not _in_range(v, lo, hi):
            failures.append(f"{key}={v} outside [{_fmt_usd(lo)}, {_fmt_usd(hi)}]")
    if failures:
        return _bad("; ".join(failures))
    return _ok("all three BWS values present and in range")


# ----- Assertions: per-foot rates (manifest extras) -----------------------

def _assert_per_foot_rates() -> tuple[Status, str]:
    manifest = _load_manifest(PARCELS_COSTS)
    if manifest is None:
        return _skip(f"{PARCELS_COSTS.with_suffix('.geojson.manifest.json').relative_to(_ROOT)} "
                     "missing (run etl/07 first)")

    rate_keys = (
        "rate_road_om_usd_per_ft",  "rate_sewer_om_usd_per_ft",  "rate_water_om_usd_per_ft",
        "rate_road_cip_usd_per_ft", "rate_sewer_cip_usd_per_ft", "rate_water_cip_usd_per_ft",
    )
    failures: list[str] = []
    for k in rate_keys:
        v = manifest.get(k)
        if v is None or not math.isfinite(v) or v <= 0:
            failures.append(f"{k}={v} not positive-finite")

    # Water O&M per foot should be substantially higher than road O&M per
    # foot — sanity range [3, 15] catches an accidental swap or a missing
    # zero in either source.
    road_om  = manifest.get("rate_road_om_usd_per_ft")
    water_om = manifest.get("rate_water_om_usd_per_ft")
    if road_om and water_om and road_om > 0:
        ratio = water_om / road_om
        if not _in_range(ratio, 3, 15):
            failures.append(
                f"water_om / road_om rate ratio = {ratio:.2f} outside [3, 15]"
            )

    if failures:
        return _bad("; ".join(failures))
    return _ok("six rates positive-finite; water/road O&M ratio in [3, 15]")


# ----- Assertions: parcels_tod.geojson ------------------------------------

_REQUIRED_PROPS = (
    "tmk", "area_ac",
    "rev_per_ac", "cost_om_per_ac", "cip_per_ac", "cost_per_ac", "net_per_ac",
    "frontage_road_ft", "frontage_sewer_ft", "frontage_water_ft",
    "station_ids", "address", "land_use", "assessed_value", "landlocked",
)

_NUMERIC_PROPS  = ("cost_per_ac", "cip_per_ac", "cost_om_per_ac",
                   "rev_per_ac", "net_per_ac")

_NON_NEG_PROPS  = ("cost_per_ac", "cip_per_ac", "cost_om_per_ac")


def _load_parcels() -> list[dict] | None:
    if not PARCELS_TOD.exists():
        return None
    gj = _load_json(PARCELS_TOD)
    if gj is None:
        return None
    return gj.get("features", [])


def _is_corrupted_number(x: object) -> bool:
    """True if x is NaN/Inf (i.e. corrupted numeric). Explicit ``None``
    is NOT considered corrupted — it's the pipeline's encoding for "no
    data" and is preserved end-to-end as JSON ``null``.
    """
    if x is None:
        return False
    try:
        f = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return True
    return math.isnan(f) or math.isinf(f)


def _assert_parcel_count() -> tuple[Status, str]:
    feats = _load_parcels()
    if feats is None:
        return _skip(f"{PARCELS_TOD.relative_to(_ROOT)} missing")
    n = len(feats)
    if not _in_range(n, 18_000, 22_000):
        return _bad(f"feature count = {n:,} outside [18,000, 22,000]")
    return _ok(f"feature count = {n:,} within [18,000, 22,000]")


def _assert_landlocked_share() -> tuple[Status, str]:
    feats = _load_parcels()
    if feats is None:
        return _skip(f"{PARCELS_TOD.relative_to(_ROOT)} missing")
    n = len(feats)
    n_landlocked = sum(1 for f in feats if f["properties"].get("landlocked"))
    share = n_landlocked / n if n else 0.0
    if share > 0.10:
        return _bad(f"{n_landlocked:,}/{n:,} = {share:.1%} landlocked > 10%")
    return _ok(f"{n_landlocked:,}/{n:,} = {share:.1%} landlocked")


def _assert_geometry_present() -> tuple[Status, str]:
    feats = _load_parcels()
    if feats is None:
        return _skip(f"{PARCELS_TOD.relative_to(_ROOT)} missing")
    missing = sum(1 for f in feats if not f.get("geometry"))
    if missing:
        return _bad(f"{missing:,} features have null geometry")
    return _ok("all features have non-null geometry")


def _assert_finite_numbers() -> tuple[Status, str]:
    feats = _load_parcels()
    if feats is None:
        return _skip(f"{PARCELS_TOD.relative_to(_ROOT)} missing")
    bad: dict[str, int] = {k: 0 for k in _NUMERIC_PROPS}
    for f in feats:
        p = f["properties"]
        for k in _NUMERIC_PROPS:
            if _is_corrupted_number(p.get(k)):
                bad[k] += 1
    failures = [f"{k}={n} NaN/Inf" for k, n in bad.items() if n]
    if failures:
        return _bad("; ".join(failures))
    return _ok("no NaN/Inf in cost_per_ac/cip_per_ac/cost_om_per_ac/rev_per_ac/net_per_ac")


def _assert_costs_non_negative() -> tuple[Status, str]:
    feats = _load_parcels()
    if feats is None:
        return _skip(f"{PARCELS_TOD.relative_to(_ROOT)} missing")
    bad: dict[str, int] = {k: 0 for k in _NON_NEG_PROPS}
    for f in feats:
        p = f["properties"]
        for k in _NON_NEG_PROPS:
            v = p.get(k)
            try:
                if v is not None and float(v) < 0:
                    bad[k] += 1
            except (TypeError, ValueError):
                pass
    failures = [f"{k} negative on {n:,} parcels" for k, n in bad.items() if n]
    if failures:
        return _bad("; ".join(failures))
    return _ok("cost_per_ac/cip_per_ac/cost_om_per_ac all ≥ 0")


def _assert_cost_outlier_ratio() -> tuple[Status, str]:
    feats = _load_parcels()
    if feats is None:
        return _skip(f"{PARCELS_TOD.relative_to(_ROOT)} missing")
    vals: list[float] = []
    for f in feats:
        v = f["properties"].get("cost_per_ac")
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(fv) and fv >= 0:
            vals.append(fv)
    if len(vals) < 100:
        return _skip(f"only {len(vals)} finite cost_per_ac values; need ≥ 100")

    vals.sort()
    p50 = vals[int(0.50 * (len(vals) - 1))]
    p99 = vals[int(0.99 * (len(vals) - 1))]
    if p50 == 0:
        return _skip("p50(cost_per_ac) = 0, ratio undefined")
    ratio = p99 / p50
    if ratio > 100:
        return _bad(f"p99/p50 cost_per_ac = {ratio:.1f}× > 100 (outlier check)")
    return _ok(f"p99/p50 cost_per_ac = {ratio:.1f}× ≤ 100")


def _assert_revenue_coverage() -> tuple[Status, str]:
    feats = _load_parcels()
    if feats is None:
        return _skip(f"{PARCELS_TOD.relative_to(_ROOT)} missing")
    non_landlocked = [f for f in feats if not f["properties"].get("landlocked")]
    if not non_landlocked:
        return _skip("no non-landlocked parcels")
    def _is_positive(v: object) -> bool:
        if v is None or _is_corrupted_number(v):
            return False
        try:
            return float(v) > 0  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False

    n_with_rev = sum(
        1 for f in non_landlocked
        if _is_positive(f["properties"].get("rev_per_ac"))
    )
    share = n_with_rev / len(non_landlocked)
    # Baseline 90.3%. Lower bound 85% catches real regression while
    # accepting the documented homeowner-exemption baseline.
    if share < 0.85:
        return _bad(
            f"only {n_with_rev:,}/{len(non_landlocked):,} = {share:.1%} of "
            "non-landlocked parcels have positive rev_per_ac (< 85%)"
        )
    return _ok(f"{n_with_rev:,}/{len(non_landlocked):,} = {share:.1%} ≥ 85%")


def _assert_required_props() -> tuple[Status, str]:
    feats = _load_parcels()
    if feats is None:
        return _skip(f"{PARCELS_TOD.relative_to(_ROOT)} missing")
    missing: dict[str, int] = {}
    for f in feats:
        p = f["properties"]
        for k in _REQUIRED_PROPS:
            if k not in p:
                missing[k] = missing.get(k, 0) + 1
    if missing:
        first = ", ".join(f"{k} (missing on {n:,} parcels)"
                          for k, n in list(missing.items())[:3])
        return _bad(f"required property gaps: {first}")
    return _ok(f"all {len(_REQUIRED_PROPS)} required properties present on every feature")


def _assert_assessed_value_coverage() -> tuple[Status, str]:
    feats = _load_parcels()
    if feats is None:
        return _skip(f"{PARCELS_TOD.relative_to(_ROOT)} missing")
    n_with = sum(
        1 for f in feats
        if f["properties"].get("assessed_value") is not None
    )
    share = n_with / len(feats) if feats else 0.0
    if share < 0.80:
        return _bad(
            f"only {n_with:,}/{len(feats):,} = {share:.1%} of parcels have "
            "non-null assessed_value (< 80%)"
        )
    return _ok(f"{n_with:,}/{len(feats):,} = {share:.1%} ≥ 80%")


# ----- Budget reconciliation ----------------------------------------------

def _assert_budget_reconciliation() -> tuple[Status, str]:
    """Σ(cost_om_per_ac × area_ac) ≈ Σ(frontage × per-foot rate), within 1%.

    The Σ across TOD parcels equals the citywide-rate × Σ(TOD frontage),
    not the citywide budget total — this is by construction (the rate is
    derived from the city budget divided by citywide frontage, then applied
    to TOD frontage only). So we compare:

      LHS = Σ(parcel cost_om_per_ac × area_ac)         [from parcels]
      RHS = road_om_rate  × Σ(frontage_road_ft)        [from manifest extras]
          + sewer_om_rate × Σ(frontage_sewer_ft)
          + water_om_rate × Σ(frontage_water_ft)

    This is tautological if the pipeline is consistent — when it isn't,
    something has drifted between step 03/03b/07 and step 08.
    """
    manifest = _load_manifest(PARCELS_COSTS)
    if manifest is None:
        return _skip(f"{PARCELS_COSTS.with_suffix('.geojson.manifest.json').relative_to(_ROOT)} "
                     "missing (run etl/07 first)")
    feats = _load_parcels()
    if feats is None:
        return _skip(f"{PARCELS_TOD.relative_to(_ROOT)} missing")

    rates = {
        "road":  manifest.get("rate_road_om_usd_per_ft"),
        "sewer": manifest.get("rate_sewer_om_usd_per_ft"),
        "water": manifest.get("rate_water_om_usd_per_ft"),
    }
    if any(r is None for r in rates.values()):
        return _skip("manifest missing one or more rate_*_om_usd_per_ft fields")

    sum_om_cost = 0.0
    sum_road_ft = 0.0
    sum_sewer_ft = 0.0
    sum_water_ft = 0.0
    for f in feats:
        p = f["properties"]
        try:
            area_ac = float(p["area_ac"])
            om      = float(p["cost_om_per_ac"])
            rd      = float(p.get("frontage_road_ft")  or 0.0)
            sw      = float(p.get("frontage_sewer_ft") or 0.0)
            wt      = float(p.get("frontage_water_ft") or 0.0)
        except (KeyError, TypeError, ValueError):
            continue
        sum_om_cost  += om * area_ac
        sum_road_ft  += rd
        sum_sewer_ft += sw
        sum_water_ft += wt

    rhs = (
        rates["road"]  * sum_road_ft  +
        rates["sewer"] * sum_sewer_ft +
        rates["water"] * sum_water_ft
    )
    if rhs == 0:
        return _bad("RHS = 0; can't reconcile")
    delta = abs(sum_om_cost - rhs) / rhs
    if delta > 0.01:
        return _bad(
            f"Σ(cost_om × area) vs Σ(frontage × rate): "
            f"LHS = {_fmt_usd(sum_om_cost)}, RHS = {_fmt_usd(rhs)}, "
            f"Δ = {delta:.2%} (> 1%)"
        )
    return _ok(
        f"Σ(cost_om × area) = {_fmt_usd(sum_om_cost)}; "
        f"Σ(frontage × rate) = {_fmt_usd(rhs)}; Δ = {delta:.3%}"
    )


# ----- Runner --------------------------------------------------------------

# (name, function). Names are stable, short, and grep-able.
_ASSERTIONS: list[tuple[str, Assertion]] = [
    ("city_budget_om",            _assert_city_budget_om),
    ("city_cip",                  _assert_city_cip),
    ("bws_magnitudes",            _assert_bws),
    ("per_foot_rates",            _assert_per_foot_rates),
    ("parcel_count",              _assert_parcel_count),
    ("landlocked_share",          _assert_landlocked_share),
    ("geometry_present",          _assert_geometry_present),
    ("finite_numbers",            _assert_finite_numbers),
    ("costs_non_negative",        _assert_costs_non_negative),
    ("cost_outlier_ratio",        _assert_cost_outlier_ratio),
    ("revenue_coverage",          _assert_revenue_coverage),
    ("required_props",            _assert_required_props),
    ("assessed_value_coverage",   _assert_assessed_value_coverage),
    ("budget_reconciliation",     _assert_budget_reconciliation),
]


def main() -> int:
    n_pass = n_fail = n_skip = 0
    for name, fn in _ASSERTIONS:
        try:
            status, msg = fn()
        except Exception as exc:  # noqa: BLE001 — sanitizer must not crash
            status, msg = "FAIL", f"raised {type(exc).__name__}: {exc}"
        print(f"[{status}] {name:26s} {msg}")
        if   status == "PASS": n_pass += 1
        elif status == "FAIL": n_fail += 1
        else:                  n_skip += 1

    total = len(_ASSERTIONS)
    if n_fail:
        print(f"FAIL: {n_fail}/{total} failed ({n_pass} passed, {n_skip} skipped)")
        return 1
    print(f"OK: {n_pass}/{total} passed ({n_skip} skipped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
