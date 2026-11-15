"""Hard constraints: filters every candidate must pass, conflicts between hard constraints, relaxations."""
from __future__ import annotations

from .model import Constraints, Derived, Warn

HYPERSCALERS = frozenset({"aws", "gcp", "azure"})
PARENT_CLOUD = {"firebase": "gcp"}
ZONES = ("india", "eu", "us")

SCOPE = {
    "backend": "data", "database": "data", "storage": "data", "cache": "data", "search": "data",
    "queue": "data", "auth": "data", "offline_sync": "data", "ml": "data", "iot_ingestion": "data", "cms": "data", "bi": "data",
    "hosting": "compute", "realtime": "compute", "security": "compute",
    "payments": "processor", "notifications": "processor",
    "cdn": "edge", "observability": "telemetry", "ci_cd": "tooling", "artifact_mirror": "tooling",
    "frontend": "none",
}
LOCK_LAYERS = frozenset({"hosting", "database", "cache", "queue", "storage", "search", "ml",
                         "observability", "security", "iot_ingestion"})
ONPREM_EXTERNAL_LAYERS = frozenset({"payments", "notifications"})
SELF_KINDS = ("self", "vm")


def _get(spec: dict, path: str, default=None):
    node = spec
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def cloud_of(provider: str | None) -> str | None:
    return PARENT_CLOUD.get(provider, provider)


def managed_migration(spec: dict, system: dict, off: dict | None) -> dict | None:
    """Move-to-managed details when an existing database engine lands on a managed offering, else None.

    The result carries the engine wording and, when the cloud has one, the migration tool and its docs link."""
    mig = system.get("migrate")
    if not mig or not off or off.get("provider") in (None, "self") or off.get("kind") == "self":
        return None
    if _get(spec, "infrastructure.deployment_model", "managed_cloud") != "managed_cloud":
        return None
    tool = mig.get(cloud_of(off["provider"])) or {}
    return {"detail": mig["detail"], "tool": tool.get("tool"), "url": tool.get("url")}


def _attestation_scopes(kb) -> dict[str, frozenset[str]]:
    out: dict[str, set[str]] = {}
    for row in kb.compliance.values():
        for att in row.get("attestation_required", []):
            out.setdefault(att, set()).update(row.get("scopes", []))
    return {k: frozenset(v) for k, v in out.items()}


def _attestation_exempt(kb) -> dict[str, frozenset[str]]:
    """Layers a regime leaves out of its attestation rule (for example HIPAA and payment processing)."""
    out: dict[str, set[str]] = {}
    for row in kb.compliance.values():
        layers = {x["layer"] for x in row.get("exempt_layers", [])}
        for att in row.get("attestation_required", []):
            out.setdefault(att, set()).update(layers)
    return {k: frozenset(v) for k, v in out.items()}


