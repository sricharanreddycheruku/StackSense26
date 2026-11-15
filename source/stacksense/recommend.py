"""Candidate scoring and selection of the frontend, backend, database, hosting and supporting services."""
from __future__ import annotations

import itertools
import math
import re
from dataclasses import dataclass, replace

from . import constraints as cons
from .cost import gb_rate, item_cost
from .model import (ArchDecision, article, Constraints, Derived, Excluded, Mod, Pick, Scored, Selection)

GENERIC_TAGS = frozenset({"faas", "managed", "mobile", "any", "edge"})
SHARE = {"hosting": 0.35, "database": 0.30, "backend": 0.35}
SUPPORT_SHARE = 0.08
JS_FAMILY = frozenset({"javascript", "typescript"})
LICENCE_PROVIDER = {"aws_credits": "aws", "gcp_credits": "gcp", "azure_credits": "azure",
                    "microsoft_365": "azure", "google_workspace": "gcp"}
LICENCE_ENTRY = {"sql_server": "sqlserver", "github_enterprise": "github_actions", "gitlab": "gitlab_ci"}
OUT_LAYER = {"offline_sync": "other", "notifications": "other", "security": "other",
             "iot_ingestion": "other", "artifact_mirror": "other", "cms": "other",
             "bi": "other"}
BAAS_DB = {"postgres": "postgresql"}
MOBILE = ("android", "ios")
NO_TEAM_SIZE = 1
COHERENCE_EXEMPT = frozenset({"payments", "ml"})
RELATIONAL_TYPES = frozenset({"web_app", "saas_b2b", "ecommerce", "marketplace", "fintech", "healthtech", "internal_tool"})
CLOUD_MISMATCH = -60
GAMEPLAY_WORDS = re.compile(r"\b(?:shooter|fps|multiplayer|gameplay|platformer|2d|3d|battle royale|racing game|"
                            r"real-time (?:game|multiplayer|1v1)|turn-based|puzzle|(?:card|word|board) game)\b", re.I)
GAME_SERVICE_WORDS = re.compile(r"\b(?:dashboard|companion|admin|analytics|backend|api|store ?front|marketplace)\b", re.I)


class NoCandidate(Exception):
    def __init__(self, layer: str, reasons: list[Excluded]):
        super().__init__(layer)
        self.layer = layer
        self.reasons = reasons


@dataclass
class Ctx:
    spec: dict
    d: Derived
    c: Constraints
    kb: object
    arch: ArchDecision
    budget_mode: bool
    budget: float
    commercial: bool


def G(spec: dict, path: str, default=None):
    node = spec
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return default if node is None else node


def cloud(provider):
    return cons.cloud_of(provider)


def vendor_of(provider):
    """Account holder behind a provider; None for self-hosted software."""
    return None if provider in (None, "self") else cloud(provider)


def has(entry: dict, cap: str) -> bool:
    return cap in entry.get("capabilities", [])


def _tags(entry: dict) -> set[str]:
    return set(entry.get("runs_on", [])) - GENERIC_TAGS


def _key(s: Scored) -> tuple:
    r = (s.entry or {}).get("ratings", {})
    return (r.get("ops_burden", 3), -r.get("maturity", 3), s.entry_id, s.offering_id or "")


def _rank(s: Scored) -> tuple:
    return (-s.score,) + _key(s)


def zone_of(ctx: Ctx) -> str:
    return ctx.c.zone or ctx.d.preferred_zone


def region_of(off: dict | None, ctx: Ctx, prefer: str | None = None) -> str | None:
    if not off or off.get("kind") == "self":
        return None
    regs = off.get("regions") or {}
    own = regs.get(zone_of(ctx)) or []
    if own:
        return prefer if prefer in own else own[0]
    for z in ("us", "eu", "india", "apac"):
        if regs.get(z):
            return regs[z][0]
    return None


# scoring

def lang_fit(entry: dict, ctx: Ctx, layer: str) -> float:
    if layer not in ("frontend", "backend"):
        return 1.0
    langs = set(entry.get("languages") or [])
    if not langs:
        return 0.8
    team = set(ctx.d.team_langs) - ({"sql"} if layer == "backend" else set())
    if not team:
        return 0.6
    if langs & team:
        return 1.0
    if langs & JS_FAMILY and team & JS_FAMILY:
        return 0.9
    return 0.0


def _cost_fit(ctx: Ctx, entry: dict, off: dict | None, cost: tuple, share: float) -> float:
    src = off if off is not None else entry
    if not src.get("cost_anchors"):
        return float(entry["ratings"]["resource_efficiency"])
    mid = (cost[0] + cost[1]) / 2
    if ctx.budget <= 0:
        return 5.0 if cost[0] == 0 else 0.0
    b = ctx.budget * share
    if mid <= 0.2 * b:
        return 5.0
    if mid >= 1.5 * b:
        return 0.0
    return 5.0 * math.log(1.5 * b / mid) / math.log(7.5)


def _cost_tail(ctx: Ctx, entry: dict, off: dict | None, cost: tuple, share: float) -> float:
    """Extra cost points below a zero score, so options that are all over budget still rank by price."""
    src = off if off is not None else entry
    if not src.get("cost_anchors"):
        return 0.0
    if ctx.budget <= 0 and cost[0] == 0:
        return 0.0
    mid = (cost[0] + cost[1]) / 2
    limit = 1.5 * max(ctx.budget, 1.0) * share
    return min(100.0, 5.0 * math.log(mid / limit) / math.log(7.5)) if mid > limit else 0.0


def dims_for(ctx: Ctx, layer: str, entry: dict, off: dict | None, cost: tuple, share: float) -> dict[str, float]:
    r = entry["ratings"]
    d = ctx.d
    lc = r["learning_curve"]
    x = {"beginner": 0.0, "mixed": 0.3, "intermediate": 0.4, "senior": 0.8}.get(G(ctx.spec, "team.experience"), 0.4)
    learn = lc + (5 - lc) * x
    p95 = G(ctx.spec, "requirements.p95_latency_ms")
    need = 2 if p95 is None else 5 if p95 <= 100 else 4 if p95 <= 200 else 3 if p95 <= 500 else 2
    devops = bool(G(ctx.spec, "team.has_devops", False))
    out = {
        "cost": _cost_fit(ctx, entry, off, cost, share),
        "time_to_market": 0.45 * 5 * lang_fit(entry, ctx, layer) + 0.35 * r["dev_speed"] + 0.20 * learn,
        "scalability": 5 - 2.5 * max(0.0, d.req_scale_rating - r["scalability"]),
        "performance": r["performance"] - max(0, need - r["performance"]),
        "maintainability": 0.4 * r["maturity"] + 0.3 * r["ecosystem"] + 0.3 * (5 - r["ops_burden"] * (0.5 if devops else 1.0)),
        "security": float(r["security"]),
    }
    return {k: round(max(0.0, min(5.0, v)), 3) for k, v in out.items()}


def weights(ctx: Ctx) -> dict[str, float]:
    w = dict(ctx.d.weights)
    if ctx.budget_mode:
        w["cost"] = max(5.0, w["cost"] * 2)
    return w


def base_mods(ctx: Ctx, layer: str, entry: dict, off: dict | None) -> list[Mod]:
    spec, d = ctx.spec, ctx.d
    mods: list[Mod] = []
    ptype = G(spec, "project.type")
    good = [g.replace("internal_tools", "internal_tool") for g in entry.get("good_for", [])]
    avoid = [g.replace("internal_tools", "internal_tool") for g in entry.get("avoid_for", [])]
    if ptype in good:
        mods.append(Mod(6, f"suited to project.type {ptype}", ("project.type",)))
    if ptype in avoid:
        mods.append(Mod(-10, f"a poor fit for project.type {ptype}", ("project.type",)))
    for m in d.existing:
        eff = (m["entry"].get("effects") or {}).get(layer)
        mig = m["entry"].get("migration") if m["deprecated"] else None
        if mig and mig["layer"] == layer:
            if mig["prefer"] == entry["id"]:
                mods.append(Mod(float(mig["bonus"]), f"is the supported replacement for the end-of-life \"{m['input']}\"",
                                ("infrastructure.existing_systems",)))
        elif eff and entry["id"] in (eff.get("prefer"), *eff.get("accept", ())):
            if cons.managed_migration(spec, m["entry"], off):
                why = (f"migrates the existing system \"{m['input']}\" to {(off or entry)['name']}, "
                       f"which keeps {m['entry']['migrate']['detail']}")
            else:
                why = eff["reason"].replace("{input}", m["input"]) if eff.get("reason") else f"reuses the existing system \"{m['input']}\""
            mods.append(Mod(float(eff["bonus"]), why, ("infrastructure.existing_systems",)))
    for lic in sorted(d.licences):
        if off and cloud(off["provider"]) == LICENCE_PROVIDER.get(lic) and layer in ("hosting", "database"):
            mods.append(Mod(5, f"the existing licence {lic} is usable with {off['provider']}",
                            ("infrastructure.existing_licences",)))
        elif LICENCE_ENTRY.get(lic) == entry["id"]:
            mods.append(Mod(5, f"the existing licence {lic} covers {entry['name']}",
                            ("infrastructure.existing_licences",)))
    tol = d.lock_in_tolerance
    if tol == "low" and entry.get("lock_in", 0) >= 4:
        mods.append(Mod(-8, f"lock-in {entry['lock_in']} of 5 conflicts with a low vendor lock-in tolerance",
                        ("infrastructure.vendor_lock_in_tolerance",)))
    if tol == "high" and off and off.get("kind") in ("managed", "faas"):
        mods.append(Mod(3, "a managed service fits the high vendor lock-in tolerance",
                        ("infrastructure.vendor_lock_in_tolerance",)))
    r = entry["ratings"]
    if layer in ("frontend", "backend") and lang_fit(entry, ctx, layer) == 0.0:
        mods.append(Mod(-25, f"none of the team's languages ({', '.join(sorted(d.team_langs))}) is used by {entry['name']}",
                        ("team.languages",)))
    if not G(spec, "team.has_devops", False) and r["ops_burden"] >= 4:
        mods.append(Mod(-10, f"operations burden {r['ops_burden']} of 5 with no devops engineer", ("team.has_devops",)))
    if G(spec, "team.experience") == "beginner" and r["learning_curve"] <= 2:
        mods.append(Mod(-8, f"learning curve {r['learning_curve']} of 5 for a beginner team", ("team.experience",)))
    ram = d.min_device_ram_gb
    if layer in ("frontend", "backend") and ram is not None and ram <= 3 and r["resource_efficiency"] <= 2:
        mods.append(Mod(-8, f"heavy on memory while devices have {ram:g} GB RAM", ("requirements.min_device_ram_gb",)))
    if layer in ("frontend", "backend") and d.team_size_12m >= 8:
        mods.append(Mod((r["ecosystem"] - 3) * 2, f"hiring pool matters with {d.team_size_12m} people in 12 months",
                        ("team.size_in_12_months",)))
    return mods


