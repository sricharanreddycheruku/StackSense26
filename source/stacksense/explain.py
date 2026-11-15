"""Turns the selection into the explanation blocks of the output JSON."""
from __future__ import annotations

import math
import re

from .cost import item_cost
from .model import ArchDecision, Pick, Scored, article
from .schema import FIELD_INDEX

DIM_LABEL = {"cost": "cost", "time_to_market": "time to market", "scalability": "scalability",
             "performance": "performance", "maintainability": "maintainability", "security": "security"}


LANG_CLAIMS = ("which the team already uses", "like the frontend")
PARTICIPLE = re.compile(r"(?:suited|written|built for|designed)\b")
NOUN_START = re.compile(r"(?:largest|built-in|vector search)\b")
VERB_START = re.compile(r"(?:reuses|migrates|integrates|deploys|includes|has|is|supports|keeps|fits|runs|meets|handles|offers)\b")
POOR_FIT = "a poor fit for project.type"
WEB_HOST_HINT = "; pair with a web host for the front end"


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:] if text[1:2].islower() else text


def _get(spec: dict, path: str, default=None):
    node = spec
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return default if node is None else node


def _paths(fields) -> list[str]:
    return sorted({f for f in fields if f in FIELD_INDEX})


def _top_dims(s: Scored, weights: dict, n: int = 2) -> list[str]:
    ranked = sorted(((weights.get(k, 0) * v, k) for k, v in s.dims.items() if k in weights),
                    key=lambda t: (-t[0], t[1]))
    return [k for _, k in ranked[:n]]


def _dim_reason(s: Scored, key: str, spec: dict, d, layer_cost: tuple | None = None, lang_said: bool = False,
                mvp_said: bool = False) -> str | None:
    """A concrete property of this scenario that the component satisfies, or None when it is not a strength."""
    if s.dims.get(key, 0) < 3.5:
        return None
    e = s.entry or {}
    size = _get(spec, "team.size", 1)
    if key == "cost":
        lo, hi = layer_cost or s.cost
        budget = _get(spec, "budget.monthly_infra_usd", 0)
        if hi == 0:
            return "free at the expected load"
        return f"about ${lo:,.0f}-{hi:,.0f} a month for this layer against the ${budget:,.0f} monthly budget"
    if key == "time_to_market":
        weeks = _get(spec, "timeline.mvp_weeks")
        known = sorted(set(e.get("languages") or []) & set(d.team_langs))
        tail = "" if mvp_said else f", for the {weeks}-week MVP"
        if known and not lang_said:
            return f"uses {', '.join(known)}, which the team already writes{tail}"
        return "quick to build with" if mvp_said else f"quick to build with for {article(weeks)} {weeks}-week MVP"
    if key == "scalability":
        return f"handles the {_get(spec, 'scale.users_12_months', 0):,} users expected in 12 months (about {d.peak_concurrent:,.0f} concurrent at peak)"
    if key == "performance":
        p95 = _get(spec, "requirements.p95_latency_ms")
        return f"meets the {p95} ms p95 target" if p95 else f"fast enough for a {_get(spec, 'scale.workload', 'balanced').replace('_', ' ')} workload"
    if key == "maintainability":
        if _get(spec, "team.has_devops", False):
            return f"mature and well documented for a team of {size} with a devops engineer"
        return f"low operations burden for a team of {size} with no devops engineer"
    if key == "security":
        comp = _get(spec, "compliance", [])
        return "strong security defaults" + (f" for {', '.join(comp)}" if comp else f" for {d.data_sensitivity} data")
    return None


def _front_role(part: Scored, d) -> str:
    """The platforms one part of a multi-part frontend is built for."""
    sub = (part.entry or {}).get("sub_layer", "")
    if sub.startswith("web_"):
        return "Web app"
    mobile = [n for p, n in (("android", "Android"), ("ios", "iOS")) if p in d.platforms]
    if sub in ("mobile_cross", "mobile_native", "game_engine") and mobile:
        return " and ".join(mobile) + (" apps" if len(mobile) > 1 else " app")
    return "Desktop app"


