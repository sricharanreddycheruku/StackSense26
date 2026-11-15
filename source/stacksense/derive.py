"""Validated spec -> Derived features (tiers, zone, effort, flags, effective weights)."""
from __future__ import annotations

import math
import re
from dataclasses import replace

from . import describe
from .model import Derived, Signal, Validated

_BACKEND_TYPES = frozenset({
    "web_app", "saas_b2b", "ecommerce", "marketplace", "social", "realtime_collab", "fintech",
    "healthtech", "data_analytics", "iot", "ai_ml_product", "internal_tool", "gaming", "api_platform",
})
_PERSISTENCE_FREE_TYPES = frozenset({"ai_ml_product", "api_platform", "other"})

# Effort heuristic. These constants are rough planning figures, not measurements:
# engineer-weeks of work per scenario element, and a team capacity per experience level.
_TYPE_POINTS = {
    "content_site": 0.25, "internal_tool": 2, "web_app": 3, "api_platform": 3, "other": 3,
    "saas_b2b": 4, "ecommerce": 4, "data_analytics": 4, "ai_ml_product": 4, "marketplace": 5,
    "social": 5, "realtime_collab": 5, "iot": 5, "healthtech": 6, "gaming": 6, "fintech": 8,
}
_AUTH_POINTS = {"none": 0, "email_password": 1, "social": 1, "phone_otp": 1.5, "enterprise_sso": 2}
_ML_POINTS = {"none": 0, "inference_api": 2, "llm_rag": 3, "training": 4}
_COMPLIANCE_POINTS = {"pci_dss": 2, "hipaa": 2, "rbi_localisation": 2}
_CAPACITY = {"beginner": 0.6, "mixed": 0.75, "intermediate": 0.85, "senior": 1.0}
_CROSS_PLATFORM_LANGS = frozenset({"javascript", "typescript", "dart", "kotlin", "csharp"})
_IDP = re.compile(r"entra|azure ad|active directory|okta|google workspace|keycloak|auth0", re.I)

_SENSITIVITY_ORDER = ("health", "financial")
_RPO_BY_AVAILABILITY = {"99": 1440, "99.9": 60, "99.95": 15, "99.99": 5}
_ZONES = ("india", "eu", "us")


def _get(spec: dict, path: str, default=None):
    node = spec
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def _signal_values(signals: list[Signal], field: str) -> list:
    return [s.value for s in signals if s.field == field]


def _mark(signals: list[Signal], field: str, value) -> tuple[Signal, ...]:
    return tuple(replace(s, applied=True) if (s.field == field and s.value == value) else s
                 for s in signals)


def _match_existing(spec: dict, kb) -> tuple[dict, ...]:
    entries = getattr(kb, "existing", None) or ()
    deprecated = getattr(kb, "deprecated", None) or ()
    out = []
    for text in _get(spec, "infrastructure.existing_systems", []):
        for entry in entries:
            pattern = entry.get("pattern") if isinstance(entry, dict) else None
            if pattern and re.search(pattern, text, re.I):
                out.append({
                    "input": text,
                    "entry": entry,
                    "deprecated": [d for d in deprecated
                                   if d.get("pattern") and re.search(d["pattern"], text, re.I)],
                })
    return tuple(out)


def _effort(spec: dict, needs_backend: bool, collab: bool, idp_reused: bool, langs) -> float:
    ptype = _get(spec, "project.type")
    platforms = list(_get(spec, "project.platforms", []))
    points = _TYPE_POINTS.get(ptype, 3)
    # a sheet-sized internal tool is mostly one form and one table, not a full backend build
    tiny = ptype == "internal_tool" and max(_get(spec, "scale.users_at_launch", 0) or 0,
                                           _get(spec, "scale.users_12_months", 0) or 0) <= 100
    if "web" in platforms:
        points += 3 if needs_backend and not tiny else 0.75
    mobile = [p for p in platforms if p in ("android", "ios")]
    if mobile:
        points += 3
    if len(mobile) > 1:
        viable = not langs or bool(set(langs) & _CROSS_PLATFORM_LANGS)
        points += 1 if viable else 3
    if "desktop" in platforms:
        points += 3
    if "api_only" in platforms:
        points += 1
    auth = _get(spec, "requirements.auth", "none")
    points += 1 if (auth == "enterprise_sso" and idp_reused) else _AUTH_POINTS.get(auth, 0)
    if _get(spec, "requirements.payments"):
        points += 2 + (1 if ptype == "marketplace" else 0)
    if _get(spec, "requirements.realtime"):
        points += 2 + (1 if collab else 0)
    if _get(spec, "requirements.offline_support"):
        points += 3
    if _get(spec, "requirements.full_text_search"):
        points += 1
    if _get(spec, "requirements.file_media_storage"):
        points += 1
    if _get(spec, "requirements.multi_tenant"):
        points += 2
    points += _ML_POINTS.get(_get(spec, "requirements.ml_features", "none"), 0)
    for code in _get(spec, "compliance", []):
        points += _COMPLIANCE_POINTS.get(code, 1)
    return points * 0.5 if tiny else points