def make(ctx: Ctx, layer: str, entry: dict, off: dict | None, extra: list[Mod], share: float | None = None) -> Scored:
    share = share if share is not None else SHARE.get(layer, SUPPORT_SHARE)
    cost = item_cost(entry, off, ctx.d.users_peak, ctx.commercial)
    # in budget mode and in the over-budget penalties, price as the estimate does: per-GB storage for the database and storage layers
    priced = cost
    per_gb = gb_rate(off, ctx.c.deployment)
    if per_gb and layer in ("database", "storage"):
        vol = float(G(ctx.spec, "scale.data_volume_gb_12_months", 0) or 0)
        g = (1.0 if layer == "database" else 0.6) * vol * per_gb
        priced = (cost[0] + g, cost[1] + g)
    # and extra instances for a given peak_rps on hosts that run always-on instances
    rps = G(ctx.spec, "scale.peak_rps")
    if layer == "hosting" and rps and ctx.d.needs_backend \
            and entry.get("sub_layer") not in ("static", "baas_platform", "serverless"):
        extra_i = math.ceil(float(rps) / ctx.kb.sizing["rps_per_instance"]) - 1
        if extra_i > 0:
            lo_i, hi_i = ctx.kb.sizing["instance_usd_month"]
            priced = (priced[0] + extra_i * lo_i, priced[1] + extra_i * hi_i)
    dims = dims_for(ctx, layer, entry, off, priced if ctx.budget_mode else cost, share)
    w = weights(ctx)
    total = sum(w[k] for k in dims)
    base = 20 * sum(w[k] * dims[k] for k in dims) / total if total else 0.0
    if total:
        base -= (60 if ctx.budget_mode else 20) * w["cost"] * _cost_tail(ctx, entry, off, priced, share) / total
    mods = base_mods(ctx, layer, entry, off) + extra
    if not ctx.budget_mode and ctx.budget > 0 and priced[0] > 0.6 * ctx.budget:
        mods.append(Mod(-25, f"its lowest price (${priced[0]:,.0f} a month) is over 60% of the ${ctx.budget:,.0f} budget",
                        ("budget.monthly_infra_usd",)))
    score = round(base + sum(m.delta for m in mods), 2)
    return Scored(entry["id"], off["id"] if off else None, off["name"] if off else entry["name"], score, dims, mods,
                  None, off["provider"] if off else None, entry, off, None, cost)


def bundled(ctx: Ctx, s: Scored, parent: str) -> Scored:
    """Same product as another pick, so its price is already counted there: rescore at zero cost,
    apart from per-GB database storage in budget mode, which the estimate charges to the database itself."""
    per_gb = gb_rate(s.off, ctx.c.deployment) if ctx.budget_mode else None
    g = float(G(ctx.spec, "scale.data_volume_gb_12_months", 0) or 0) * per_gb if per_gb else 0.0
    share = SHARE["database"]
    dims = {**s.dims, "cost": _cost_fit(ctx, s.entry, s.off, (g, g), share) if g else 5.0}
    w = weights(ctx)
    total = sum(w[k] for k in dims)
    base = 20 * sum(w[k] * dims[k] for k in dims) / total if total else 0.0
    if total and g:
        base -= 60 * w["cost"] * _cost_tail(ctx, s.entry, s.off, (g, g), share) / total
    mods = [m for m in s.mods if m.fields != ("budget.monthly_infra_usd",) or not m.reason.startswith("its lowest price")]
    return replace(s, dims=dims, mods=mods, cost=(0.0, 0.0), score=round(base + sum(m.delta for m in mods), 2),
                   bundled_in=parent)


def add_mod(s: Scored, mod: Mod) -> Scored:
    return replace(s, score=round(s.score + mod.delta, 2), mods=s.mods + [mod])


def self_host_mod(ctx: Ctx, entry: dict, off: dict | None) -> Mod | None:
    """Self-hosted software on a managed-cloud deployment costs patching and uptime work; a stated reason softens it."""
    if not off or off.get("provider") != "self" or ctx.c.deployment != "managed_cloud" \
            or "existing" in off["name"].lower() or entry["id"] in _existing_ids(ctx):
        return None
    if ctx.d.lock_in_tolerance == "low":
        return Mod(-6, "self-hosted, accepted because infrastructure.vendor_lock_in_tolerance is low",
                   ("infrastructure.vendor_lock_in_tolerance", "infrastructure.deployment_model"))
    if G(ctx.spec, "team.has_devops", False):
        return Mod(-12, "self-hosted, so the devops engineer must patch, scale and back it up when a managed option exists",
                   ("infrastructure.deployment_model", "team.has_devops"))
    return Mod(-30, "self-hosted on a managed-cloud deployment with no devops engineer to patch and back it up",
               ("infrastructure.deployment_model", "team.has_devops"))


# candidates

def _existing_ids(ctx: Ctx) -> set[str]:
    return {eff["prefer"] for m in ctx.d.existing for eff in (m["entry"].get("effects") or {}).values()
            if eff.get("prefer")}


def candidates(ctx: Ctx, layer: str, entries=None, pred=None):
    ok: list[tuple[dict, dict | None]] = []
    excluded: list[Excluded] = []
    for e in (entries if entries is not None else ctx.kb.entries(layer)):
        offs = e.get("offerings") or [None]
        reasons = []
        passed = False
        for off in offs:
            if off and "existing" in off["name"].lower() and e["id"] not in _existing_ids(ctx):
                reasons.append(("is only offered when the project already runs it", ("infrastructure.existing_systems",)))
                continue
            cc = (replace(ctx.c, zone=None, cloud_lock=ctx.c.cloud_lock if layer in cons.LOCK_LAYERS else None)
                  if e["id"] in _existing_ids(ctx) else ctx.c)
            good, why, flds = cons.passes(e, off, layer, cc, ctx.kb)
            if good and pred is not None:
                good, why, flds = pred(e, off)
            if good:
                ok.append((e, off))
                passed = True
            else:
                reasons.append((why, flds))
        if not passed and reasons:
            excluded.append(Excluded(e["id"], e["name"], reasons[0][0], tuple(reasons[0][1])))
    excluded.sort(key=lambda x: (-ctx.kb.entry(layer, x.entry_id)["ratings"]["ecosystem"], x.entry_id))
    return ok, excluded


def pick_from(layer: str, scored: list[Scored], excluded: list[Excluded], sub_layer=None, alts: int = 2) -> Pick:
    scored = sorted(scored, key=_rank)
    if not scored:
        raise NoCandidate(layer, excluded)
    best = scored[0]
    ru, seen = [], {best.entry_id}
    for s in scored[1:]:
        if s.entry_id in seen:
            continue
        seen.add(s.entry_id)
        ru.append(replace(s, vs=best))
        if len(ru) == alts:
            break
    return Pick(layer, sub_layer, best, runners_up=ru, excluded=excluded[:3])


# frontend

def _front_roles(ctx: Ctx):
    p = ctx.d.platforms
    roles = []
    if "web" in p:
        roles.append("web")
    mob = sorted(p & set(MOBILE))
    if mob:
        roles.append("mobile")
    if "desktop" in p:
        roles.append("desktop")
    return roles, mob


def game_client(ctx: Ctx) -> bool:
    """A gaming project whose description says the game itself runs on the user's device."""
    if G(ctx.spec, "project.type") != "gaming" or not (ctx.d.platforms & {"android", "ios", "desktop"}):
        return False
    text = str(G(ctx.spec, "project.description", ""))
    return bool(GAMEPLAY_WORDS.search(text)) and not GAME_SERVICE_WORDS.search(text)


def _front_mods(ctx: Ctx, e: dict, roles: set[str], mob: list[str]) -> list[Mod]:
    spec, d = ctx.spec, ctx.d
    mods = []
    sub = e.get("sub_layer")
    if game_client(ctx):
        if sub == "game_engine":
            mods.append(Mod(40, "a real-time game client needs an engine with its own render, physics and input loop",
                            ("project.type", "project.description")))
            if e["id"] == "unity" and "csharp" in d.team_langs:
                mods.append(Mod(4, "keeps the team's C# and has the largest engine ecosystem and multiplayer tooling",
                                ("team.languages",)))
            if e["id"] == "godot" and weights(ctx)["cost"] >= max(weights(ctx).values()):
                mods.append(Mod(6, "free MIT licence with no revenue threshold suits a cost-first project",
                                ("priorities.cost",)))
        else:
            mods.append(Mod(-40, "renders UI widgets, not a real-time game loop", ("project.type", "project.description")))
    seo = bool(G(spec, "requirements.seo_critical", False))
    if "web" in roles and seo:
        if has(e, "ssr") or has(e, "ssg"):
            mods.append(Mod(8, "server rendering or pre-built pages serve search engines", ("requirements.seo_critical",)))
        elif sub in ("web_spa", "mobile_cross", "web_mpa") and not has(e, "ssr"):
            mods.append(Mod(-15, "client-side rendering is weak for search engines", ("requirements.seo_critical",)))
    if "web" in roles and not d.needs_backend and sub == "web_static":
        mods.append(Mod(10, "content that rarely changes is best pre-built as static files",
                        ("project.type", "scale.workload")))
    if "web" in roles and d.needs_backend and sub == "web_static":
        mods.append(Mod(-30, "pre-built pages cannot serve signed-in, interactive screens", ("project.type", "requirements.auth")))
    if "web" in roles and d.needs_backend and sub == "web_spa" and not seo and G(spec, "requirements.auth", "none") != "none":
        mods.append(Mod(8, "the screens sit behind sign-in and need no search indexing, so a client-rendered app served as "
                        "static files is enough and there is no rendering server to run",
                        ("requirements.seo_critical", "requirements.auth")))
    fits = [g.replace("internal_tools", "internal_tool") for g in e.get("good_for", [])]
    if "web" in roles and (sub or "").startswith("web_") and e["ratings"]["learning_curve"] <= 2 \
            and G(spec, "project.type") not in fits:
        mods.append(Mod(-6, f"learning curve {e['ratings']['learning_curve']} of 5 and a structure aimed at large "
                        "enterprise codebases, which this kind of project does not need", ("project.type",)))
    shared = set(e.get("languages") or []) & set(d.team_langs)
    weeks = G(spec, "timeline.mvp_weeks")
    if "web" in roles and d.needs_backend and weeks is not None and weeks <= 8 and e["ratings"]["ecosystem"] >= 4:
        mods.append(Mod((e["ratings"]["ecosystem"] - 3) * 2.5, f"{article(weeks)} {weeks}-week MVP leans on a large library ecosystem",
                        ("timeline.mvp_weeks",)))
    if "web" in roles and d.collab and e["ratings"]["ecosystem"] >= 5:
        mods.append(Mod(8, "largest ecosystem of canvas and shared-editor libraries for a collaborative app",
                        ("project.type", "requirements.realtime")))
    if "web" in roles and d.collab and sub in ("web_mpa", "web_ssr") and not has(e, "spa"):
        mods.append(Mod(-45, "server-rendered pages cannot drive a live shared canvas or cursors",
                        ("project.type", "requirements.realtime")))
    if shared and len(e.get("languages") or []) <= 3:
        mods.append(Mod(5, f"written in {', '.join(sorted(shared))}, which the team already uses", ("team.languages",)))
    if "mobile" in roles and sub == "mobile_cross" and d.team_langs and not shared \
            and e["ratings"]["learning_curve"] <= 2 and G(spec, "project.type") not in e.get("good_for", []):
        mods.append(Mod(-8, f"learning curve {e['ratings']['learning_curve']} of 5 and no team language "
                        f"({', '.join(sorted(d.team_langs))}) in common", ("team.languages",)))
    offline = bool(G(spec, "requirements.offline_support", False))
    if offline and "mobile" in roles:
        if has(e, "offline_store"):
            mods.append(Mod(6, "supports an on-device store for offline use", ("requirements.offline_support",)))
        else:
            mods.append(Mod(-10, "no built-in on-device store for offline use", ("requirements.offline_support",)))
    if offline and "web" in roles and not mob and has(e, "spa"):
        mods.append(Mod(6, "can be installed and work offline as a progressive web app with a service worker; browser "
                        "storage can be evicted, so offline guarantees are limited", ("requirements.offline_support",)))
    if d.connectivity in ("intermittent", "poor") and has(e, "lightweight_payload"):
        mods.append(Mod(3, f"small payloads suit {d.connectivity} connectivity", ("requirements.connectivity",)))
    if "mobile" in roles:
        if len(mob) == 2 and sub == "mobile_cross":
            mods.append(Mod(8, "one codebase covers both Android and iOS", ("project.platforms",)))
        elif len(mob) == 1 and sub == "mobile_native" and set(e.get("languages", [])) & set(d.team_langs):
            mods.append(Mod(5, f"native {mob[0]} app in a language the team knows", ("project.platforms", "team.languages")))
    return mods


