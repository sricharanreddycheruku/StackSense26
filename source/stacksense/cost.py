"""Monthly cost band from the knowledge-base price anchors."""
from __future__ import annotations

import math

from .model import Constraints, CostBand, Derived, Scored, Selection

AVAILABILITY_FACTOR = {"99": 1.0, "99.9": 1.0, "99.95": 1.5, "99.99": 2.2}
TRAFFIC_MAX_FACTOR = {"spiky": 1.25, "seasonal": 1.15}
SECURITY_BASELINE = (50.0, 300.0)
STAGING_MIN, STAGING_MAX = 1.1, 1.3
MAX_SPREAD = 3.0
CORE_LAYERS = ("hosting", "database", "cache")


def interpolate(anchors, users: float) -> tuple[float, float]:
    """Log-linear interpolation of [MAU, min, max] anchors, clamped at both ends."""
    if not anchors:
        return 0.0, 0.0
    pts = sorted(anchors, key=lambda a: a[0])
    if users <= pts[0][0]:
        return float(pts[0][1]), float(pts[0][2])
    if users >= pts[-1][0]:
        return float(pts[-1][1]), float(pts[-1][2])
    for lo, hi in zip(pts, pts[1:]):
        if lo[0] <= users <= hi[0]:
            t = (math.log10(users) - math.log10(lo[0])) / (math.log10(hi[0]) - math.log10(lo[0]))
            return (lo[1] + t * (hi[1] - lo[1]), lo[2] + t * (hi[2] - lo[2]))
    return float(pts[-1][1]), float(pts[-1][2])


def item_cost(entry: dict, off: dict | None, users: float, commercial: bool) -> tuple[float, float]:
    src = off if off is not None else entry
    lo, hi = interpolate(src.get("cost_anchors") or [], users)
    free = (off or {}).get("free_tier") or {}
    if commercial and free.get("available") and free.get("commercial_use") is False:
        lo = max(lo, hi * 0.5)
    return round(lo, 2), round(hi, 2)


def gb_rate(off: dict | None, deployment: str) -> float:
    """Per-GB monthly storage price; disks of a self-run database on the organisation's own servers are not billed monthly."""
    rate = float((off or {}).get("per_gb_month") or 0)
    if (off or {}).get("kind") == "self" and deployment in ("on_premise", "air_gapped"):
        return 0.0
    return rate


def _nice(value: float, up: bool) -> float:
    step = 5 if value < 100 else 10 if value < 1000 else 50
    fn = math.ceil if up else math.floor
    return float(fn(value / step) * step)


def _free_note(s: Scored) -> str | None:
    if s.bundled_in:
        return f"{s.name}: included in the {s.bundled_in} price, with no separate charge"
    free = ((s.off or {}).get("free_tier") or {})
    if free.get("available") and s.cost[0] == 0 and (s.off or {}).get("kind") == "self":
        return f"{s.name}: no licence fee ({free.get('limits', 'open-source software')})"
    if free.get("available") and s.cost[0] == 0:
        return f"{s.name}: free tier used at the low end ({free.get('limits', 'see provider')})"
    return None