def _self_hosted_reason(s: Scored, pick: Pick, spec: dict) -> str | None:
    """Why a self-hosted component was chosen although the deployment is a managed cloud."""
    if s.provider != "self" or _get(spec, "infrastructure.deployment_model", "managed_cloud") != "managed_cloud" \
            or "existing" in (s.off or {}).get("name", "").lower():
        return None
    if _get(spec, "infrastructure.vendor_lock_in_tolerance") == "low":
        return "Self-hosted because infrastructure.vendor_lock_in_tolerance is low."
    if _get(spec, "team.has_devops", False):
        return "Self-hosted although the deployment is managed_cloud: team.has_devops is true, so the team can run it."
    managed = [r for r in pick.runners_up if r.provider != "self"]
    if managed:
        return (f"Self-hosted although the deployment is managed_cloud: it scored above the best managed option, "
                f"{managed[0].name} (see alternatives).")
    noise = ("needs a library", "is bundled with", "only offered when", "does not provide")
    why = "; ".join(x.reason.rstrip(".") for x in pick.excluded if not any(n in x.reason for n in noise))
    return ("Self-hosted although the deployment is managed_cloud, because no managed option passed the hard constraints"
            + (f" ({why})." if why else "."))


def _mod_reason(m) -> str:
    return m.reason.rstrip(".")


def _free_terms(s: Scored) -> str | None:
    free = (s.off or {}).get("free_tier") or {}
    return free.get("limits") if free.get("available") else None


def _tie_break(alt: Scored, best: Scored) -> str:
    """The concrete point that separates two options whose scores are within half a point."""
    ra, rb = (alt.entry or {}).get("ratings", {}), (best.entry or {}).get("ratings", {})
    if rb.get("ops_burden", 3) < ra.get("ops_burden", 3):
        return f"{best.name} has the lower operations burden ({rb['ops_burden']} against {ra['ops_burden']} of 5)"
    if rb.get("maturity", 3) > ra.get("maturity", 3):
        return f"{best.name} is the more mature option ({rb['maturity']} against {ra['maturity']} of 5)"
    fa, fb = _free_terms(alt), _free_terms(best)
    if fb and fb != fa:
        return f"{best.name} is chosen on its free-tier terms ({fb}); {alt.name} has {fa or 'no free tier'}"
    return f"{best.name} wins the fixed tie-break order (operations burden, maturity, then name)"


def _clause(why: str) -> str:
    """Make a mod reason read as a clause after 'because'."""
    if VERB_START.match(why):
        return "it " + why
    if PARTICIPLE.match(why):
        return "it is " + why
    if why.startswith("largest"):
        return "it has the " + why
    if NOUN_START.match(why):
        return "it has " + why
    return why


def _back_label(term) -> str:
    _, kind, what = term
    return DIM_LABEL.get(what, what) if kind == 0 else _mod_reason(what)


# exclusions that hold for almost every project, so naming the option as an alternative says nothing
OFF_TOPIC_REASONS = ("(Kubernetes)", "runs on your own servers")