def _parts_name(parts: list[Scored], roles_of: list[str]) -> str:
    return " + ".join(f"{p.name} ({r})" for p, r in zip(parts, roles_of))


def _front_label(role: str, mob: list[str]) -> str:
    if role == "mobile":
        return ", ".join({"android": "Android", "ios": "iOS"}[m] for m in mob)
    return role


def _platform_text(roles: list[str], mob: list[str]) -> str:
    names = [x for r in roles for x in _front_label(r, mob).split(", ")]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def select_frontend(ctx: Ctx) -> Pick:
    roles, mob = _front_roles(ctx)
    if not roles:
        return Pick("frontend", none_reason="Only an API is requested (project.platforms is api_only), so no client is needed.",
                    none_fields=("project.platforms",))
    plats = ctx.d.platforms
    gc = game_client(ctx)
    entries = [e for e in ctx.kb.entries("frontend") if gc or e.get("sub_layer") != "game_engine"]
    excluded_all: list[Excluded] = []

    def group(role):
        def pred(e, off):
            sub = e.get("sub_layer")
            if role == "web":
                okk = sub in ("web_static", "web_ssr", "web_spa", "web_mpa")
                if okk and not ctx.d.needs_backend:
                    okk = "static" in e.get("runs_on", [])
            elif role == "mobile":
                okk = sub in ("mobile_cross", "mobile_native", "game_engine") and set(mob) <= set(e["platforms"])
            else:
                okk = sub == "desktop" or (sub in ("mobile_cross", "game_engine") and "desktop" in e["platforms"])
            return (okk, "does not cover the required platform", ("project.platforms",))
        return pred

    scored_by_role: dict[str, list[Scored]] = {}
    for role in roles:
        ok, ex = candidates(ctx, "frontend", entries, group(role))
        excluded_all += [x for x in ex if x.reason != "does not cover the required platform"]
        scored_by_role[role] = sorted((make(ctx, "frontend", e, None, _front_mods(ctx, e, {role}, mob)) for e, _ in ok), key=_rank)
        if not scored_by_role[role]:
            raise NoCandidate("frontend", excluded_all)

    combos: list[Scored] = []
    parts_of: dict[int, list[Scored]] = {}
    if len(roles) == 1:
        combos = scored_by_role[roles[0]]
    else:
        for parts in itertools.product(*[scored_by_role[r][:3] for r in roles]):
            fams = [set(p.entry.get("languages", [])) for p in parts]
            same = len(fams) >= 2 and all(f & JS_FAMILY for f in fams[:2]) or (fams[0] & fams[1])
            pair = f"{roles[0]} and {roles[1]} parts"
            shared = [Mod(5, f"{pair} share a language, so types and code can be shared", ("team.languages",))] if same else []
            lib = parts[0].entry.get("ui_library")
            if lib and lib == parts[1].entry.get("ui_library"):
                shared.append(Mod(5, f"{pair} are both built on {lib}, so component logic, state handling and the API "
                                  "client are written once", ("project.platforms",)))
            mean = sum(p.score for p in parts) / len(parts)
            mods = [m for p in parts for m in p.mods] + shared
            dims = {k: round(sum(p.dims[k] for p in parts) / len(parts), 3) for k in parts[0].dims}
            name = _parts_name(list(parts), [_front_label(r, mob) for r in roles])
            ids = "+".join(p.entry_id for p in parts)
            sc = Scored(ids, None, name, round(mean + sum(m.delta for m in shared), 2), dims, mods, None, None,
                        parts[0].entry, None, None, (0.0, 0.0))
            combos.append(sc)
            parts_of[id(sc)] = list(parts)
        singles = []
        for e in entries:
            if e.get("sub_layer") not in ("mobile_cross", "game_engine"):
                continue
            if not set(p for p in plats if p != "api_only") <= set(e["platforms"]):
                continue
            ok, _ = candidates(ctx, "frontend", [e])
            if not ok:
                continue
            covers = _platform_text(roles, mob)
            if "web" in roles and "mobile" in roles and not has(e, "spa"):
                # a mobile toolkit's browser build is a secondary target, so the web app gets its own framework
                one = Mod(-12, "its browser build is a secondary target of a mobile toolkit, behind a web framework in "
                          "routing, bundle size and accessibility", ("project.platforms",))
            else:
                one = Mod(6, f"a single codebase covers {covers}", ("project.platforms",))
            s = make(ctx, "frontend", e, None, _front_mods(ctx, e, set(roles), mob) + [one])
            singles.append(replace(s, name=f"{s.name} (one codebase for {covers})"))
        combos += singles
    combos = sorted(combos, key=_rank)
    best = combos[0]
    ru, seen = [], {best.entry_id}
    for s in combos[1:]:
        if s.entry_id not in seen:
            seen.add(s.entry_id)
            ru.append(replace(s, vs=best))
            if len(ru) == 2:
                break
    # the single-codebase option is always shown next to a web + mobile pair, and the reverse
    single_ids = {s.entry_id for s in combos if id(s) not in parts_of} if len(roles) > 1 else set()
    is_single = best.entry_id in single_ids
    other = next((s for s in combos if (s.entry_id in single_ids) != is_single), None) if single_ids else None
    if other is not None and all((r.entry_id in single_ids) == is_single for r in ru):
        ru = ru[:1] + [replace(other, vs=best)]
    parts = parts_of.get(id(best), [])
    return Pick("frontend", chosen=best, runners_up=ru, excluded=excluded_all[:3], parts=parts)


def web_part(front: Pick, want_web: bool = True) -> dict | None:
    """Entry of the web-facing part of the chosen frontend, or None."""
    parts = front.parts or ([front.chosen] if front.chosen else [])
    for p in parts:
        e = p.entry
        if e.get("sub_layer", "").startswith("web_"):
            return e
    for p in parts:
        if want_web and p.entry.get("sub_layer") == "mobile_cross" and "web" in p.entry["platforms"] and len(parts) == 1:
            return p.entry
    return None


def web_tags(ctx: Ctx, e: dict) -> set[str]:
    if e.get("sub_layer") == "mobile_cross":
        return {"static"}
    tags = _tags(e)
    if e.get("sub_layer") in ("web_ssr", "web_mpa") and ctx.d.needs_backend:
        tags.discard("static")
    return tags


# backend + database + hosting

def _norm_langs(langs) -> set[str]:
    """Language names with JavaScript and TypeScript treated as one language."""
    return {"typescript" if x in JS_FAMILY else x for x in (langs or [])}


def _backend_mods(ctx: Ctx, e: dict, front_web: dict | None, front_langs: set[str] | None = None) -> list[Mod]:
    spec = ctx.spec
    mods = []
    same = _norm_langs(e.get("languages")) & (front_langs or set())
    if same:
        lang = ", ".join(sorted(same))
        if not ctx.d.team_langs:
            mods.append(Mod(10, f"team.languages is empty, so {lang} like the frontend keeps the whole app in one language",
                            ("team.languages",)))
        elif G(spec, "team.size", 1) <= 3:
            team = _norm_langs(ctx.d.team_langs)
            # a platform that also runs another team language gives no reason to match the frontend
            if same <= team and not (_norm_langs(e.get("languages")) & team) - same:
                names = ", ".join(sorted(l for l in ctx.d.team_langs if _norm_langs([l]) & same))
                mods.append(Mod(4, f"{names} like the frontend, so a team of {G(spec, 'team.size', 1)} works in one language",
                                ("team.size",)))
    if G(spec, "requirements.realtime", False) and any(c.startswith("websocket") or c == "bundled_realtime" or c == "signalr"
                                                       for c in e.get("capabilities", [])):
        mods.append(Mod(5, "has built-in real-time support", ("requirements.realtime",)))
    p95 = G(spec, "requirements.p95_latency_ms")
    if p95 is not None and p95 <= 100 and has(e, "cold_start"):
        mods.append(Mod(-10, f"cold starts break a p95 target of {p95} ms", ("requirements.p95_latency_ms",)))
    if ctx.arch.style == "serverless" and e.get("sub_layer") in ("baas", "faas"):
        mods.append(Mod(8, "fits a serverless architecture with no servers to run", ("team.has_devops", "scale.traffic_pattern")))
    if e.get("sub_layer") == "baas" and not G(spec, "team.has_devops", False) and G(spec, "team.size", 1) <= 3:
        need = [n for n, on in (("sign-in", G(spec, "requirements.auth", "none") != "none"),
                                ("file storage", G(spec, "requirements.file_media_storage", False)),
                                ("real-time", G(spec, "requirements.realtime", False))) if on]
        if need:
            mods.append(Mod(3 * len(need), f"includes {', '.join(need)} out of the box, so fewer services for a team of "
                            f"{G(spec, 'team.size', 1)} to wire up", ("team.size", "team.has_devops")))
    if e["id"] == "nextjs_routes" and front_web and front_web["id"] == "nextjs":
        mods.append(Mod(8, "the API lives in the same Next.js app as the web frontend", ("project.platforms",)))
    return mods


NOSQL_WORDS = re.compile(r"key[- ]value|time[- ]series|event stream|document (?:store|database)|nosql|wide[- ]column", re.I)


ANALYTICS_STORE_GB = 1000


