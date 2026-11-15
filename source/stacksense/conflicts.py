"""Warnings that depend on the chosen stack, the cost band and the derived features."""
from __future__ import annotations

from . import KB_AS_OF
from .constraints import cloud_block, cloud_of, managed_migration
from .model import CostBand, Derived, Constraints, Selection, Warn

CLIENT_PLATFORMS = ("web", "android", "ios", "desktop")
SELF_HOSTED = ("on_premise", "air_gapped", "self_hosted_cloud_vm")
JS_FAMILY = frozenset({"javascript", "typescript"})


def _get(spec: dict, path: str, default=None):
    node = spec
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return default if node is None else node


def _parts(sel: Selection):
    """Every chosen component as (layer, Scored)."""
    for pick in (sel.frontend, sel.backend, sel.database, sel.hosting, *sel.supporting):
        for s in (pick.parts or ([pick.chosen] if pick.chosen else [])):
            yield pick.layer, s


def pre_relax(spec: dict) -> tuple[dict, list[Warn], list[str]]:
    """Availability 99.99 for a team that cannot run it is designed as 99.95 instead."""
    avail = str(_get(spec, "requirements.availability", "99.9"))
    size = _get(spec, "team.size", 1)
    exp = _get(spec, "team.experience", "intermediate")
    devops = bool(_get(spec, "team.has_devops", False))
    if avail != "99.99" or (size > 2 and exp != "beginner" and devops):
        return spec, [], []
    why = []
    if size <= 2:
        why.append(f"team.size is {size}")
    if exp == "beginner":
        why.append("team.experience is beginner")
    if not devops:
        why.append("team.has_devops is false")
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in spec.items()}
    out["requirements"]["availability"] = "99.95"
    msg = ("A 99.99% availability target needs multi-region failover and an on-call practice, but "
           + ", ".join(why) + ". The design targets 99.95% instead.")
    return (out,
            [Warn("AVAILABILITY_UNREALISTIC", "critical", msg,
                  ("requirements.availability", "team.size", "team.has_devops", "team.experience"))],
            ["RELAXED: requirements.availability: 99.99 is designed as 99.95 (" + ", ".join(why) + ")."])


def no_candidate(layer: str, reasons, constraint_fields=()) -> tuple[Warn, list[str]]:
    shown = [f"{r.name}: {r.reason}" for r in reasons[:3]]
    fields = sorted({f for r in reasons for f in r.fields} | set(constraint_fields)) or ["infrastructure.deployment_model"]
    msg = f"No {layer} option in the knowledge base meets every hard constraint."
    if shown:
        msg += " Closest rejections: " + "; ".join(shown) + "."
    return (Warn("NO_COMPLIANT_CANDIDATE", "critical", msg, tuple(fields)),
            [f"Relax one of {', '.join(fields)} so that at least one {layer} option remains."])


def _eol_severity(rec: dict) -> str:
    rule = rec.get("severity_rule", "by_date")
    if rule != "by_date":
        return rule
    date = rec.get("date") or "9999-12-31"
    if date <= KB_AS_OF:
        return "critical"
    year, rest = int(date[:4]), date[4:]
    return "warning" if f"{year - 1}{rest}" <= KB_AS_OF else "info"


def _phase(subject: str, rec: dict) -> str | None:
    """Support-phase sentence for a matched deprecated record, or None when nothing needs saying."""
    status, date, succ, name = rec["status"], rec.get("date") or "", rec["successor"], rec["name"]
    past = bool(date) and date <= KB_AS_OF
    if status == "renamed":
        if succ.lower() in subject.lower():
            return None
        return f"{subject} is the former name of {succ} (renamed {date}); use the new name in configuration and documents."
    if "extended support" in name.lower():
        if past:
            return f"{subject} is out of extended support since {date}; upgrade to {succ}."
        return (f"{subject} is past mainstream support and in extended support until {date}; "
                f"plan an upgrade to {succ}.")
    if status == "maintenance":
        return f"{subject} only receives maintenance fixes (since {date}); plan a move to {succ}."
    if status == "closed_to_new":
        return f"{subject} is closed to new customers (since {date}); plan a move to {succ}."
    word = {"eol": "end of life", "retired": "retired", "shutdown": "shut down"}.get(status, status)
    if past:
        verb = "reached end of life" if status == "eol" else f"was {word}"
        return f"{subject} {verb} on {date}; move to {succ}."
    verb = "reaches end of life" if status == "eol" else f"will be {word}"
    return f"{subject} {verb} on {date}; plan a move to {succ}."