def derive(v: Validated, kb=None) -> Derived:
    spec = v.spec
    assumptions: list[str] = []
    signals = describe.parse(_get(spec, "project.description", ""))
    provided = v.provided
    deployment = _get(spec, "infrastructure.deployment_model", "managed_cloud")
    if deployment in ("on_premise", "air_gapped") and any(s.keyword == "no internet" for s in signals):
        # on a closed site network "no internet" describes the deployment, not unreliable connectivity for users
        signals = [s for s in signals if s.keyword != "no internet"]
        if deployment == "on_premise":
            signals.append(Signal("no internet", "infrastructure.deployment_model", "air_gapped", False,
                                  "description mentions 'no internet', which suggests a network with no outside access"))
            signals.sort(key=lambda s: (s.field, s.keyword, repr(s.value)))

    def absent(path: str) -> bool:
        return path not in provided

    def note(path: str, value, why: str) -> None:
        shown = json_text(value)
        assumptions.append(f"{path} not provided; derived as {shown} ({why})")

    def json_text(value) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if value is None:
            return "null"
        if isinstance(value, (list, tuple)):
            return "[" + ", ".join(str(x) for x in value) + "]"
        return str(value)

    launch = _get(spec, "scale.users_at_launch", 0)
    y1 = _get(spec, "scale.users_12_months", 0)
    users_peak = max(launch, y1)
    scale_level = math.log10(max(users_peak, 1))
    peak_concurrent = _get(spec, "scale.peak_concurrent_users")
    if peak_concurrent is None:
        peak_concurrent = max(10, int(users_peak * 0.05))
    pattern = _get(spec, "scale.traffic_pattern", "steady")
    workload = _get(spec, "scale.workload", "balanced")

    peak_rps = _get(spec, "scale.peak_rps")
    if peak_rps is None:
        peak_rps = round(peak_concurrent * 0.2 * (1.5 if pattern == "spiky" else 1.0), 2)
        note("scale.peak_rps", peak_rps, "0.2 requests per second per concurrent user, x1.5 when spiky")

    rating = min(5.0, max(1.0, scale_level - 2))
    # a derived peak is 5% of users by construction, so only a supplied peak can show a high ratio
    given_ratio = ("scale.peak_concurrent_users" in provided and users_peak
                   and peak_concurrent / users_peak > 0.1)
    if given_ratio or peak_rps > 500:
        rating += 0.5
    if workload == "write_heavy" and scale_level >= 5:
        rating += 0.5
    rating = min(5.0, rating)

    compliance = frozenset(_get(spec, "compliance", []))
    residency = _get(spec, "infrastructure.data_residency", "none")
    zone = None if residency == "none" else residency
    if zone is None and "rbi_localisation" in compliance:
        zone = "india"
        assumptions.append("infrastructure.data_residency is none but rbi_localisation requires payment "
                           "data to stay in India; the data zone is set to india")

    # user geography: given, else description, else compliance, else global
    geography = _get(spec, "scale.user_geography")
    if geography is None:
        from_text = sorted({s.value for s in signals if s.field == "scale.user_geography"})
        if from_text:
            geography = from_text
            for g in from_text:
                signals = list(_mark(signals, "scale.user_geography", g))
            kw = sorted({s.keyword for s in signals if s.field == "scale.user_geography"})
            note("scale.user_geography", geography, "description mentions " + ", ".join(repr(k) for k in kw))
        else:
            geography = sorted(
                {"eu" for c in compliance if c == "gdpr"} | {"india" for c in compliance if c in ("dpdp", "rbi_localisation")}
            )
            if geography:
                note("scale.user_geography", geography, "implied by compliance " + ", ".join(sorted(compliance)))
            else:
                geography = ["global"]
                note("scale.user_geography", geography, "no description or compliance hint")
    geography = tuple(geography)

    if zone:
        preferred = zone
    else:
        preferred = next((g for g in geography if g in _ZONES), None)
        if preferred is None:
            if "gdpr" in compliance:
                preferred = "eu"
            elif "dpdp" in compliance:
                preferred = "india"
            else:
                preferred = "us"
                assumptions.append("no residency and no geography: defaulting to a US region for widest "
                                   "service availability; CDN serves global users")

    platforms = frozenset(_get(spec, "project.platforms", []))
    ptype = _get(spec, "project.type")

    def req(key, default=False):
        return _get(spec, "requirements." + key, default)

    auth, ml = req("auth", "none"), req("ml_features", "none")
    features = (auth != "none" or req("payments") or req("realtime") or req("multi_tenant")
                or req("file_media_storage") or ml != "none" or req("offline_support")
                or (req("full_text_search") and ptype != "content_site"))
    needs_backend = bool(features or ptype in _BACKEND_TYPES)
    persistent = (auth != "none" or req("payments") or req("realtime") or req("file_media_storage")
                  or req("multi_tenant") or req("offline_support") or req("full_text_search"))
    ml_only = ml == "inference_api" and not persistent and ptype in _PERSISTENCE_FREE_TYPES
    needs_database = needs_backend and not ml_only

    # extras: given, else description signal (logged), else rule
    sig_geo = any(s.field == "derived.geo" for s in signals)
    collab = ptype == "realtime_collab" or any(s.field == "derived.collab" for s in signals)
    static_hint = ptype == "content_site" or any(s.field == "derived.static_hint" for s in signals)
    signals = list(_mark(signals, "derived.geo", True))
    signals = list(_mark(signals, "derived.collab", True))
    signals = list(_mark(signals, "derived.static_hint", True))
    cms = any(s.field == "derived.cms" for s in signals)
    signals = list(_mark(signals, "derived.cms", True))

    sensitivity = req("data_sensitivity", None)
    if sensitivity is None:
        sens_signals = {s.value: s for s in signals if s.field == "requirements.data_sensitivity"}
        if "hipaa" in compliance:
            sensitivity, why = "health", "compliance includes hipaa"
        elif compliance & {"pci_dss", "rbi_localisation"} or ptype == "fintech":
            sensitivity, why = "financial", "compliance or project.type is payment related"
        else:
            hit = next((sens_signals[k] for k in _SENSITIVITY_ORDER if k in sens_signals), None)
            if hit:
                sensitivity, why = hit.value, f"description mentions '{hit.keyword}'"
                signals = list(_mark(signals, hit.field, hit.value))
            elif compliance & {"dpdp", "gdpr"}:
                sensitivity, why = "personal", "compliance covers personal data"
            elif ptype == "internal_tool":
                sensitivity, why = "internal", "project.type is internal_tool"
            elif ptype == "content_site" and auth == "none":
                sensitivity, why = "public", "content site without sign-in"
            else:
                sensitivity, why = "personal", "default for products with users"
        note("requirements.data_sensitivity", sensitivity, why)

    consistency = req("consistency", None)
    if consistency is None:
        sig = next((s for s in signals if s.field == "requirements.consistency"), None)
        if req("payments") or ptype in ("fintech", "healthtech", "ecommerce", "marketplace"):
            consistency, why = "strong", "payments or a transactional project type"
        elif sig:
            consistency, why = "strong", f"description mentions '{sig.keyword}'"
            signals = list(_mark(signals, sig.field, sig.value))
        else:
            consistency, why = "eventual", "no payments or transactional signals"
        note("requirements.consistency", consistency, why)

    notifications = req("notifications", None)
    if notifications is None:
        chosen, reasons = set(), []
        if platforms & {"android", "ios"} and (req("realtime") or ptype in ("marketplace", "social")):
            chosen.add("push")
            reasons.append("mobile app with live or social features")
        if auth in ("email_password", "enterprise_sso") or req("payments"):
            chosen.add("email")
            reasons.append("email sign-in, SSO or payments")
        if auth == "phone_otp":
            chosen.add("sms")
            reasons.append("phone OTP sign-in")
        for s in signals:
            if s.field == "requirements.notifications":
                chosen.add(s.value)
                reasons.append(f"description mentions '{s.keyword}'")
                signals = list(_mark(signals, s.field, s.value))
        notifications = sorted(chosen)
        note("requirements.notifications", notifications, "; ".join(dict.fromkeys(reasons)) or "none implied")
    notifications = tuple(notifications)

    background = req("background_jobs", None)
    if background is None:
        sig = next((s for s in signals if s.field == "requirements.background_jobs"), None)
        triggers = [name for name, on in (
            ("payments", req("payments")), ("notifications", bool(notifications)),
            ("ML features", ml != "none"), ("file uploads", req("file_media_storage"))) if on]
        if sig:
            triggers.append(f"description mentions '{sig.keyword}'")
            signals = list(_mark(signals, sig.field, sig.value))
        background = bool(triggers)
        note("requirements.background_jobs", background, ", ".join(triggers) or "no trigger found")

    connectivity = req("connectivity", None)
    if connectivity is None:
        values = _signal_values(signals, "requirements.connectivity")
        if values:
            connectivity = "poor" if "poor" in values else "intermittent"
            signals = list(_mark(signals, "requirements.connectivity", connectivity))
            kw = sorted({s.keyword for s in signals if s.field == "requirements.connectivity"})
            note("requirements.connectivity", connectivity, "description mentions " + ", ".join(repr(k) for k in kw))
        else:
            connectivity = "reliable"
            note("requirements.connectivity", connectivity, "no connectivity hint in the description")

    ram = req("min_device_ram_gb", None)
    if ram is None:
        values = _signal_values(signals, "requirements.min_device_ram_gb")
        if values:
            ram = min(values)
            signals = list(_mark(signals, "requirements.min_device_ram_gb", ram))
            kw = sorted({s.keyword for s in signals if s.field == "requirements.min_device_ram_gb"})
            note("requirements.min_device_ram_gb", ram, "description mentions " + ", ".join(repr(k) for k in kw))
        else:
            note("requirements.min_device_ram_gb", None, "no device hint; no memory penalty applied")

    availability = req("availability", "99.9")
    rpo = req("rpo_minutes", None)
    if rpo is None:
        rpo = _RPO_BY_AVAILABILITY.get(availability, 60)
        note("requirements.rpo_minutes", rpo, f"availability target {availability}")

    size = _get(spec, "team.size", 1)
    size_12m = _get(spec, "team.size_in_12_months")
    if size_12m is None:
        size_12m = size
        note("team.size_in_12_months", size_12m, "same as team.size")
    lock_in = _get(spec, "infrastructure.vendor_lock_in_tolerance")
    if lock_in is None:
        lock_in = "medium"
        note("infrastructure.vendor_lock_in_tolerance", lock_in, "neutral default")
    licences = _get(spec, "infrastructure.existing_licences")
    if licences is None:
        licences = []
        note("infrastructure.existing_licences", licences, "none declared")

    existing = _match_existing(spec, kb)
    suite = {"m365": "microsoft_365", "google_workspace": "google_workspace"}
    licences = sorted(set(licences) | {suite[m["entry"]["id"]] for m in existing if m["entry"]["id"] in suite})
    idp_reused = any(_IDP.search(t) for t in _get(spec, "infrastructure.existing_systems", []))
    langs = frozenset(_get(spec, "team.languages", []))
    points = _effort(spec, needs_backend, collab, idp_reused, langs)
    capacity = _CAPACITY.get(_get(spec, "team.experience", "intermediate"), 0.85)
    est_weeks = max(0.5, round(2 * points / (capacity * size ** 0.8)) / 2)

    weights = {k: float(_get(spec, "priorities." + k, 3)) for k in
               ("cost", "time_to_market", "scalability", "performance", "maintainability", "security")}
    if (sensitivity in ("financial", "health") or compliance & {"pci_dss", "hipaa", "rbi_localisation"}) \
            and weights["security"] < 4:
        assumptions.append(f"priorities.security is {weights['security']:g}; raised to 4 because the data is "
                           "sensitive or the compliance scope is regulated")
        weights["security"] = 4.0
    p95 = req("p95_latency_ms", None)
    if p95 is not None and p95 <= 100 and weights["performance"] < 4:
        assumptions.append(f"priorities.performance is {weights['performance']:g}; raised to 4 because "
                           f"requirements.p95_latency_ms is {p95}")
        weights["performance"] = 4.0

    return Derived(
        users_peak=users_peak, scale_level=round(scale_level, 4), req_scale_rating=round(rating, 4),
        peak_concurrent=peak_concurrent, peak_rps=float(peak_rps), zone=zone, preferred_zone=preferred,
        platforms=platforms, needs_backend=needs_backend, needs_database=needs_database,
        data_sensitivity=sensitivity, consistency=consistency, connectivity=connectivity,
        min_device_ram_gb=ram, notifications=notifications, background_jobs=background,
        effort_points=round(points, 2), est_mvp_weeks=est_weeks, weights=weights,
        signals=tuple(signals), existing=existing, licences=frozenset(licences), team_langs=langs,
        user_geography=geography, team_size_12m=size_12m, lock_in_tolerance=lock_in, rpo_minutes=rpo,
        collab=collab, geo=sig_geo, static_hint=static_hint, cms=cms, assumptions=assumptions,
    )