def _nosql_signal(ctx: Ctx) -> str | None:
    """The stated input that justifies a key-value, document or time-series store, or None."""
    spec, d = ctx.spec, ctx.d
    ptype = G(spec, "project.type")
    if ptype in ("iot", "gaming"):
        return f"project.type {ptype}"
    if G(spec, "scale.workload") == "write_heavy" and d.req_scale_rating >= 4:
        return "a write-heavy workload at very high scale"
    if d.peak_rps >= 5000:
        return f"a peak of {d.peak_rps:,.0f} requests per second"
    m = NOSQL_WORDS.search(str(G(spec, "project.description", "")))
    return f"the description mentions '{m.group(0).lower()}'" if m else None


def _db_mods(ctx: Ctx, e: dict, off: dict | None) -> list[Mod]:
    spec, d = ctx.spec, ctx.d
    mods = []
    if d.consistency == "strong" and not has(e, "acid"):
        mods.append(Mod(-20, "no multi-row transactions, but strong consistency is needed",
                        ("requirements.consistency", "requirements.payments")))
    avail = float(G(spec, "requirements.availability", "99.9"))
    if off and avail >= 99.95 and (not off.get("ha") or (off.get("sla") or 0) < avail):
        mods.append(Mod(-10, f"no {avail:g}% availability guarantee", ("requirements.availability",)))
    elif off and d.rpo_minutes <= 15 and not off.get("ha"):
        mods.append(Mod(-10, f"no replica or point-in-time recovery, but at most {d.rpo_minutes} minutes of data may be lost",
                        ("requirements.rpo_minutes",)))
    if G(spec, "requirements.offline_support", False) and e.get("sub_layer") != "relational" and e.get("sub_layer") != "embedded":
        mods.append(Mod(-8, "device-side SQLite tables sync most simply to relational tables", ("requirements.offline_support",)))
    if "sql" in d.team_langs and e.get("sub_layer") == "relational":
        mods.append(Mod(3, "the team already knows SQL", ("team.languages",)))
    if d.geo and has(e, "postgis"):
        mods.append(Mod(2, "location queries are supported (PostGIS)", ("project.description",)))
    if G(spec, "requirements.ml_features") == "llm_rag" and has(e, "pgvector"):
        mods.append(Mod(3, "vector search without a separate system (pgvector)", ("requirements.ml_features",)))
    if G(spec, "requirements.full_text_search", False) and has(e, "fts"):
        mods.append(Mod(2, "built-in full-text search", ("requirements.full_text_search",)))
    sub = e.get("sub_layer")
    ptype = G(spec, "project.type")
    heavy = G(spec, "scale.workload") == "write_heavy"
    signal = _nosql_signal(ctx)
    vol = float(G(spec, "scale.data_volume_gb_12_months", 0) or 0)
    if sub in ("relational", "newsql"):
        why = ("requirements.consistency is strong" if d.consistency == "strong"
               else f"project.type {ptype} is typical relational CRUD" if ptype in RELATIONAL_TYPES
               else "the input has no key-value, document or very-high-scale signal")
        columnar = ptype == "data_analytics" and vol >= ANALYTICS_STORE_GB and d.consistency != "strong"
        if (not signal or d.consistency == "strong" or ptype in RELATIONAL_TYPES) and not columnar:
            mods.append(Mod(4, f"a relational database suits the data: {why}", ("requirements.consistency", "project.type")))
    elif sub == "analytics" and ptype == "data_analytics" and vol >= ANALYTICS_STORE_GB:
        mods.append(Mod(8, f"columnar storage scans {vol:,.0f} GB of history for dashboards faster than a row store",
                        ("project.type", "scale.data_volume_gb_12_months")))
    elif sub != "embedded" and e["id"] not in _existing_ids(ctx):
        an = "an" if sub[:1] in "aeiou" else "a"
        if signal:
            mods.append(Mod(4, f"{signal} favours {an} {sub} store", ("project.type", "scale.workload", "project.description")))
        else:
            mods.append(Mod(-14, f"{an} {sub} store has no matching signal (iot, gaming, very high write scale or a "
                            "key-value or time-series need in the description)",
                            ("requirements.consistency", "project.type", "scale.workload")))
    if ptype == "data_analytics" and sub == "document" and e["id"] not in _existing_ids(ctx):
        mods.append(Mod(-15, "dashboard tools work best with SQL joins and aggregations, which a document store "
                        "offers only partly", ("project.type",)))
    if ptype == "iot" and heavy and sub == "timeseries":
        mods.append(Mod(8, "a time-series database is built for write-heavy sensor data", ("project.type", "scale.workload")))
    if e.get("sub_layer") == "embedded" and off and off.get("kind") == "self" and d.users_peak > 100_000:
        mods.append(Mod(-8, "a single-file database is a limit at this user count", ("scale.users_12_months",)))
    return mods


def _host_mods(ctx: Ctx, e: dict, off: dict | None) -> list[Mod]:
    spec, d = ctx.spec, ctx.d
    mods = []
    avail = float(G(spec, "requirements.availability", "99.9"))
    if off and avail >= 99.95 and (not off.get("ha") or (off.get("sla") or 0) < avail):
        mods.append(Mod(-10, f"no {avail:g}% availability guarantee", ("requirements.availability",)))
    soft = (off or {}).get("soft_limit_users")
    if soft and d.users_peak > soft:
        mods.append(Mod(-40, f"its bandwidth soft limit fits about {soft:,} users, not {d.users_peak:,.0f}",
                        ("scale.users_12_months",)))
    p95 = G(spec, "requirements.p95_latency_ms")
    if p95 is not None and p95 <= 100 and has(e, "faas"):
        mods.append(Mod(-10, f"function cold starts break a p95 target of {p95} ms", ("requirements.p95_latency_ms",)))
    if ctx.arch.style == "serverless" and (has(e, "autoscale_to_zero") or has(e, "faas")):
        mods.append(Mod(4, "scales to zero between traffic peaks", ("scale.traffic_pattern",)))
    if ctx.arch.style == "static" and has(e, "static_hosting"):
        mods.append(Mod(6, "built for static files with a global CDN", ("project.type",)))
    if not G(spec, "team.has_devops", False) and has(e, "git_deploy"):
        mods.append(Mod(3, "deploys straight from git, no pipeline to maintain", ("team.has_devops",)))
    if ctx.c.zone is None and off and not (off.get("regions") or {}).get(zone_of(ctx)) and off.get("kind") != "self" \
            and not off.get("edge_not_regional"):
        mods.append(Mod(-4, f"no region close to the {zone_of(ctx)} users", ("scale.user_geography",)))
    return mods


def _self_list(ctx: Ctx, entry: dict, off: dict | None) -> list[Mod]:
    m = self_host_mod(ctx, entry, off)
    return [m] if m else []


SINGLE_WRITER_RPS = 100
EMBEDDED_MAX_USERS = 5000
CONCURRENT_WRITE_TYPES = ("ecommerce", "fintech", "marketplace")


def _embedded_unfit(ctx: Ctx) -> str | None:
    """Why a single-writer embedded database cannot be the server database, or None when it can."""
    d = ctx.d
    if not d.platforms & {"web", "api_only"}:
        return None
    if d.peak_rps > SINGLE_WRITER_RPS:
        return (f"a single-writer file database cannot take {d.peak_rps:,.0f} requests per second "
                f"(about {SINGLE_WRITER_RPS} is its limit with writes queued behind one writer)")
    if d.users_peak > EMBEDDED_MAX_USERS:
        return f"a single-writer file database does not suit {d.users_peak:,.0f} users on a shared server"
    ptype = G(ctx.spec, "project.type")
    if ptype in CONCURRENT_WRITE_TYPES:
        return f"a {ptype} backend needs transactions from many concurrent writers, and SQLite has one writer"
    if ptype == "data_analytics":
        return ("a dashboard tool and bulk data loads need a database server they can reach over the network, "
                "not a file inside one application process")
    return None


def _runs(host: dict, tags: set[str]) -> bool:
    return bool(set(host.get("runs_on", [])) & tags)