def _lang_ok(entry: dict, team: frozenset) -> bool:
    langs = set(entry.get("languages") or [])
    if not langs or not team:
        return True
    return bool(langs & team) or bool(langs & JS_FAMILY and team & JS_FAMILY)


def check(spec: dict, d: Derived, c: Constraints, sel: Selection, cost: CostBand, kb, provided=frozenset()) -> list[Warn]:
    out: list[Warn] = []

    def add(code, sev, msg, fields):
        out.append(Warn(code, sev, msg, tuple(fields)))

    size = _get(spec, "team.size", 1)
    exp = _get(spec, "team.experience", "intermediate")
    devops = bool(_get(spec, "team.has_devops", False))
    avail = str(_get(spec, "requirements.availability", "99.9"))
    plats = list(_get(spec, "project.platforms", []))
    deployment = _get(spec, "infrastructure.deployment_model", "managed_cloud")
    zone = c.zone
    budget = float(_get(spec, "budget.monthly_infra_usd", 0) or 0)
    weeks = _get(spec, "timeline.mvp_weeks", 0)
    reqs = _get(spec, "requirements", {}) or {}

    # budget
    if cost.min > budget:
        top = sorted((b for b in cost.breakdown if b.get("min", 0) > 0), key=lambda b: (-b["min"], b["component"]))[:3]
        levers = (" The largest costs are " + ", ".join(f"{b['component']} (${b['min']:,.0f})" for b in top)
                  + ". Raise the budget to at least that minimum, or lower the 12-month user count, the "
                  "availability target or the stored data volume.") if top else ""
        add("BUDGET_EXCEEDED", "critical",
            f"The lowest-cost stack found that meets the hard constraints costs at least ${cost.min:,.0f} a month, "
            f"but budget.monthly_infra_usd is ${budget:,.0f}.{levers}",
            ["budget.monthly_infra_usd", "scale.users_12_months"])
    elif cost.min <= budget < cost.max:
        add("BUDGET_TIGHT", "warning",
            f"The estimate is ${cost.min:,.0f}-{cost.max:,.0f} a month against a budget of ${budget:,.0f}, "
            "so a busy month could go over.", ["budget.monthly_infra_usd"])

    # free tiers
    free = []
    for _, s in _parts(sel):
        f = (s.off or {}).get("free_tier") or {}
        if f.get("available") and s.cost[0] == 0 and not s.bundled_in and (s.off or {}).get("kind") != "self" \
                and s.name not in [x[0] for x in free]:
            free.append((s.name, f.get("limits", "")))
    if free:
        add("FREE_TIER_LIMITS", "info",
            "The low end of the estimate relies on free tiers: " + "; ".join(f"{n} ({l})" for n, l in free[:4])
            + ". Check them against the expected traffic.", ["budget.monthly_infra_usd", "scale.traffic_pattern"])

    # availability, timeline, platforms, latency
    if avail in ("99.95", "99.99"):
        factor = 1.5 if avail == "99.95" else 2.2
        add("AVAILABILITY_COST", "info",
            f"requirements.availability is {avail}, so hosting, database and cache are priced at x{factor:g} "
            "for standby capacity.", ["requirements.availability"])
    est = d.est_mvp_weeks
    if weeks and weeks < 0.5 * est:
        add("TIMELINE_UNREALISTIC", "critical",
            f"timeline.mvp_weeks is {weeks}, but the scope needs about {est:g} weeks for this team; "
            "cut features or extend the timeline.", ["timeline.mvp_weeks", "project.platforms", "team.size"])
    elif weeks and weeks < est:
        add("TIMELINE_TIGHT", "warning",
            f"timeline.mvp_weeks is {weeks} against an estimate of about {est:g} weeks for the scope.",
            ["timeline.mvp_weeks"])
    clients = [p for p in plats if p in CLIENT_PLATFORMS]
    if len(clients) >= 3 and size <= 3:
        add("PLATFORM_SPREAD", "warning",
            f"{len(clients)} client platforms ({', '.join(clients)}) for a team of {size} spreads effort thin; "
            "ship one platform first.", ["project.platforms", "team.size"])
    p95 = reqs.get("p95_latency_ms")
    geo = list(d.user_geography)
    if p95 is not None and (p95 <= 50 or (p95 <= 100 and (len(geo) > 1 or "global" in geo))):
        add("LATENCY_AGGRESSIVE", "warning",
            f"requirements.p95_latency_ms is {p95}; that needs regional deployment close to users and "
            "measured load tests.", ["requirements.p95_latency_ms", "scale.user_geography"])

    # team fit
    if d.team_langs:
        for layer, s in _parts(sel):
            if layer in ("frontend", "backend") and s.entry and not _lang_ok(s.entry, d.team_langs):
                add("SKILL_MISMATCH", "warning",
                    f"{s.name} uses {', '.join(s.entry.get('languages', []))}, but team.languages is "
                    f"{', '.join(sorted(d.team_langs))}; budget time for the team to learn it.",
                    ["team.languages"])
    feats = [n for n, on in (("realtime", reqs.get("realtime")), ("payments", reqs.get("payments")),
                             ("multi-tenancy", reqs.get("multi_tenant")), ("offline support", reqs.get("offline_support")),
                             ("ML features", reqs.get("ml_features", "none") != "none"),
                             ("regulated compliance", bool(set(_get(spec, "compliance", [])) &
                                                           {"hipaa", "pci_dss", "rbi_localisation", "soc2", "iso27001"})))
             if on]
    if (exp == "beginner" or size == 1) and len(feats) >= 2:
        add("BEGINNER_COMPLEXITY", "warning",
            f"team.experience is {exp} with team.size {size}, yet the scope includes {', '.join(feats)}; "
            "prefer managed services and cut scope.", ["team.experience", "team.size"])
    if deployment in SELF_HOSTED and not devops:
        crit = deployment == "air_gapped" and avail in ("99.95", "99.99")
        add("NO_DEVOPS_SELF_HOSTED", "critical" if crit else "warning",
            f"infrastructure.deployment_model is {deployment} but team.has_devops is false"
            + (f", and the {avail}% availability target needs on-call operations" if crit else "")
            + "; someone must patch and back up the servers.", ["team.has_devops", "infrastructure.deployment_model"])
    if (d.users_peak >= 1_000_000 or d.req_scale_rating >= 4) and sel.arch.style in ("monolith", "modular_monolith", "serverless") \
            and (size <= 8 or not devops):
        add("OVER_ENGINEERING_AVOIDED", "info",
            f"With {_get(spec, 'scale.users_12_months', 0):,} users in 12 months a {sel.arch.style.replace('_', ' ')} architecture is "
            f"still enough for a team of {size}; split services only when a measured bottleneck appears.",
            ["team.size", "team.has_devops", "scale.users_12_months"])

    # platform mismatches
    if reqs.get("seo_critical") and "web" not in plats:
        add("SEO_WITHOUT_WEB", "warning", "requirements.seo_critical is true but project.platforms has no web client.",
            ["requirements.seo_critical", "project.platforms"])
    if reqs.get("offline_support") and plats and set(plats) <= {"web"}:
        add("OFFLINE_WEB_ONLY", "warning",
            "requirements.offline_support is true for a web-only project; a PWA with a service worker covers it "
            "with limited storage.", ["requirements.offline_support", "project.platforms"])

    # compliance
    for code in _get(spec, "compliance", []):
        rec = kb.compliance.get(code)
        if not rec:
            continue
        ob = "; ".join(rec["obligations"][:2])
        where = f" The stack keeps data in {sel.region}." if sel.region and code in ("rbi_localisation", "dpdp", "gdpr") else ""
        att = (f" Managed services need attestations: {', '.join(sorted(c.required_attestations))}."
               if c.required_attestations and code in ("hipaa", "pci_dss", "soc2", "iso27001") else "")
        used = {layer for layer, _ in _parts(sel)}
        ex = "".join(f" The {x['layer']} layer is outside this rule: {x['reason']}."
                     for x in rec.get("exempt_layers", []) if x["layer"] in used)
        add("COMPLIANCE_" + {"rbi_localisation": "RBI"}.get(code, code.upper()), "info",
            f"{rec['name']}: {ob}.{where}{att}{ex}", ["compliance"])

    # sms
    sms = reqs.get("auth") == "phone_otp" or "sms" in d.notifications
    if sms and (zone == "india" or "india" in geo):
        add("SMS_DLT_REQUIRED", "info",
            "SMS to Indian numbers must go through a DLT-registered sender and templates; register before launch.",
            ["requirements.auth", "scale.user_geography"])

    # on-premise external processors
    if c.onprem_processors:
        what = []
        for _, s in _parts(sel):
            e = s.entry or {}
            if s.off and s.off.get("kind") != "self" and s.name not in what and (
                    e.get("layer") in ("payments", "notifications") or e.get("sub_layer") == "sms_otp"):
                what.append(s.name)
        if what:
            add("ONPREM_EXTERNAL_API", "warning",
                f"infrastructure.deployment_model is {deployment}, but {', '.join(what)} "
                f"{'still calls an external service' if len(what) == 1 else 'still call external services'}; "
                "open only those outbound connections.",
                ["infrastructure.deployment_model", "requirements.payments"])

    # a layer that had no option on the preferred cloud
    existing_ids = {e.get("prefer") for m in d.existing for e in (m["entry"].get("effects") or {}).values()}
    off_cloud = [f"{s.name} ({layer})" for layer, s in _parts(sel)
                 if s.off and s.entry_id not in existing_ids and cloud_block(layer, s.name, s.off, c)]
    if off_cloud:
        add("CLOUD_PREFERENCE_RELAXED", "critical",
            f"infrastructure.cloud_preference is {_get(spec, 'infrastructure.cloud_preference')}, but no option on it "
            f"meets the other hard constraints for {', '.join(off_cloud)}; these use another cloud or vendor.",
            ["infrastructure.cloud_preference"])

    # residency of telemetry and CDN
    for layer, s in _parts(sel):
        if zone and s.off and s.off.get("kind") != "self" and layer == "observability" \
                and not (s.off.get("regions") or {}).get(zone):
            add("RESIDENCY_TELEMETRY", "warning",
                f"{s.name} has no {zone} region; log only scrubbed events and keep personal data out of telemetry.",
                ["infrastructure.data_residency"])
        if zone and layer == "cdn":
            add("CDN_STATIC_ONLY", "info",
                f"The CDN ({s.name}) is used for static assets only; personal data stays at the origin in {zone}.",
                ["infrastructure.data_residency"])

    # description and existing systems
    desc = _get(spec, "project.description", "") or ""
    hit = kb.deprecated_hit(desc)
    if hit:
        add("DEPRECATED_TECH_MENTIONED", "warning",
            f"project.description mentions {hit['name']} ({hit['status']} since {hit['date']}); the stack uses "
            f"{hit['successor']} instead.", ["project.description"])
    for sig in d.signals:
        if not sig.applied and sig.field in provided and _get(spec, sig.field) != sig.value:
            add("DESCRIPTION_MISMATCH", "info",
                f"The description suggests {sig.field} = {sig.value} ({sig.reason}); the value you provided is kept.",
                ["project.description", sig.field])
    chosen_ids = {s.entry_id for _, s in _parts(sel)}
    by_layer = {}
    for layer, s in _parts(sel):
        by_layer.setdefault(layer, s)
    picks = {p.layer: p for p in (sel.backend, sel.database, *sel.supporting)}
    matched, reused = set(), set()
    for m in d.existing:
        matched.add(m["input"])
        recs = m["deprecated"]
        mig = m["entry"].get("migration") if recs else None
        for i, rec in enumerate(recs):
            msg = _phase(m["input"], rec)
            if msg and i == 0 and mig:
                target = by_layer.get(mig["layer"])
                msg += " " + mig["text"].replace("{target}", target.name if target else "the new component")
            if msg:
                add("EOL_EXISTING_SYSTEM", _eol_severity(rec), msg, ["infrastructure.existing_systems"])
        if any(r["status"] in ("eol", "retired", "shutdown") and (r.get("date") or "9999") <= KB_AS_OF for r in recs):
            continue
        effects = m["entry"].get("effects") or {}
        prefer = {x for e in effects.values() for x in (e.get("prefer"), *e.get("accept", ()))}
        how = m["entry"].get("integration", "").replace("{name}", m["input"])
        if (prefer & chosen_ids or not prefer) and m["input"] not in reused:
            reused.add(m["input"])
            dbp = picks.get("database")
            moved = (managed_migration(spec, m["entry"], dbp.chosen.off)
                     if dbp and dbp.chosen and dbp.chosen.entry_id == (effects.get("database") or {}).get("prefer") else None)
            if moved:
                how = (f"It moves to {dbp.chosen.name}, which keeps {moved['detail']}"
                       + (f"; migrate with {moved['tool']} ({moved['url']})." if moved["tool"] else "; plan the data migration."))
                add("EXISTING_SYSTEM_REUSED", "info", f"{m['input']} is reused as the data source, not kept in place. {how}",
                    ["infrastructure.existing_systems"])
            else:
                add("EXISTING_SYSTEM_REUSED", "info", f"{m['input']} is reused. {how}", ["infrastructure.existing_systems"])
        for layer, eff in sorted(effects.items()):
            pick = picks.get(layer)
            if layer in ("database", "auth") and not {eff.get("prefer"), *eff.get("accept", ())} & chosen_ids \
                    and pick and pick.chosen:
                why = next((f"it was excluded: {x.reason.rstrip('.')}" for x in pick.excluded
                            if x.entry_id == eff["prefer"]), "it scored lower than the chosen option for this project")
                add("EXISTING_SYSTEM_REPLACED", "info",
                    f"{m['input']} is not kept as the {layer} of the new stack: {pick.chosen.name} is used instead "
                    f"because {why}. Plan a migration of its data or accounts.", ["infrastructure.existing_systems"])
    dbp, hp = picks.get("database"), sel.hosting
    if dbp and dbp.chosen and dbp.chosen.off and dbp.chosen.off.get("kind") == "self" and "existing" in dbp.chosen.name.lower() \
            and hp and any(x.off and x.off.get("kind") != "self" for x in (hp.parts or [hp.chosen]) if x):
        link = {"aws": "AWS Site-to-Site VPN or Direct Connect", "azure": "an Azure VPN gateway or ExpressRoute",
                "gcp": "Cloud VPN or Cloud Interconnect"}.get(cloud_of(hp.chosen.provider), "a site-to-site VPN or a private link")
        add("HYBRID_CONNECTIVITY", "warning",
            f"{dbp.chosen.name} stays on-premises while the app runs on {hp.chosen.name}. Connect them with {link}; "
            f"every query then crosses that link, so expect added latency and a dependency on it for availability.",
            ["infrastructure.existing_systems", "infrastructure.deployment_model"])
    for text in _get(spec, "infrastructure.existing_systems", []):
        if text not in matched and not kb.deprecated_hit(text):
            add("EXISTING_SYSTEM_UNRECOGNISED", "info",
                f"{text} is not in the knowledge base, so no integration was planned for it.",
                ["infrastructure.existing_systems"])
        elif text not in matched:
            hit = kb.deprecated_hit(text)
            msg = _phase(text, hit)
            target = by_layer.get(hit.get("layer"))
            if msg and target:
                msg += f" The stack's {hit['layer']} is {target.name}, so plan a rewrite rather than an in-place upgrade."
            if msg:
                add("EOL_EXISTING_SYSTEM", _eol_severity(hit), msg, ["infrastructure.existing_systems"])

    # compliance process and scope
    comp = set(_get(spec, "compliance", []))
    if reqs.get("payments") and "pci_dss" not in comp and deployment not in SELF_HOSTED:
        add("PCI_SCOPE", "info",
            "Card data still falls under PCI DSS even though compliance does not list pci_dss. Use the payment "
            "provider's hosted checkout or redirect so card numbers never touch these servers; the merchant then "
            "files the short Self-Assessment Questionnaire A instead of a full assessment.",
            ["requirements.payments", "compliance"])
    certs = sorted(comp & {"iso27001", "soc2"})
    if certs:
        add("CERTIFICATION_IS_PROCESS", "info",
            f"{' and '.join(x.replace('iso27001', 'ISO 27001').replace('soc2', 'SOC 2') for x in certs)} "
            "certifies the organisation, not the stack: the chosen services' attestations cover their part, but the "
            "team still needs its own risk register, access reviews, change records and an external audit.",
            ["compliance"])
    if zone == "india" and deployment not in ("air_gapped",):
        add("CERT_IN_LOGS", "info",
            "CERT-In Directions (28 April 2022) require system logs to be kept for 180 days within India and cyber "
            "incidents to be reported within 6 hours; set log retention in the India region accordingly.",
            ["infrastructure.data_residency"])
    if avail in ("99.95", "99.99") and (size <= 3 or not devops):
        add("ON_CALL_GAP", "warning",
            f"requirements.availability is {avail}% (about {43.8 if avail == '99.95' else 4.4:g} minutes of downtime "
            f"a month) with team.size {size} and team.has_devops {str(devops).lower()}; that needs a paged on-call "
            "rota and tested runbooks, or a lower target.",
            ["requirements.availability", "team.size", "team.has_devops"])

    # device and network conditions
    mobile = [p for p in plats if p in ("android", "ios")]
    if d.min_device_ram_gb is not None and d.min_device_ram_gb <= 2 and mobile:
        add("LOW_END_DEVICE", "info",
            f"Target devices have about {d.min_device_ram_gb:g} GB of RAM: keep the app bundle small, use the "
            "Hermes engine for React Native, paginate lists and avoid holding images in memory.",
            ["requirements.min_device_ram_gb", "project.platforms"])
    if d.connectivity == "poor" and plats:
        add("LOW_BANDWIDTH_MEDIA", "info",
            "requirements.connectivity is poor: compress and resize photos on the device before upload, upload "
            "in resumable chunks, and cache reads locally.",
            ["requirements.connectivity"])
    if d.geo and (zone or comp & {"dpdp", "gdpr"}):
        add("LOCATION_DATA", "info",
            "The app records location, which is personal data: ask for consent before collecting it, store only "
            "the precision the feature needs and set a retention period.",
            ["project.description", "compliance"])

    # domain patterns
    ptype = _get(spec, "project.type", "other")
    pay = next((p.chosen for p in sel.supporting if p.layer == "payments" and p.chosen), None)
    if reqs.get("payments") and pay:
        msg = (f"Make every {pay.name} charge and refund call idempotent (one idempotency key or order id per attempt) "
               "and store processed webhook event ids, so a retry or a repeated webhook never charges or refunds twice.")
        fields = ["requirements.payments"]
        if ptype == "fintech":
            msg += (" Record money movements in an append-only double-entry ledger (each transfer as balanced debit "
                    "and credit rows) rather than updating balances in place.")
            fields.append("project.type")
        add("PAYMENT_IDEMPOTENCY", "info", msg, fields)
    db = sel.database.chosen
    if ptype in ("ecommerce", "marketplace") and db:
        how = ("a single conditional UPDATE (for example WHERE stock > 0) or a row lock inside a transaction"
               if (db.entry or {}).get("sub_layer") in ("relational", "newsql", "embedded")
               else "a conditional or transactional write")
        add("OVERSELL_RISK", "info",
            f"Claim stock, slots or bookings in {db.name} with {how}, not a read followed by a write, so two buyers "
            "cannot take the last unit.", ["project.type"])
    stores = [rule for plat, rule in (
        ("ios", "App Store Review Guideline 1.2 asks for content filtering, reporting with timely responses, "
                "user blocking and published contact details"),
        ("android", "Google Play's user-generated content policy asks for in-app reporting and blocking of "
                    "objectionable content and users"))
        if plat in plats]
    if ptype == "social" and stores:
        add("UGC_MODERATION", "info",
            "User posts need moderation tools before store review: " + ", and ".join(stores) + ".",
            ["project.type", "project.platforms"])
    return out