def build(spec: dict, d: Derived, kb) -> Constraints:
    deployment = _get(spec, "infrastructure.deployment_model", "managed_cloud")
    pref = _get(spec, "infrastructure.cloud_preference", "any")
    residency = _get(spec, "infrastructure.data_residency", "none")
    compliance = list(_get(spec, "compliance", []))
    ptype = _get(spec, "project.type")
    air = deployment == "air_gapped"
    onprem = deployment == "on_premise"
    self_host = deployment in ("on_premise", "air_gapped", "self_hosted_cloud_vm")

    attestations = set()
    for code in compliance:
        row = kb.compliance.get(code)
        if row:
            attestations.update(row.get("attestation_required", []))

    zone = d.zone if d.zone in ZONES else None
    c = Constraints(
        zone=zone, allowed_providers=None,
        cloud_lock=pref if pref in HYPERSCALERS else None,
        no_hyperscaler=(pref == "none" and deployment == "managed_cloud"),
        self_host_only=self_host, no_saas=(air or onprem), no_cdn=(air or onprem), no_internet=air,
        required_attestations=frozenset(attestations),
        payment_zone="india" if "rbi_localisation" in compliance else None,
        deployment=deployment,
        auth_needed=_get(spec, "requirements.auth", "none") != "none",
        commercial=(ptype not in ("content_site", "internal_tool") or bool(_get(spec, "requirements.payments"))),
        no_free_tier="hipaa" in compliance,
        onprem_processors=onprem,
    )

    # hard versus hard
    if "rbi_localisation" in compliance and residency in ("eu", "us", "other"):
        c.conflicts.append(Warn(
            "HARD_CONSTRAINT_CONFLICT", "critical",
            f"compliance includes rbi_localisation, which keeps payment data on servers in India, but "
            f"infrastructure.data_residency is {residency}. Both cannot hold at the same time.",
            ("compliance", "infrastructure.data_residency")))
        c.suggestions.append("Set infrastructure.data_residency to india (or none) so the data stays in India.")
        c.suggestions.append("Or remove rbi_localisation from compliance if payment data of Indian users is not stored.")
    if self_host and deployment != "self_hosted_cloud_vm" and c.cloud_lock:
        c.conflicts.append(Warn(
            "HARD_CONSTRAINT_CONFLICT", "critical",
            f"infrastructure.deployment_model is {deployment}, which cannot use a public cloud, but "
            f"infrastructure.cloud_preference is {pref}. Both cannot hold at the same time.",
            ("infrastructure.deployment_model", "infrastructure.cloud_preference")))
        c.suggestions.append("Set infrastructure.cloud_preference to any, or use deployment_model managed_cloud "
                             f"or self_hosted_cloud_vm to run on {pref}.")
        c.suggestions.append(f"Or keep {deployment} and drop the cloud preference.")

    # hard versus requirement
    if air:
        auth = _get(spec, "requirements.auth", "none")
        if _get(spec, "requirements.payments"):
            c.relaxed_requirements["requirements.payments"] = False
            msg = ("requirements.payments=true cannot be met inside an air-gapped network; payments must be "
                   "handled by a separate internet-connected billing system")
            c.relaxations.append("RELAXED: " + msg)
            c.notes.append(Warn("REQUIREMENT_RELAXED", "critical", msg[0].upper() + msg[1:] + ".",
                                ("requirements.payments", "infrastructure.deployment_model")))
        if auth in ("social", "phone_otp"):
            c.relaxed_requirements["requirements.auth"] = "email_password"
            how = ("social sign-in needs internet identity providers; Keycloak local accounts are used instead"
                   if auth == "social" else
                   "SMS OTP needs a carrier gateway; Keycloak local accounts with an authenticator-app OTP "
                   "are used instead (an on-premise SMS gateway can be added later)")
            msg = f"requirements.auth={auth} cannot be met inside an air-gapped network: {how}"
            c.relaxations.append("RELAXED: " + msg)
            c.notes.append(Warn("REQUIREMENT_RELAXED", "critical", msg[0].upper() + msg[1:] + ".",
                                ("requirements.auth", "infrastructure.deployment_model")))
        if _get(spec, "requirements.ml_features") == "llm_rag":
            msg = ("requirements.ml_features=llm_rag cannot use hosted LLM APIs in an air-gapped network; "
                   "a self-hosted open-weight model on local GPUs is used instead")
            c.relaxations.append("RELAXED: " + msg)
            c.notes.append(Warn("REQUIREMENT_RELAXED", "critical", msg[0].upper() + msg[1:] + ".",
                                ("requirements.ml_features", "infrastructure.deployment_model")))
        given = _get(spec, "requirements.notifications")
        if isinstance(given, list) and "push" in given:
            c.relaxed_requirements["requirements.notifications"] = [n for n in given if n != "push"]
            msg = ("requirements.notifications includes push, which needs Apple and Google push services over "
                   "the internet; push is dropped and users see in-app notifications instead")
            c.relaxations.append("RELAXED: " + msg)
            c.notes.append(Warn("REQUIREMENT_RELAXED", "critical", msg[0].upper() + msg[1:] + ".",
                                ("requirements.notifications", "infrastructure.deployment_model")))

    if residency == "other":
        c.notes.append(Warn(
            "REGION_UNVERIFIED", "warning",
            "infrastructure.data_residency is other, so no region can be checked against the knowledge base. "
            "Providers with the widest region lists are preferred; confirm the exact country or region "
            "before deployment.", ("infrastructure.data_residency",)))
    if "gdpr" in compliance and residency not in ("eu", "none"):
        c.notes.append(Warn(
            "GDPR_TRANSFER", "warning",
            f"compliance includes gdpr but infrastructure.data_residency is {residency}. Personal data of EU "
            "residents stored there needs a transfer mechanism (adequacy decision or standard contractual "
            "clauses) and a data processing agreement with each vendor.",
            ("compliance", "infrastructure.data_residency")))
    return c


def cloud_block(layer: str, name: str, off: dict, c: Constraints, kb=None) -> str | None:
    """Why an offering breaks infrastructure.cloud_preference, or None. Self-hosted software is always allowed."""
    kind = off["kind"]
    cloud = cloud_of(off["provider"])
    provider = ((kb.provider(off["provider"]) if kb else None) or {}).get("name", off["provider"])
    if layer == "frontend" or (kind == "self" and layer != "hosting"):
        return None
    if layer in LOCK_LAYERS:
        if c.cloud_lock and layer == "hosting" and cloud != c.cloud_lock:
            return f"{name} is not on {c.cloud_lock.upper()} (infrastructure.cloud_preference is {c.cloud_lock})"
        # the other lock layers also exclude neutral SaaS; the remaining layers below only exclude hyperscalers
        if c.cloud_lock and cloud != c.cloud_lock:
            return f"{name} is offered by {provider}, but infrastructure.cloud_preference is {c.cloud_lock}"
    elif c.cloud_lock and cloud in HYPERSCALERS and cloud != c.cloud_lock:
        return f"{name} is offered by {provider}, but infrastructure.cloud_preference is {c.cloud_lock}"
    if c.no_hyperscaler and cloud in HYPERSCALERS:
        return f"{name} is a {cloud.upper()} service, but infrastructure.cloud_preference is none (no hyperscaler)"
    return None