def select_core(ctx: Ctx, front: Pick) -> tuple[Pick, Pick, Pick]:
    d, c = ctx.d, ctx.c
    c.k8s_allowed = (ctx.arch.style in ("service_oriented", "microservices")
                     and bool(G(ctx.spec, "team.has_devops", False)) and G(ctx.spec, "team.size", 1) >= 8)
    w_entry = web_part(front, "web" in d.platforms)
    w_tags = web_tags(ctx, w_entry) if w_entry else None
    front_langs = _norm_langs(l for p in (front.parts or [front.chosen]) if p is not None
                              for l in (p.entry.get("languages") or []))

    # hosting candidates
    h_ok, h_ex = candidates(ctx, "hosting")
    hosts = [make(ctx, "hosting", e, o, _host_mods(ctx, e, o) + _self_list(ctx, e, o)) for e, o in h_ok]
    if w_tags is not None and "static" not in w_tags:
        # a server-rendered web app needs a host that runs it natively, not through add-on functions
        weak = Mod(-25, "renders server-side pages only through add-on functions, not natively", ("project.platforms",))
        hosts = [add_mod(h, weak) if has(h.entry, "ssr_hosting_via_functions") and not has(h.entry, "ssr_hosting")
                 else h for h in hosts]
    if not d.needs_backend:
        if not w_entry:
            reason = "The app runs entirely on the device, so there is no server to host."
            return (Pick("backend", none_reason=reason, none_fields=("project.platforms", "requirements.auth")),
                    Pick("database", none_reason="No stored user data is needed, so there is no database.",
                         none_fields=("requirements.auth", "requirements.payments")),
                    Pick("hosting", none_reason=reason, none_fields=("project.platforms",)))
        sh = [h for h in hosts if _runs(h.entry, w_tags)]
        hp = pick_from("hosting", sh, h_ex)
        return (Pick("backend", none_reason="No server-side logic is needed: the site is pre-built and served as static files.",
                     none_fields=("project.type", "requirements.auth", "requirements.payments")),
                Pick("database", none_reason="No stored user data; content lives in Git.",
                     none_fields=("requirements.auth", "requirements.payments")),
                hp)

    # backend candidates
    def bpred(e, off):
        if e.get("sub_layer") == "game_server" and G(ctx.spec, "project.type") != "gaming":
            return False, "a game server is only for gaming projects", ("project.type",)
        if e["id"] == "nextjs_routes" and not (w_entry and w_entry["id"] == "nextjs"):
            return False, "its API routes need Next.js as the web frontend", ("project.platforms",)
        if e.get("sub_layer") == "baas" and (e.get("bundled") or {}).get("db") and \
                not ctx.kb.entry("database", BAAS_DB.get((e["bundled"] or {}).get("db"), e["bundled"]["db"])):
            return False, "its bundled database is not in the knowledge base", ()
        if e.get("sub_layer") == "faas" and G(ctx.spec, "project.type") == "data_analytics":
            return False, ("bulk data loads and a self-run dashboard tool need a long-running process, "
                           "while each function run is capped at minutes"), ("project.type",)
        return True, "", ()

    b_ok, b_ex = candidates(ctx, "backend", None, bpred)
    backends = [(e, o, make(ctx, "backend", e, o, _backend_mods(ctx, e, w_entry, front_langs) + _self_list(ctx, e, o))) for e, o in b_ok]

    need_db = d.needs_database
    def dpred(e, off):
        why = _embedded_unfit(ctx) if e.get("sub_layer") == "embedded" else None
        if G(ctx.spec, "project.type") == "data_analytics" and e.get("sub_layer") in ("keyvalue", "widecolumn") \
                and e["id"] not in _existing_ids(ctx):
            return (False, "none of the dashboard tools in the knowledge base connects to a key-value or wide-column "
                    "store directly, so the data would have to be copied into a SQL store first", ("project.type",))
        return (False, why, ("scale.peak_rps", "scale.users_12_months", "project.type")) if why else (True, "", ())

    d_ok, d_ex = candidates(ctx, "database", None, dpred) if need_db else ([], [])
    dbs = [make(ctx, "database", e, o, _db_mods(ctx, e, o) + _self_list(ctx, e, o)) for e, o in d_ok]
    if need_db and not dbs:
        raise NoCandidate("database", d_ex)

    small_team = G(ctx.spec, "team.size", 1) <= 3 or not G(ctx.spec, "team.has_devops", False)
    whosts = [h for h in hosts if w_tags is not None and _runs(h.entry, w_tags)]
    def best_by_cloud(pool: list[Scored]) -> dict[str, Scored]:
        out: dict[str, Scored] = {}
        for h in pool:
            cl = cloud(h.provider)
            if cl not in out or _rank(h) < _rank(out[cl]):
                out[cl] = h
        return out

    # a functions or BaaS API has no servers, so its web build goes to a platform host, not a container service
    platform_w = [h for h in whosts if has(h.entry, "static_hosting")
                  and ("static" in w_tags or has(h.entry, "ssr_hosting") or not has(h.entry, "ssr_hosting_via_functions"))] \
        or whosts
    best_w, best_w_pf = best_by_cloud(whosts), best_by_cloud(platform_w)

    def web_hosts_for(h: Scored | None, serverless: bool = False) -> list[Scored]:
        """Web hosts worth combining with an API host: the best one on the same provider and the best overall."""
        pool, by_cloud = (platform_w, best_w_pf) if serverless else (whosts, best_w)
        if not pool:
            # no static host qualifies; a container host can serve the web build itself
            return [h] if h is not None and "container" in h.entry.get("runs_on", []) else []
        top = min(pool, key=_rank)
        out = [top]
        same = by_cloud.get(cloud(h.provider)) if h is not None else None
        if same is not None and same.entry_id != top.entry_id:
            out.insert(0, add_mod(same, Mod(4, "same provider as the API host", ("infrastructure.cloud_preference",))))
        return out

    def affinity(db: Scored, h: Scored) -> float:
        a = 0.0
        if db.provider and h.provider and cloud(db.provider) == cloud(h.provider) and db.provider != "self":
            a += 4
        if db.entry["id"] == "sqlite" and db.provider == "self" and h.entry.get("sub_layer") not in ("vm", "self", "paas"):
            a -= 100
        # a self-run database server needs a machine or cluster in the stack to run on
        if db.provider == "self" and db.entry.get("sub_layer") != "embedded" and "existing" not in db.off["name"].lower() \
                and db.entry_id not in _existing_ids(ctx) and not (has(h.entry, "vm") or has(h.entry, "k8s")):
            a -= 100
        if db.provider and h.provider and db.provider == h.provider and db.provider != "self":
            r1 = region_of(db.off, ctx)
            if r1 and r1 == region_of(h.off, ctx):
                a += 2
        return a

    best_b: dict[tuple, tuple] = {}
    best_db: dict[str, tuple] = {}
    best_h: dict[str, tuple] = {}

    hyper = {k for k, r in ctx.kb.providers.items() if r.get("kind") == "hyperscaler"}

    def coherence(b, db, hostparts):
        """Cloud-specific services need one hyperscaler; every extra vendor is one more account to run."""
        comps = [x for x in (*hostparts, b, db) if x is not None]
        clouds = {cloud(x.provider) for x in comps if x.provider} & hyper
        vendors = {vendor_of(x.provider) for x in comps} - {None}
        data_clouds = {cloud(x.provider) for x in (b, db) if x is not None and x.provider} & hyper
        host_clouds = {cloud(x.provider) for x in hostparts if x.provider}
        off_cloud = len(clouds) > 1 or bool(data_clouds - host_clouds)
        return (CLOUD_MISMATCH if off_cloud else 0) - (10 if small_team else 5) * max(0, len(vendors) - 1)

    def consider(total, b, db, hostparts):
        total += coherence(b, db, hostparts)
        rk = (-round(total, 2), _key(b) if b else (), _key(db) if db else (), _key(hostparts[0]))
        t = (rk, total, b, db, hostparts)
        for table, k in ((best_b, (b.entry_id, b.offering_id) if b else None),
                         (best_db, db.entry_id if db else None), (best_h, hostparts[0].entry_id)):
            if k is not None and (k not in table or rk < table[k][0]):
                table[k] = t
        return t

    winner = None

    def keep(t):
        nonlocal winner
        if winner is None or t[0] < winner[0]:
            winner = t

    for e, off, bs in backends:
        bd = e.get("bundled") or {}
        sub = e.get("sub_layer")
        managed_baas = sub == "baas" and off["kind"] == "managed"
        if managed_baas:
            dbe = BAAS_DB.get(bd.get("db"), bd.get("db"))
            fixed = [x for x in dbs if x.entry_id == dbe and cloud(x.provider) == cloud(off["provider"])]
            api_hosts = [h for h in hosts if cloud(h.provider) == cloud(off["provider"]) and has(h.entry, "baas_platform")
                         or (h.provider == off["provider"] and "baas" in h.entry.get("runs_on", []))
                         or (cloud(h.provider) == cloud(off["provider"]) and h.entry["id"] == "firebase_hosting")]
            if not (fixed or not need_db) or not api_hosts:
                continue
            ah = min(api_hosts, key=_rank)
            dbx = add_mod(fixed[0], Mod(8, f"bundled with {e['name']}", ("requirements.auth",))) if fixed else None
            if dbx is not None:
                dbx = bundled(ctx, dbx, off["name"])
            ahz = bundled(ctx, ah, off["name"])
            if w_entry is not None and not _runs(ah.entry, w_tags):
                hps = [[wh, ahz] for wh in web_hosts_for(ah)]
            else:
                hps = [[ahz]]
            for hp in hps:
                total = bs.score + (dbx.score if dbx else 0) + hp[0].score
                keep(consider(total, bs, dbx, hp))
            continue
        tags = _tags(e)
        if sub == "faas":
            bs = replace(bs, cost=(0.0, 0.0))
        if sub == "baas":
            tags = {"container"}
        selfdb = None
        if sub == "baas" and bd.get("db"):
            dbe = BAAS_DB.get(bd["db"], bd["db"])
            selfdb = [x for x in dbs if x.entry_id == dbe and x.provider == "self"]
            if not selfdb:
                continue
        # a long-lived server needs a host that runs it; static and Jamstack hosts only run functions
        server = sub not in ("faas", "baas") and e["id"] != "nextjs_routes"
        use_h = [h for h in hosts if _runs(h.entry, tags)
                 and (h.entry.get("sub_layer") != "static" or (e["id"] == "nextjs_routes" and has(h.entry, "ssr_hosting")))
                 and not (server and has(h.entry, "static_hosting") and "container" not in h.entry.get("runs_on", []))]
        use_d = selfdb if selfdb is not None else (dbs if need_db else [None])
        for h in use_h:
            for db in use_d:
                total = bs.score + h.score
                if db is not None:
                    total += db.score + affinity(db, h)
                    if selfdb is not None:
                        db = bundled(ctx, add_mod(db, Mod(8, f"bundled with {e['name']}", ("requirements.auth",))),
                                     off["name"] if off else e["name"])
                        total += 8
                if w_entry is not None and not _runs(h.entry, w_tags):
                    for wh in web_hosts_for(h, sub == "faas"):
                        hp = [h] if wh is h else [wh, h]
                        keep(consider(total - h.score + (wh.score + h.score) / 2, bs, db, hp))
                else:
                    keep(consider(total, bs, db, [h]))

    if winner is None:
        raise NoCandidate("hosting", h_ex + b_ex)

    _, _, wb, wdb, whp = winner

    win = winner

    def alts(table, win_id, getter, n=2):
        out, seen = [], {win_id}
        for k, t in sorted(table.items(), key=lambda kv: kv[1][0]):
            s = getter(t)
            if s is None or s.entry_id in seen:
                continue
            seen.add(s.entry_id)
            out.append(replace(s, stack_gap=round(win[1] - t[1], 2)))
            if len(out) == n:
                break
        return out

    b_alts = [replace(x, vs=wb) for x in alts(best_b, wb.entry_id, lambda t: t[2])]
    # a language the team writes but no listed backend uses gets its best stack shown, so the trade is visible
    covered = {lang for x in [wb, *b_alts] for lang in x.entry.get("languages") or []}
    seen_b = {x.entry_id for x in [wb, *b_alts]}
    for lang in [str(x).lower() for x in G(ctx.spec, "team.languages", []) or []]:
        if lang in covered or lang == "sql":  # SQL is a query language, not what a backend is written in
            continue
        t = min((t for t in best_b.values() if t[2].entry_id not in seen_b and lang in (t[2].entry.get("languages") or [])),
                key=lambda t: t[0], default=None)
        if t is not None:
            b_alts.append(replace(t[2], vs=wb, stack_gap=round(win[1] - t[1], 2), team_lang=lang))
            break
    d_alts = [replace(x, vs=wdb) for x in alts(best_db, wdb.entry_id, lambda t: t[3])] if wdb else []
    main_h = whp[0]
    h_alts = [replace(x, vs=main_h) for x in alts(best_h, main_h.entry_id, lambda t: t[4][0])]
    backend = Pick("backend", sub_layer=wb.entry.get("sub_layer"), chosen=wb, runners_up=b_alts, excluded=b_ex[:3])
    if need_db:
        database = Pick("database", sub_layer=wdb.entry.get("sub_layer"), chosen=wdb, runners_up=d_alts, excluded=d_ex[:3])
    else:
        database = Pick("database", none_reason="The app stores no user data (it only serves model predictions or relays requests).",
                        none_fields=("requirements.ml_features",))
    if len(whp) > 1:
        api, api_tags = whp[-1], _tags(wb.entry)
        pool = sorted((h for h in hosts if _runs(h.entry, api_tags) and h.entry.get("sub_layer") != "static"
                       and h.entry_id != api.entry_id and cloud(h.provider) == cloud(api.provider)), key=_rank)
        h_alts += [replace(x, name=f"{x.name} (API host)", vs=api) for x in pool[:2]]
    hosting = Pick("hosting", sub_layer=main_h.entry.get("sub_layer"), chosen=main_h, runners_up=h_alts,
                   excluded=h_ex[:3], parts=list(whp) if len(whp) > 1 else [])
    server = wb.entry.get("sub_layer") not in ("faas", "baas") and wb.entry_id != "nextjs_routes"
    if len(whp) == 1 and w_entry is not None and server and "web" in d.platforms:
        if "static" in w_tags:
            hosting.note = (f"The built {w_entry['name']} web app is served from the same host as the {wb.name} API, "
                            "so there is one deployment target.")
        else:
            hosting.note = f"The {w_entry['name']} web server and the {wb.name} API run as separate services on it."
    return backend, database, hosting