def _why_not(alt: Scored, best: Scored, weights: dict, layer: str | None = None) -> str:
    gap = best.score - alt.score
    if alt.stack_gap is not None and alt.stack_gap > 0 and gap < 0.5:
        side = f"{-gap:.1f} points higher" if gap < 0 else "about as high"
        return (f"{alt.name} scores {side} on its own, but the best full stack built around it totals "
                f"{alt.stack_gap:.1f} points less once hosting, database and cloud fit are counted.")
    if gap < 0.5:
        return f"{alt.name} and {best.name} are about equal on the weighted score; {_tie_break(alt, best)}."
    total = sum(weights.get(k, 0) for k in alt.dims if k in weights)
    terms = []
    for k, v in alt.dims.items():
        if k in weights and total:
            terms.append((20 * weights[k] * (best.dims.get(k, 0) - v) / total, 0, k))
    for m in alt.mods:
        if m.delta < 0 and m not in best.mods:
            terms.append((-m.delta, 1, m))
    for m in best.mods:
        if m.delta > 0 and m not in alt.mods and not (layer == "hosting" and m.fields == ("infrastructure.existing_systems",)):
            terms.append((m.delta, 2, m))
    # a bare project-type match says little, so a concrete difference is quoted first
    terms.sort(key=lambda t: (t[1] == 2 and _mod_reason(t[2]).startswith("suited to project.type"), -t[0], t[1],
                              str(t[2].reason if t[1] else t[2])))
    if terms and terms[0][0] > 0:
        value, kind, what = terms[0]
        if kind == 0:
            head = (f"{alt.name} scores lower on {DIM_LABEL.get(what, what)} ({alt.dims[what]:.1f} against "
                    f"{best.dims[what]:.1f}, priorities.{what} is {weights[what]:g})")
            if value <= gap + 0.05:
                return f"{head}, which accounts for {value:.1f} of the {gap:.1f}-point gap."
            back = next((t for t in reversed(terms) if t[0] < 0), None)
            tail = f" (it gains some back on {_back_label(back)})" if back else ""
            return f"{head}, worth {value:.1f} weighted points, more than its {gap:.1f}-point overall deficit{tail}."
        if kind == 1:
            return f"{alt.name}: {_mod_reason(what)}."
        return f"{best.name} is preferred because {_clause(_mod_reason(what))}."
    return f"{alt.name} ranks {gap:.1f} points below {best.name} on the weighted score."


def _cheapest(kb, layer: str, users: float, commercial: bool):
    best = None
    for e in kb.entries(layer):
        for off in (e.get("offerings") or [None]):
            if off is not None and "existing" in off.get("name", "").lower():
                continue
            lo = item_cost(e, off, users, commercial)[0]
            key = (lo, e["name"])
            if best is None or key < best[0]:
                best = (key, e, off)
    return best


def _sources(s: Scored | None) -> list[str]:
    if s is None:
        return []
    urls = []
    for src in list((s.off or {}).get("sources") or []) + list((s.entry or {}).get("sources") or []):
        url = src.get("url") if isinstance(src, dict) else src
        if url and url not in urls:
            urls.append(url)
    return urls


def _headroom(pick: Pick, users: int) -> str | None:
    """Flag a core pick whose scalability rating sits near or under the 12-month user peak."""
    s = pick.parts[-1] if pick.layer == "hosting" and len(pick.parts) > 1 else pick.chosen
    if s.bundled_in:
        return None
    rating = int(((s.entry or {}).get("ratings") or {}).get("scalability", 5))
    if rating >= 5 or users <= 0:
        return None
    ceiling = 10 ** (rating + 2)
    if users * 3 < ceiling:
        return None
    up = next((r for r in pick.runners_up
               if int(((r.entry or {}).get("ratings") or {}).get("scalability", 0)) > rating), None)
    where = "is above" if users > ceiling else "is within 3x of"
    text = (f"Scale headroom: {s.name} is rated {rating} of 5 for scalability (comfortable to about {ceiling:,} users), "
            f"and the 12-month peak of {users:,} {where} that.")
    if up:
        text += f" {up.name} is the step up if growth runs ahead of plan."
    return text