def passes(entry: dict, off: dict | None, layer: str, c: Constraints, kb) -> tuple[bool, str, tuple[str, ...]]:
    """Hard filter for one candidate. Returns (ok, reason, input fields behind the reason)."""
    name = (off or entry)["name"] if off else entry["name"]

    hit = kb.deprecated_hit(entry["name"]) or (kb.deprecated_hit(off["name"]) if off else None)
    if hit:
        return False, f"{name} is end-of-life or retired ({hit['name']}, {hit.get('date', 'date unknown')})", ()

    caps = entry.get("capabilities", [])
    if "k8s" in caps and not c.k8s_allowed:
        return (False, f"{name} (Kubernetes) adds operations load; it is allowed only for service-oriented or "
                       "microservices styles with a devops engineer and a team of 8 or more",
                ("team.has_devops", "team.size"))

    if layer == "cdn" and c.no_cdn:
        return False, f"{name}: no public CDN in a {c.deployment} deployment", ("infrastructure.deployment_model",)
    if layer == "hosting":
        if c.deployment == "managed_cloud" and ("on_premise" in caps or (off and off["kind"] == "self")):
            return False, f"{name} runs on your own servers, not a managed cloud", ("infrastructure.deployment_model",)
        if c.deployment == "air_gapped" and "air_gapped" not in caps:
            return (False, f"{name} cannot run without internet access (infrastructure.deployment_model is "
                           "air_gapped)", ("infrastructure.deployment_model",))
        if c.deployment == "on_premise" and "on_premise" not in caps:
            return (False, f"{name} is a cloud service, but infrastructure.deployment_model is on_premise",
                    ("infrastructure.deployment_model",))
        if c.deployment == "self_hosted_cloud_vm" and not (off and off["kind"] in SELF_KINDS):
            return (False, f"{name} is a managed platform, but infrastructure.deployment_model is "
                           "self_hosted_cloud_vm (you run the VMs)", ("infrastructure.deployment_model",))
        if c.no_cdn and off and off.get("edge_not_regional"):
            return False, f"{name} depends on a global edge network", ("infrastructure.deployment_model",)

    if off is None:
        return True, "", ()
    kind = off["kind"]
    provider = off["provider"]
    cloud = cloud_of(provider)

    # deployment model
    if c.self_host_only:
        core = layer in ("backend", "database", "hosting")
        if c.deployment == "self_hosted_cloud_vm":
            if core and kind not in SELF_KINDS:
                return (False, f"{name} is a managed service, but infrastructure.deployment_model is "
                               "self_hosted_cloud_vm", ("infrastructure.deployment_model",))
        else:
            external_ok = c.onprem_processors and layer in ONPREM_EXTERNAL_LAYERS
            if kind not in SELF_KINDS and not external_ok:
                return (False, f"{name} is an external service, but infrastructure.deployment_model is "
                               f"{c.deployment}", ("infrastructure.deployment_model",))

    # explicit cloud choice
    why = cloud_block(layer, name, off, c, kb)
    if why:
        return False, why, ("infrastructure.cloud_preference",)

    scope = SCOPE.get(layer, "none")
    zone = c.zone
    prov = kb.provider(provider) or {}

    # data residency
    if zone and kind != "self":
        pz = off.get("processing_zone") or (off.get("auth_processing_zone") if c.auth_needed else None)
        if pz and pz != zone:
            return (False, f"{name} processes user data in {pz.upper() if pz == 'us' else pz}, which breaks "
                           f"data_residency={zone}", ("infrastructure.data_residency", "requirements.auth"))
        if scope in ("data", "compute"):
            if not off.get("regions", {}).get(zone):
                return (False, f"{name} has no verified {zone} region (infrastructure.data_residency is {zone})",
                        ("infrastructure.data_residency",))
            if scope == "data" and prov.get("full_residency", {}).get(zone) is False:
                return (False, f"{provider} cannot keep data, logs and backups inside {zone} "
                               f"(infrastructure.data_residency is {zone})", ("infrastructure.data_residency",))
    if c.payment_zone and layer == "payments" and kind != "self":
        if not (off.get("regions", {}).get(c.payment_zone) or prov.get("full_residency", {}).get(c.payment_zone)):
            return (False, f"{name} does not store payment data in {c.payment_zone}, which rbi_localisation requires",
                    ("compliance", "infrastructure.data_residency"))

    # attestations on managed in-scope services
    if kind != "self" and c.required_attestations:
        scopes = _attestation_scopes(kb)
        exempt = _attestation_exempt(kb)
        have = set(off.get("attestations", []))
        for att in sorted(c.required_attestations):
            if scope in scopes.get(att, ()) and layer not in exempt.get(att, ()) and att not in have:
                return (False, f"{name} does not list {att} in the knowledge base (required by the compliance "
                               "setting)", ("compliance",))
    return True, "", ()