# supporting services

class _Sel:
    """Read-only view of what the core selection chose, used by the supporting-layer rules."""

    def __init__(self, ctx: Ctx, front: Pick, backend: Pick, database: Pick, hosting: Pick):
        self.front, self.backend, self.database, self.hosting = front, backend, database, hosting
        b = backend.chosen
        self.b_entry = b.entry if b else None
        self.b_id = b.entry_id if b else None
        self.b_name = b.name if b else None
        self.b_sub = b.entry.get("sub_layer") if b else None
        self.db_id = database.chosen.entry_id if database.chosen else None
        self.db_caps = set(database.chosen.entry["capabilities"]) if database.chosen else set()
        h = hosting.chosen
        self.host = h
        self.host_cloud = cloud(h.provider) if h and h.provider else None
        self.host_ids = {p.entry_id for p in (hosting.parts or ([h] if h else []))}
        self.runs_procs = any(has(p.entry, "container") for p in (hosting.parts or ([h] if h else [])))
        self.vendors = {vendor_of(s.provider) for s in
                        [*(hosting.parts or ([h] if h else [])), backend.chosen, database.chosen] if s is not None}
        self.vendors.discard(None)
        self.hyper = {k for k, r in ctx.kb.providers.items() if r.get("kind") == "hyperscaler"}
        self.clouds = self.vendors & self.hyper
        self.used: set[str] = set()
        self.relaxed: list[str] = []
        self.small = G(ctx.spec, "team.size", 1) <= 3 or not G(ctx.spec, "team.has_devops", False)
        fparts = front.parts or ([front.chosen] if front.chosen else [])
        self.front_ids = {p.entry_id for p in fparts}
        self.front_langs = set(l for p in fparts for l in p.entry.get("languages", []))


def c_managed(ctx: Ctx) -> bool:
    return ctx.c.deployment == "managed_cloud"


def _lang_ok(ctx: Ctx, v: _Sel, e: dict, layer: str) -> bool:
    langs = set(e.get("languages") or [])
    if not langs:
        return True
    if layer == "offline_sync":
        return bool(langs & v.front_langs) or bool(langs & JS_FAMILY and v.front_langs & JS_FAMILY)
    if v.b_entry is None or v.b_sub not in ("framework", "game_server"):
        return False
    bl = set(v.b_entry.get("languages") or [])
    return bool(langs & bl) or bool(langs & JS_FAMILY and bl & JS_FAMILY)


def _support(ctx: Ctx, v: _Sel, layer: str, *args, **kw) -> Pick | NoCandidate:
    """One supporting layer; when the explicit cloud preference leaves no option, that preference is relaxed for it."""
    res = _support_pass(ctx, v, layer, *args, **kw)
    if isinstance(res, NoCandidate):
        # only server-run options are left: keep one, scored down, rather than leave the layer empty
        again = _support_pass(ctx, v, layer, *args, strict=False, **kw)
        res = again if isinstance(again, Pick) else res
    if isinstance(res, NoCandidate) and (ctx.c.cloud_lock or ctx.c.no_hyperscaler):
        loose = replace(ctx, c=replace(ctx.c, cloud_lock=None, no_hyperscaler=False))
        again = _support_pass(loose, v, layer, *args, strict=False, **kw)
        if isinstance(again, Pick):
            v.relaxed.append(f"{again.chosen.name} ({layer})")
            return again
    return res


# the auth module of the chosen framework, shown instead of the generic entry name
FRAMEWORK_AUTH_NAME = {"django": "Django authentication", "springboot": "Spring Security", "aspnet": "ASP.NET Core Identity",
                       "laravel": "Laravel Breeze", "fastapi": "FastAPI security utilities (OAuth2 password flow)"}


def _needs_server(ctx: Ctx, v: _Sel, layer: str, e: dict, off: dict | None) -> bool:
    """Self-run software, library job queues and in-process WebSocket servers need a host that runs long-lived processes;
    an existing system already has its own servers."""
    if v.runs_procs or e["id"] in _existing_ids(ctx) or (off and "existing" in off["name"].lower()):
        return False
    return bool((off and off["kind"] == "self") or (not off and layer == "queue") or e.get("sub_layer") == "framework_native")


def _support_pass(ctx: Ctx, v: _Sel, layer: str, pred=None, mods=None, entries=None, sub=None, alts: int = 2,
                  strict: bool = True) -> Pick | NoCandidate:
    def wrapped(e, off):
        if strict and _needs_server(ctx, v, layer, e, off):
            return False, "needs a long-running server, and the chosen hosts run functions only", ()
        for cap in e.get("capabilities", []):
            if cap.startswith("bundled_") and cap[len("bundled_"):] != v.b_id and not (
                    v.vendors & {vendor_of(o["provider"]) for o in e.get("offerings", [])}):
                return False, f"is bundled with {cap[len('bundled_'):]}, which is not the chosen backend", ()
        if not _lang_ok(ctx, v, e, layer):
            return False, "needs a library for the chosen backend language", ("team.languages",)
        if pred is not None:
            return pred(e, off)
        return True, "", ()

    ok, ex = candidates(ctx, layer, entries, wrapped)
    fw_name = FRAMEWORK_AUTH_NAME.get(v.b_id)
    if fw_name:
        ex = [replace(x, name=fw_name) if x.entry_id == "framework_auth" else x for x in ex]
    scored = []
    for e, off in ok:
        extra = []
        if _needs_server(ctx, v, layer, e, off):
            extra.append(Mod(-40, "needs a long-running server, and the chosen hosts run functions only", ()))
        vend = vendor_of(off["provider"]) if off else None
        if vend and layer not in COHERENCE_EXEMPT and e["id"] not in _existing_ids(ctx):
            if vend in v.hyper and v.clouds and vend not in v.clouds:
                extra.append(Mod(CLOUD_MISMATCH, f"{(ctx.kb.provider(off['provider']) or {}).get('name', off['provider'])} is a different cloud "
                                 f"from the {', '.join(sorted(v.clouds))} core stack",
                                 ("infrastructure.cloud_preference",)))
            elif sub in ("sms_otp", "sms"):
                pass
            elif vend in v.vendors:
                kind = "cloud" if vend in v.hyper else "vendor"
                extra.append(Mod(4 if layer == "ci_cd" else 8,
                                 f"same {kind} ({(ctx.kb.provider(vend) or {}).get('name', vend)}) as the core stack, "
                                 "so one account and no cross-vendor traffic",
                                 ("infrastructure.cloud_preference",)))
            elif vend not in v.used and layer != "ci_cd":
                extra.append(Mod(-8 if v.small else -4,
                                 f"adds another vendor ({(ctx.kb.provider(off['provider']) or {}).get('name', off['provider'])}) "
                                 "to sign up with, secure and pay",
                                 ("team.size", "team.has_devops")))
        sm = self_host_mod(ctx, e, off) if layer not in COHERENCE_EXEMPT else None
        if sm:
            extra.append(sm)
        included = f"bundled_{v.b_id}" in e.get("capabilities", [])
        if included:
            extra.append(Mod(8, f"already included with {v.b_entry['name']}", ("infrastructure.cloud_preference",)))
        if mods:
            extra += mods(e, off)
        sc = make(ctx, layer, e, off, extra)
        if fw_name and e["id"] == "framework_auth":
            sc = replace(sc, name=fw_name)
        scored.append(bundled(ctx, sc, v.b_name) if included else sc)
    out_layer = OUT_LAYER.get(layer, layer)
    try:
        res = pick_from(out_layer, scored, ex, sub or (layer if layer in OUT_LAYER else None), alts)
    except NoCandidate as err:
        return err
    if res.chosen is not None and layer not in COHERENCE_EXEMPT:
        v.used.update(x for x in (vendor_of(res.chosen.provider),) if x)
    return res


def _combine(pick_layer: str, parts: list[Scored], sub: str | None, extra_pick: Pick | None = None) -> Pick:
    uniq, seen = [], set()
    for p in parts:
        if p.entry_id not in seen:
            seen.add(p.entry_id)
            uniq.append(p)
    if len(uniq) == 1:
        return Pick(pick_layer, sub, uniq[0], runners_up=extra_pick.runners_up if extra_pick else [],
                    excluded=extra_pick.excluded if extra_pick else [])
    lo = sum(p.cost[0] for p in uniq)
    hi = sum(p.cost[1] for p in uniq)
    first = uniq[0]
    comp = Scored("+".join(p.entry_id for p in uniq), None, " + ".join(p.name for p in uniq),
                  round(sum(p.score for p in uniq) / len(uniq), 2), first.dims, [m for p in uniq for m in p.mods],
                  None, first.provider, first.entry, first.off, None, (lo, hi))
    return Pick(pick_layer, sub, comp, runners_up=extra_pick.runners_up if extra_pick else [],
                excluded=extra_pick.excluded if extra_pick else [], parts=uniq)