def _tradeoffs(s: Scored, spec: dict, d, pick: Pick) -> list[str]:
    layer = pick.layer
    e = s.entry or {}
    out = [str(x) for x in (e.get("limits") or [])[:2]]
    free = (s.off or {}).get("free_tier") or {}
    if free.get("available") and s.cost[0] == 0 and not s.bundled_in and (s.off or {}).get("kind") != "self":
        out.append(f"Free tier limits apply: {free.get('limits', 'see provider')}.")
    if e.get("lock_in") in ("high",) or (s.off or {}).get("lock_in") == "high":
        out.append(f"High lock-in; priorities show lock-in tolerance {d.lock_in_tolerance}.")
    if layer in ("frontend", "backend") and e.get("languages") and d.team_langs and not (set(e["languages"]) & d.team_langs):
        out.append(f"Uses {', '.join(e['languages'])}; team.languages is {', '.join(sorted(d.team_langs))}, "
                   "so expect a learning period.")
    if "cold" in " ".join(e.get("limits") or []).lower() and "cold" not in " ".join(out).lower():
        out.append("Cold starts can add latency on the first request after idle time.")
    for m in sorted((m for m in s.mods if m.delta < 0 and not m.reason.startswith(("self-hosted", POOR_FIT))),
                    key=lambda m: (m.delta, m.reason))[:1]:
        if m.reason.startswith("heavy on memory") and d.min_device_ram_gb is not None:
            out = [x for x in out if "heavier than native" not in x.lower()]
            out.append(f"Heavier than native; keep the bundle lean for {d.min_device_ram_gb:g} GB devices.")
        else:
            out.append(m.reason[0].upper() + m.reason[1:].rstrip(".") + ".")
    room = _headroom(pick, d.users_peak) if layer in ("backend", "database", "hosting") else None
    if room:
        out.append(room)
    return out