def estimate(sel: Selection, spec: dict, d: Derived, c: Constraints, kb=None) -> CostBand:
    vol = float((spec.get("scale") or {}).get("data_volume_gb_12_months") or 0)
    team = (spec.get("team") or {}).get("size", 1)
    req = spec.get("requirements") or {}
    avail = str(req.get("availability", "99.9"))
    traffic = (spec.get("scale") or {}).get("traffic_pattern", "steady")

    rows: list[tuple[str, Scored, float, float, str]] = []
    for pick in (sel.frontend, sel.backend, sel.database, sel.hosting, *sel.supporting):
        parts = pick.parts if pick.parts else ([pick.chosen] if pick.chosen else [])
        for s in parts:
            if s is None:
                continue
            rows.append((pick.layer, s, s.cost[0], s.cost[1], s.name))
        if pick.layer == "database" and pick.chosen and gb_rate(pick.chosen.off, c.deployment):
            g = round(vol * gb_rate(pick.chosen.off, c.deployment), 2)
            rows.append(("database", pick.chosen, g, g, f"{pick.chosen.name} (storage)"))
        if pick.layer == "storage" and pick.chosen and gb_rate(pick.chosen.off, c.deployment):
            g = round(0.6 * vol * gb_rate(pick.chosen.off, c.deployment), 2)
            rows.append(("storage", pick.chosen, g, g, f"{pick.chosen.name} (storage)"))

    rps = (spec.get("scale") or {}).get("peak_rps")
    sized = 0
    host = sel.hosting
    api = host.parts[-1] if host.parts else host.chosen
    if kb is not None and rps and api and sel.backend.chosen \
            and (api.entry or {}).get("sub_layer") not in ("static", "baas_platform", "serverless"):
        sizing = kb.sizing
        sized = math.ceil(float(rps) / sizing["rps_per_instance"])
        extra = sized - 1
        if extra > 0:
            lo_i, hi_i = sizing["instance_usd_month"]
            rows.append(("hosting", api, extra * lo_i, extra * hi_i, f"{api.name} (sizing: {sized:,} instances)"))

    amult = AVAILABILITY_FACTOR.get(avail, 1.0)
    tmax = TRAFFIC_MAX_FACTOR.get(traffic, 1.0)
    nonprod = team >= 3
    lo_total = hi_total = 0.0
    breakdown: list[dict] = []
    seen: set[str] = set()
    for layer, s, lo, hi, label in rows:
        if layer not in ("database", "storage") or lo != hi:
            if label in seen:
                continue
            seen.add(label)
        core = layer in CORE_LAYERS
        if core:
            # peak allowances overlap, so the larger one applies rather than their product
            lo, hi = lo * amult, hi * amult * max(tmax, STAGING_MAX if nonprod else 1.0)
            if nonprod:
                lo *= STAGING_MIN
        elif nonprod and layer == "backend":
            lo, hi = lo * STAGING_MIN, hi * STAGING_MAX
        lo_total += lo
        hi_total += hi
        breakdown.append({"layer": layer, "component": label, "min": round(lo, 2), "max": round(hi, 2)})

    sec_lo = sec_hi = 0.0
    if {"pci_dss", "hipaa", "rbi_localisation"} & set(spec.get("compliance") or []):
        sec_lo, sec_hi = SECURITY_BASELINE
        lo_total += sec_lo
        hi_total += sec_hi
        breakdown.append({"layer": "security", "component": "security baseline (KMS, WAF, audit log)",
                          "min": sec_lo, "max": sec_hi})

    capped = lo_total > 0 and hi_total > MAX_SPREAD * lo_total
    if capped:
        hi_total = MAX_SPREAD * lo_total
    lo_n, hi_n = _nice(lo_total, False), _nice(hi_total, True)
    if hi_n < lo_n:
        hi_n = lo_n

    a: list[str] = [
        f"Priced for {d.users_peak:,} monthly active users (the larger of users_at_launch and "
        f"users_12_months), interpolated between list-price anchors in the knowledge base.",
        "Prices are list prices of the cheapest region inside the chosen residency zone; taxes are excluded.",
    ]
    if sized > 1:
        a.append(f"scale.peak_rps is {float(rps):,.0f}, so the API host is sized at {sized:,} instances of about "
                 f"{kb.sizing['rps_per_instance']} requests per second each, at ${kb.sizing['instance_usd_month'][0]}-"
                 f"{kb.sizing['instance_usd_month'][1]} per instance a month; the database is not resized for it.")
    if vol:
        a.append(f"Database storage is priced for {vol:g} GB (scale.data_volume_gb_12_months).")
    if amult > 1:
        a.append(f"Hosting, database and cache are multiplied by {amult:g} for the {avail}% availability target "
                 "(standby capacity).")
    if tmax > 1:
        a.append(f"The upper bound includes a x{tmax:g} allowance for {traffic} traffic (not stacked on the staging allowance).")
    if nonprod:
        a.append("A staging environment is added for a team of 3 or more (x1.1 to x1.3 on core services).")
    if capped:
        a.append(f"The upper bound is limited to {MAX_SPREAD:g} times the lower bound: the components do not all "
                 "peak in the same month, so the band stays within one order of magnitude.")
    if sec_hi:
        a.append(f"A security baseline of ${sec_lo:g}-{sec_hi:g} is added for the compliance scope "
                 "(key management, firewall, audit logs).")
    for _, s, _, _, _ in rows:
        note = _free_note(s)
        if note and note not in a:
            a.append(note)
    a.append("Payment gateway fees (about 2% per transaction), one-time costs, domain names and developer "
             "seats are not infrastructure and are excluded.")
    return CostBand(lo_n, hi_n, a, breakdown)