def select_support(ctx: Ctx, front: Pick, backend: Pick, database: Pick, hosting: Pick, notes: list[str]) -> list[Pick]:
    spec, d, c = ctx.spec, ctx.d, ctx.c
    v = _Sel(ctx, front, backend, database, hosting)
    picks: list[Pick] = []
    india = "india" in d.user_geography or zone_of(ctx) == "india"
    geo = list(d.user_geography)
    devops = bool(G(spec, "team.has_devops", False))
    ptype = G(spec, "project.type")
    static = ctx.arch.style == "static"

    def finish(res: Pick | NoCandidate, fatal: bool, label: str, fields: tuple[str, ...] = ()) -> None:
        if isinstance(res, NoCandidate):
            if fatal:
                raise res
            reason = f"No {label} in the knowledge base meets the hard constraints."
            notes.append(reason)
            picks.append(Pick(OUT_LAYER.get(res.layer, res.layer), none_reason=reason, none_fields=fields,
                              sub_layer=res.layer if res.layer in OUT_LAYER else None))
        else:
            picks.append(res)

    # auth
    auth = G(spec, "requirements.auth", "none")
    if auth != "none":
        def apred(e, off):
            if e.get("sub_layer") == "sms_otp" or not has(e, auth):
                return False, f"does not provide {auth} sign-in", ("requirements.auth",)
            if auth == "phone_otp" and e["id"] == "framework_auth" and v.b_entry is not None \
                    and not {"java", "csharp"} & set(v.b_entry.get("languages") or []):
                return False, "has no built-in one-time sign-in codes for this backend language", ("team.languages",)
            return True, "", ()

        def amods(e, off):
            m = []
            if auth == "enterprise_sso" and e["id"] == "entra_id" and "microsoft_365" in d.licences:
                m.append(Mod(10, "Microsoft 365 licence already includes Entra ID", ("infrastructure.existing_licences",)))
            if auth == "enterprise_sso" and e["id"] == "workos" and ptype == "saas_b2b":
                m.append(Mod(4, "built for B2B customers who bring their own identity provider", ("project.type",)))
            if auth == "phone_otp" and e["id"] == "framework_auth":
                m.append(Mod(-15, "the code sign-in flow, retry throttling and SMS-fraud limits are yours to build",
                             ("requirements.auth",)))
            if G(spec, "requirements.multi_tenant", False) and e["id"] in ("clerk", "auth0", "workos", "keycloak", "zitadel"):
                m.append(Mod(3, "supports organisations for multi-tenant accounts", ("requirements.multi_tenant",)))
            return m
        res = _support(ctx, v, "auth", apred, amods, sub=auth)
        if isinstance(res, Pick) and auth == "phone_otp" and res.chosen.entry_id in ("supabase_auth", "cognito", "better_auth", "framework_auth"):
            def spred(e, off):
                return (e.get("sub_layer") == "sms_otp", "is not an SMS gateway", ())

            def smods(e, off):
                return [Mod(8, "registered for Indian DLT SMS rules", ("requirements.auth", "scale.user_geography"))] \
                    if india and has(e, "dlt") else []
            sms = _support(ctx, v, "auth", spred, smods, sub="sms_otp")
            if isinstance(sms, Pick):
                res = _combine("auth", [res.chosen, sms.chosen], "phone_otp", res)
            else:
                finish(sms, True, "SMS gateway")
        if isinstance(res, Pick) and not res.parts:
            res.sub_layer = auth
        finish(res, True, "sign-in service", ("requirements.auth",))
        got = picks[-1].chosen if picks and picks[-1].layer == "auth" else None
        if got and got.entry_id in _existing_ids(ctx) and c.zone and not (got.off.get("regions") or {}).get(c.zone):
            notes.append(f"{got.name} is reused because it is already in place; it has no verified {c.zone} region, "
                         "so confirm where the directory data is stored.")

    # payments
    if G(spec, "requirements.payments", False):
        def ppred(e, off):
            return True, "", ()

        def pmods(e, off):
            m = []
            prov = ctx.kb.provider(off["provider"]) or {}
            if has(e, "upi"):
                m.append(Mod(10, "supports UPI for Indian users", ("scale.user_geography", "infrastructure.data_residency"))
                         if india else Mod(-8, "built around UPI, which users outside India do not use", ("scale.user_geography",)))
            if india and not (off["regions"].get("india") or prov.get("full_residency", {}).get("india")):
                m.append(Mod(-12, f"{off['provider']} does not keep payment data in India", ("scale.user_geography", "infrastructure.data_residency")))
            if ptype == "marketplace" and (has(e, "marketplace_split") or has(e, "connect")):
                m.append(Mod(6, "splits payments between sellers and the platform", ("project.type",)))
            if ptype in ("saas_b2b", "realtime_collab") and (has(e, "billing") or has(e, "subscriptions")):
                m.append(Mod(4, "handles recurring per-seat subscriptions", ("project.type",)))
            if e["id"] == "paddle" and ptype in ("saas_b2b", "realtime_collab") and "eu" in geo and not india \
                    and G(spec, "team.size", 1) <= 5:
                m.append(Mod(3, "merchant of record handles EU VAT for a small team", ("scale.user_geography", "team.size")))
            return m
        finish(_support(ctx, v, "payments", ppred, pmods), True, "payment provider", ("requirements.payments",))

    # realtime
    if G(spec, "requirements.realtime", False):
        def rpred(e, off):
            sub = e.get("sub_layer")
            if sub == "gaming" and (ptype != "gaming" or e["id"].replace("_realtime", "") != v.b_id):
                return False, "is the realtime layer of a game server that is not chosen", ("project.type",)
            if sub == "collab" and not d.collab:
                return False, "is for collaborative editing, which this project does not need", ("project.type",)
            return True, "", ()

        def rmods(e, off):
            sub = e.get("sub_layer")
            m = []
            if sub == "collab" and d.collab:
                if has(e, "crdt"):
                    m.append(Mod(8, "built for shared documents with live cursors (CRDT)", ("project.type", "requirements.realtime")))
                else:
                    m.append(Mod(5, "one stateful room per shared document, so edits are ordered in one place",
                                 ("project.type", "requirements.realtime")))
            if sub == "gaming":
                m.append(Mod(8, "ships with the game server", ("project.type",)))
            if not devops and sub in ("managed", "baas", "collab") and off and off["kind"] == "managed":
                m.append(Mod(4, "managed service, nothing to operate without a devops engineer", ("team.has_devops",)))
            if devops and off and off["kind"] == "self" and sub == "collab":
                m.append(Mod(3, "self-hosting is practical with a devops engineer", ("team.has_devops",)))
                if v.runs_procs and has(e, "crdt"):
                    m.append(Mod(8, "runs as one more container on the chosen host and stores documents in the stack's "
                                    "own database, so no extra vendor holds user content", ("team.has_devops",
                                                                                            "infrastructure.cloud_preference")))
            if sub == "framework_native":
                m.append(Mod(3, "no extra system beyond the backend", ("requirements.realtime",)))
                if d.peak_concurrent >= 2000:
                    m.append(Mod(-4, f"one process holds all sockets at {d.peak_concurrent:,} concurrent users",
                                 ("scale.peak_concurrent_users",)))
            return m
        finish(_support(ctx, v, "realtime", rpred, rmods), True, "real-time service", ("requirements.realtime",))

    # storage
    if G(spec, "requirements.file_media_storage", False):
        finish(_support(ctx, v, "storage"), True, "object storage", ("requirements.file_media_storage",))

    # search
    if G(spec, "requirements.full_text_search", False):
        def fpred(e, off):
            if e["id"] == "pagefind" and not static:
                return False, "indexes static sites only", ("project.type",)
            if static and e["id"] != "pagefind":
                return False, "a static site has no server to run a search engine", ("project.type",)
            if e["id"] == "pg_fts" and not ("fts" in v.db_caps and v.db_id == "postgresql"
                                           and G(spec, "scale.data_volume_gb_12_months", 0) <= 100 and d.users_peak <= 1_000_000):
                return False, "needs PostgreSQL, at most 100 GB of data and 1,000,000 users", ("scale.data_volume_gb_12_months",)
            return True, "", ()

        def fmods(e, off):
            m = []
            if e["id"] == "pg_fts":
                m.append(Mod(12, "PostgreSQL already searches text, so there is no extra system to run",
                             ("requirements.full_text_search", "scale.data_volume_gb_12_months")))
            if e["id"] == "pagefind":
                m.append(Mod(12, "builds a search index at deploy time for a static site", ("requirements.full_text_search",)))
            if not devops and e["id"] in ("meilisearch", "typesense") and off and off["kind"] == "managed":
                m.append(Mod(3, "managed search, nothing to operate", ("team.has_devops",)))
            return m
        finish(_support(ctx, v, "search", fpred, fmods), True, "search service", ("requirements.full_text_search",))

    # cache
    p95 = G(spec, "requirements.p95_latency_ms")
    if not static and v.db_id:
        hot = d.peak_concurrent >= 1000 or d.peak_rps >= 200
        trig = hot or (G(spec, "scale.workload") == "read_heavy" and d.users_peak >= 100_000) or \
            (G(spec, "requirements.realtime", False) and (d.peak_rps >= 100 or d.peak_concurrent >= 500)) or \
            (p95 is not None and p95 <= 150)
        if ctx.budget_mode:
            trig = hot or (p95 is not None and p95 <= 150)
        if trig:
            def cmods(e, off):
                m = []
                if ctx.arch.style == "serverless" and e["id"] == "upstash_redis":
                    m.append(Mod(4, "per-request pricing suits serverless traffic", ("requirements.availability",)))
                if not devops and off and off["kind"] == "managed":
                    m.append(Mod(3, "managed cache, nothing to patch", ("team.has_devops",)))
                return m
            finish(_support(ctx, v, "cache", None, cmods), False, "cache", ("requirements.p95_latency_ms",))

    # queue
    if not static and v.b_id and (G(spec, "requirements.background_jobs", d.background_jobs) or ptype == "iot" or
                                  (G(spec, "scale.workload") == "write_heavy" and G(spec, "scale.traffic_pattern") == "spiky")):
        small = d.users_peak <= 100_000 and d.peak_rps < 500

        def qpred(e, off):
            if e["id"] == "kafka" and not (d.peak_rps >= 5000 or (ptype in ("iot", "data_analytics") and d.users_peak >= 1_000_000)):
                return False, "is for very high event throughput (5,000+ requests per second)", ("scale.peak_rps",)
            if e["id"] == "pg_queue" and v.db_id not in ("postgresql", "timescaledb", "cockroachdb"):
                return False, "needs a PostgreSQL-compatible database", ()
            if e["id"] == "bullmq" and not any(p.layer == "cache" and p.chosen for p in picks):
                return False, "needs a Redis instance, and no cache is part of the stack", ()
            return True, "", ()

        def qmods(e, off):
            if small and e["id"] == "pg_queue":
                return [Mod(10, "a queue table in the existing PostgreSQL is enough at this scale", ("scale.users_12_months",))]
            if small and not off:
                return [Mod(4, "library queue, no extra service at this scale", ("scale.users_12_months",))]
            return []
        finish(_support(ctx, v, "queue", qpred, qmods), False, "job queue", ("requirements.background_jobs",))

    # cdn
    if not c.no_cdn and not static or (static and not c.no_cdn):
        # an internal tool's users sit on the organisation's network; a default "global" is not a reason for a CDN
        wide = len(geo) > 1 if ptype == "internal_tool" else ("global" in geo or len(geo) > 1)
        need = (G(spec, "requirements.seo_critical", False) or ptype == "content_site" or wide or
                (G(spec, "requirements.file_media_storage", False) and ptype != "internal_tool"))
        if need and "web" in d.platforms:
            def cdpred(e, off):
                inc = e.get("included_with_host")
                if inc and inc not in v.host_ids:
                    return False, "is only available with its own hosting platform", ()
                return True, "", ()

            def cdmods(e, off):
                return [Mod(15, "included with the hosting platform at no extra cost", ("project.platforms",))] \
                    if e.get("included_with_host") else []
            finish(_support(ctx, v, "cdn", cdpred, cdmods), False, "CDN", ("infrastructure.deployment_model",))

    # ml
    ml = G(spec, "requirements.ml_features", "none")
    if ml != "none":
        py = "python" in d.team_langs

        def mmods(e, off):
            m = []
            if e["id"] == "onnx_inprocess" and not py:
                m.append(Mod(6, "runs inside the backend without a Python service", ("team.languages",)))
            if e["id"] == "pgvector":
                m.append(Mod(6, "vectors live in the existing PostgreSQL", ("requirements.ml_features",)))
            return m
        if ml == "llm_rag":
            def vpred(e, off):
                if e.get("sub_layer") != "vector":
                    return False, "", ()
                if e["id"] == "pgvector" and "pgvector" not in v.db_caps:
                    return False, "needs PostgreSQL with pgvector", ()
                return True, "", ()
            vec = _support(ctx, v, "ml", vpred, mmods, sub="vector")
            llm = _support(ctx, v, "ml", lambda e, o: (e.get("sub_layer") == "llm", "", ()), mmods, sub="llm")
            if isinstance(vec, NoCandidate) or isinstance(llm, NoCandidate):
                finish(vec if isinstance(vec, NoCandidate) else llm, True, "LLM service", ("requirements.ml_features",))
            else:
                picks.append(_combine("ml", [vec.chosen, llm.chosen], "llm_rag", llm))
        else:
            sub_ok = ("inference_api", "training") if ml == "inference_api" else ("training",)

            def mpred(e, off):
                if e.get("sub_layer") not in sub_ok or (ml == "inference_api" and not has(e, "inference_api")):
                    return False, "", ()
                return True, "", ()
            finish(_support(ctx, v, "ml", mpred, mmods, sub=ml), True, "ML service", ("requirements.ml_features",))

    # other: offline sync
    if G(spec, "requirements.offline_support", False):
        def opred(e, off):
            if e["id"] == "firestore_offline" and v.db_id != "firestore":
                return False, "needs Firestore as the database", ()
            return True, "", ()

        def omods(e, off):
            m = []
            if e["id"] == "sqlite_outbox":
                m.append(Mod(6, "on-device SQLite with a sync queue fits the mobile framework", ("requirements.offline_support",)))
            if e["id"] == "firestore_offline":
                m.append(Mod(14, "Firestore already caches and syncs on the device", ("requirements.offline_support",)))
            if e["id"] == "powersync" and v.db_id == "postgresql":
                m.append(Mod(4, "syncs PostgreSQL to on-device SQLite", ("requirements.offline_support",)))
            return m
        finish(_support(ctx, v, "offline_sync", opred, omods), True, "offline sync", ("requirements.offline_support",))

    # other: notifications
    if d.notifications and not static:
        parts, first = [], None
        for ch in d.notifications:
            if ch == "sms" and auth == "phone_otp":
                notes.append("SMS messages go through the same gateway as the one-time passwords, so no second SMS provider is added.")
                continue
            if ch == "push" and not set(MOBILE) & set(d.platforms):
                notes.append("Push notifications are skipped because no mobile app is planned; email covers web users.")
                continue
            def npred(e, off, ch=ch):
                if e["id"] == "ms_graph_mail" and "microsoft_365" not in d.licences:
                    return False, "needs a Microsoft 365 licence", ("infrastructure.existing_licences",)
                if e["id"] == "postfix_relay" and c.deployment not in ("on_premise", "air_gapped"):
                    return False, "cloud networks restrict outbound SMTP, so a self-run relay suits only own servers", \
                        ("infrastructure.deployment_model",)
                return (ch in e["capabilities"], "", ())

            def nmods(e, off, ch=ch):
                m = []
                if e.get("sub_layer") != ch and ch != "sms":
                    m.append(Mod(-10, f"mainly a {e.get('sub_layer')} service, with {ch} as a side feature", ("requirements.notifications",)))
                if ch == "sms" and india and has(e, "dlt_india"):
                    m.append(Mod(10, "registered for Indian DLT SMS rules", ("scale.user_geography",)))
                if ch == "email" and c.deployment in ("on_premise", "air_gapped") and off and off.get("kind") == "self":
                    m.append(Mod(10, "keeps mail inside the organisation's network", ("infrastructure.deployment_model",)))
                if ch == "email" and "microsoft_365" in d.licences and e["id"] == "ms_graph_mail":
                    m.append(Mod(5, "Microsoft 365 licence includes mail sending", ("infrastructure.existing_licences",)))
                return m
            r = _support(ctx, v, "notifications", npred, nmods, sub=ch)
            if isinstance(r, Pick):
                parts.append(r.chosen)
                first = first or r
            else:
                notes.append(f"No {ch} service in the knowledge base meets the hard constraints; use the organisation's own relay.")
        if parts:
            picks.append(_combine("other", parts, "notifications", first))
            picks[-1].sub_layer = "notifications"

    # other: security baseline
    sens = d.data_sensitivity in ("financial", "health")
    comp = set(G(spec, "compliance", []))
    if not static and (sens or comp & {"pci_dss", "hipaa", "rbi_localisation", "soc2", "iso27001"}) and v.host_cloud:
        def secpred(e, off):
            if off["provider"] == "self":
                return (c.self_host_only, "is for self-hosted platforms", ())
            return (cloud(off["provider"]) == v.host_cloud, "belongs to a different cloud than the host", ())
        r = _support(ctx, v, "security", secpred, None)
        if isinstance(r, Pick):
            picks.append(r)
        else:
            notes.append("No provider security baseline matches the hosting platform; use the host's built-in controls.")

    # other: iot ingestion
    if ptype == "iot":
        finish(_support(ctx, v, "iot_ingestion"), True, "IoT ingestion", ("project.type",))

    # other: content management for editors
    if d.cms:
        def cmods(e, off):
            m = []
            if e["id"] == "wordpress" and "php" in d.team_langs:
                m.append(Mod(8, "the team already writes PHP, the language of WordPress and its plugins", ("team.languages",)))
            if e["id"] == "decap_cms" and (static or not d.needs_backend):
                m.append(Mod(8, "edits are commits to the site's repository, so the static build needs no server or database",
                             ("project.description",)))
            return m
        finish(_support(ctx, v, "cms", None, cmods), False, "content management system", ("project.description",))

    # other: dashboards for analytics projects
    if ptype == "data_analytics":
        finish(_support(ctx, v, "bi"), False, "BI dashboard tool", ("project.type",))

    # other: artifact mirror (air-gapped)
    if c.deployment == "air_gapped":
        reg = _support(ctx, v, "artifact_mirror", lambda e, o: (has(e, "container_registry"), "", ()), None)
        prx = _support(ctx, v, "artifact_mirror", lambda e, o: (has(e, "package_proxy"), "", ()), None)
        if isinstance(reg, Pick) and isinstance(prx, Pick):
            picks.append(_combine("other", [reg.chosen, prx.chosen], "artifact_mirror", reg))
            picks[-1].sub_layer = "artifact_mirror"

    # ci/cd
    def cipred(e, off):
        if e["id"] == "expo_eas":
            return False, "", ()
        if e["id"] == "host_git_integration" and not any(has(p.entry, "native_git_ci") for p in
                                                         (hosting.parts or [hosting.chosen]) if p):
            return False, "needs a host with its own build-from-git pipeline", ()
        return True, "", ()

    def cimods(e, off):
        m = []
        if e["id"] == "host_git_integration" and static:
            m.append(Mod(10, "the static host builds and deploys from git, nothing else to run", ("project.type",)))
        if e["id"] == "github_actions":
            m.append(Mod(3, "widely used, with free minutes for small teams", ("team.size",)))
        if e["id"] == "gitlab_ci" and c.self_host_only:
            m.append(Mod(4, "can run inside your own network", ("infrastructure.deployment_model",)))
        return m
    res = _support(ctx, v, "ci_cd", cipred, cimods)
    if isinstance(res, Pick) and res.chosen.entry_id == "host_git_integration":
        ci_host = next(p for p in (hosting.parts or [hosting.chosen]) if has(p.entry, "native_git_ci"))
        res.chosen = replace(res.chosen, name=f"{ci_host.name} git integration")
    if isinstance(res, Pick) and "expo_rn" in v.front_ids:
        eas = ctx.kb.entry("ci_cd", "expo_eas")
        if eas and not ctx.c.self_host_only:
            es = make(ctx, "ci_cd", eas, eas["offerings"][0], [])
            res = _combine("ci_cd", [res.chosen, es], None, res)
    finish(res, False, "CI/CD service", ("infrastructure.deployment_model",))

    # observability
    if not static:
        def omods2(e, off):
            m = []
            if off and off["kind"] != "self" and c.zone and not (off.get("regions") or {}).get(c.zone):
                m.append(Mod(-6, f"no {c.zone} region for telemetry", ("infrastructure.data_residency",)))
            if e["id"] == "sentry" and not devops:
                m.append(Mod(3, "hosted error tracking with a free tier", ("team.has_devops",)))
            return m
        finish(_support(ctx, v, "observability", None, omods2), False, "monitoring service", ())
    if v.relaxed:
        pref = (c.cloud_lock or "none").upper() if c.cloud_lock else "no-hyperscaler"
        notes.append(f"RELAXED: infrastructure.cloud_preference: no {pref} option meets the other hard constraints for "
                     f"{', '.join(v.relaxed)}, so a service from another cloud or vendor is used there.")
    return picks