def component(pick: Pick, spec: dict, d, kb, users: float | None = None, layer_cost: tuple | None = None) -> dict:
    s = pick.chosen
    w = d.weights
    out: dict = {"layer": pick.layer} if pick.layer not in ("frontend", "backend", "database", "hosting") else {}
    if s is None:
        reason = pick.none_reason or "not needed for this scope"
        cheap = _cheapest(kb, pick.layer, d.users_peak, True)
        alts = []
        if cheap:
            _, e, off = cheap
            alts.append({"choice": (off or e)["name"], "why_not": reason[0].upper() + reason[1:].rstrip(".") + "."})
        out.update({"choice": "None", "justification": reason[0].upper() + reason[1:].rstrip(".") + ".",
                    "driven_by": _paths(pick.none_fields), "alternatives": alts, "tradeoffs": [],
                    "sources": [], "region": None, "score": None})
        if pick.sub_layer:
            out["sub_layer"] = pick.sub_layer
        return out

    reasons = []
    for m in sorted((m for m in s.mods if m.delta > 0), key=lambda m: (-m.delta, m.reason)):
        if _mod_reason(m) not in reasons:
            reasons.append(_mod_reason(m))
    reasons = reasons[:3]
    top = _top_dims(s, w)
    lang_said = any(c in r for r in reasons for c in LANG_CLAIMS)
    mvp_said = any("-week MVP" in r for r in reasons)
    props = [r for r in (_dim_reason(s, k, spec, d, layer_cost, lang_said, mvp_said) for k in top) if r]
    parts = []
    if reasons:
        parts.append("; ".join(reasons))
    if props:
        parts.append("; ".join(props))
    strengths = (s.entry or {}).get("strengths") or []
    text = f"{s.name}: " + "; ".join(parts) + "." if parts else f"{s.name}."
    if pick.layer == "frontend" and len(pick.parts) > 1:
        for part in pick.parts:
            first = ((part.entry or {}).get("strengths") or [""])[0]
            text += f" {_front_role(part, d)}: {part.name}" + (f" ({_lower_first(str(first).rstrip('.'))})." if first else ".")
    elif strengths:
        line = str(strengths[0]).rstrip(".")
        if "web" not in d.platforms:
            line = line.replace(WEB_HOST_HINT, "")
        text += f" {line}."
    if any(m.reason.startswith(POOR_FIT) for m in s.mods):
        rivals = [a for a in pick.runners_up if a.vs in (None, s)]
        lead = s.score - rivals[0].score if rivals else 0
        ahead = f", {lead:.1f} points ahead of {rivals[0].name}" if lead >= 0.1 else ""
        text += (f" It is generally a weaker fit for project.type {_get(spec, 'project.type')}, but it still ranks first "
                 f"here{ahead}.")
    own = _self_hosted_reason(s, pick, spec)
    if own:
        text += " " + own
    if s.region:
        text += f" Deployed in {s.region}."
    if pick.note:
        text += " " + pick.note

    fields = [f for m in s.mods if m.delta > 0 for f in m.fields]
    fields += [f"priorities.{k}" for k in top]
    fields += [f for x in pick.excluded for f in x.fields]
    name = s.name
    if pick.layer == "hosting" and len(pick.parts) > 1:
        name = " + ".join(x.name for x in pick.parts)
        api = pick.parts[-1]
        bits = [_lower_first(str((api.entry or {}).get("strengths", [""])[0]).rstrip(".").replace(WEB_HOST_HINT, ""))]
        comp = _get(spec, "compliance", [])
        if comp:
            bits.append(f"security rated {(api.entry or {}).get('ratings', {}).get('security', 3)} of 5 for {', '.join(comp)}")
        text += f" The web build runs on {pick.parts[0].name} and the API on {api.name} ({'; '.join(b for b in bits if b)})."
    out.update({"choice": name, "justification": text, "driven_by": _paths(fields)})

    alts = []
    for alt in pick.runners_up:
        why = _why_not(alt, alt.vs or s, w, pick.layer)
        best = alt.vs or s
        if alt.team_lang and alt.stack_gap is not None and alt.stack_gap > best.score - alt.score + 0.5 \
                and "full stack" not in why:
            why += (f" It would use {alt.team_lang}, which the team writes, but with its best hosting and database "
                    f"the whole stack totals {alt.stack_gap:.1f} points less.")
        alts.append({"choice": alt.name, "why_not": why})
    named = [x for x in pick.excluded if x.reason.strip(" .")]
    for x in [x for x in named if not any(t in x.reason for t in OFF_TOPIC_REASONS)][:1]:
        if len(alts) < 3 and x.name not in {a["choice"] for a in alts}:
            alts.append({"choice": x.name, "why_not": f"Excluded: {x.reason.rstrip('.')}."})
    if not alts and named:
        x = named[0]
        alts.append({"choice": x.name, "why_not": f"Excluded: {x.reason.rstrip('.')}."})
    out["alternatives"] = alts
    out["tradeoffs"] = _tradeoffs(s, spec, d, pick)
    out["sources"] = _sources(s)
    out["region"] = s.region
    out["score"] = round(s.score, 2)
    # margin over the nearest scored rival: the whole-stack gap for core layers, the plain score gap otherwise
    gaps = [a.stack_gap if a.stack_gap is not None else s.score - a.score
            for a in pick.runners_up if a.vs in (None, s) and not a.team_lang]
    if gaps:
        margin = round(max(0.0, min(gaps)), 1)
        out["margin"] = margin
        out["confidence"] = "high" if margin >= 8 else "medium" if margin >= 2 else "low"
    if pick.sub_layer:
        out["sub_layer"] = pick.sub_layer
    return out