# entry point

def _assign_regions(ctx: Ctx, sel: Selection) -> None:
    host = sel.hosting.chosen
    host_parts = sel.hosting.parts or ([host] if host else [])
    api_host = host_parts[-1] if host_parts else None
    hregion = region_of(api_host.off, ctx) if api_host and api_host.off else None
    sel.zone = zone_of(ctx)
    sel.region = hregion

    def setr(s: Scored | None) -> None:
        if s is not None and s.off:
            s.region = region_of(s.off, ctx, hregion)
            if s.off.get("kind") == "self" and ctx.c.deployment == "managed_cloud" and s is not api_host:
                s.region = hregion  # self-run software is deployed on the chosen host
    for p in host_parts:
        setr(p)
    for pick in (sel.backend, sel.database, *sel.supporting):
        for s in (pick.parts or ([pick.chosen] if pick.chosen else [])):
            setr(s)
        if pick.chosen and pick.parts:
            # a combined pick is placed where its own first placed part runs, in that provider's region names
            pick.chosen.region = next((p.region for p in pick.parts if p.region), None)
    if api_host and api_host.off:
        regs = (api_host.off.get("regions") or {}).get(zone_of(ctx)) or []
        want_dr = (float(G(ctx.spec, "requirements.availability", "99.9")) >= 99.95 or ctx.d.rpo_minutes <= 15
                   or "rbi_localisation" in G(ctx.spec, "compliance", []))
        if want_dr and len(regs) > 1:
            sel.dr_region = next((r for r in regs if r != hregion), None)


def select(spec: dict, d: Derived, c: Constraints, kb, arch: ArchDecision, budget_mode: bool = False) -> Selection:
    ctx = Ctx(spec, d, c, kb, arch, budget_mode, float(G(spec, "budget.monthly_infra_usd", 0) or 0), c.commercial)
    front = select_frontend(replace(ctx, budget_mode=False))
    backend, database, hosting = select_core(ctx, front)
    notes: list[str] = []
    sel = Selection(arch, front, backend, database, hosting, [], notes=notes)
    sel.supporting = select_support(ctx, front, backend, database, hosting, notes)
    _assign_regions(ctx, sel)
    return sel