def capacity(spec: dict, d, sel, kb) -> dict:
    """Peak load figures behind the sizing, and how many API instances they imply."""
    scale = spec.get("scale") or {}
    basis = []
    if scale.get("peak_concurrent_users") is None:
        basis.append("peak_concurrent_users: 5% of the larger user count, minimum 10")
    else:
        basis.append("peak_concurrent_users: given in scale.peak_concurrent_users")
    if scale.get("peak_rps") is None:
        spiky = " x1.5 for spiky traffic," if _get(spec, "scale.traffic_pattern") == "spiky" else ""
        basis.append(f"peak_rps: 0.2 requests per second per concurrent user,{spiky} derived")
    else:
        basis.append("peak_rps: given in scale.peak_rps")
    be = sel.backend
    host = sel.hosting.parts[-1] if sel.hosting.parts else sel.hosting.chosen
    instances = None
    if be.chosen and host and (host.entry or {}).get("sub_layer") not in ("static", "baas_platform", "serverless"):
        per = kb.sizing["rps_per_instance"]
        instances = max(1, math.ceil(d.peak_rps / per))
        basis.append(f"api_instances_at_peak: peak_rps / {per} requests per second per instance, rounded up")
        if str(_get(spec, "requirements.availability", "99.9")) in ("99.95", "99.99") and instances < 2:
            instances = 2
            basis.append("at least 2 instances in separate zones for the availability target")
        if instances > 1 and scale.get("peak_rps") is None:
            basis.append("the cost band prices hosting by monthly users; per-instance sizing is added to it only "
                         "when scale.peak_rps is given")
    elif be.chosen and host:
        basis.append(f"api_instances_at_peak: none to size, {host.name} scales per request")
    else:
        basis.append("api_instances_at_peak: none to size, no server-side backend")
    return {"peak_users": d.users_peak, "peak_concurrent_users": d.peak_concurrent,
            "peak_rps": d.peak_rps, "api_instances_at_peak": instances,
            "data_volume_gb_12_months": scale.get("data_volume_gb_12_months"), "basis": basis}


def arch_block(arch: ArchDecision) -> dict:
    return {"justification": arch.reason, "driven_by": _paths(arch.fields),
            "alternatives": [{"choice": style, "why_not": why[0].upper() + why[1:].rstrip(".") + "."}
                             for style, why, _ in arch.rejected][:3]}


def _names(pick: Pick) -> str:
    return pick.chosen.name if pick.chosen else "no " + pick.layer


def _host_text(sel) -> str:
    """Hosting parts with the region each one runs in, named as its own provider names it."""
    parts = sel.hosting.parts or ([sel.hosting.chosen] if sel.hosting.chosen else [])
    if not parts:
        return "no hosting"
    placed = [p for p in parts if p.region and p.region != sel.zone]
    names = " + ".join(p.name for p in parts)
    regions = sorted({p.region for p in placed})
    if len(regions) == 1 and len(placed) == len(parts):
        return f"{names} in {regions[0]}"
    return names + (f" ({', '.join(f'{p.name} in {p.region}' for p in placed)})" if placed else "")


def _price_text(cost) -> str:
    lo, hi = cost.min, cost.max
    return "$0/month" if hi == 0 else f"about ${lo:,.0f}-{hi:,.0f}/month"


def summary(sel, cost, drivers: list[str]) -> str:
    style = sel.arch.style.replace("_", " ").capitalize()
    return (f"{style} architecture: {_names(sel.frontend)} with {_names(sel.backend)} and "
            f"{_names(sel.database)} on {_host_text(sel)}. Driven mainly by "
            f"{' and '.join(drivers)}; {_price_text(cost)}.")


def top_drivers(sel, d) -> list[str]:
    picks = [sel.frontend, sel.backend, sel.database, sel.hosting]
    score: dict[str, float] = {}
    for p in picks:
        if p.chosen:
            for m in p.chosen.mods:
                if m.delta > 0:
                    for f in m.fields:
                        if f in FIELD_INDEX:
                            score[f] = score.get(f, 0) + m.delta
    ranked = [f for f, _ in sorted(score.items(), key=lambda t: (-t[1], t[0]))][:2]
    if len(ranked) < 2:
        extra = sorted(d.weights, key=lambda k: (-d.weights[k], k))
        ranked += [f"priorities.{k}" for k in extra if f"priorities.{k}" not in ranked][:2 - len(ranked)]
    return ranked
