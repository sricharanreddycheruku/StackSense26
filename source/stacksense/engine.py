"""Pipeline: validate -> derive -> constraints -> architecture -> recommend -> cost -> conflicts -> explain."""
from __future__ import annotations

import copy

from . import ENGINE_VERSION, KB_AS_OF, architecture, conflicts, constraints, cost, derive, explain, recommend, validate
from .kb_loader import load_kb
from .model import SEVERITY_RANK, Warn

LLM_NOTE = "No language model is used; the stack is chosen by deterministic rules and the knowledge base."
CORE = ("frontend", "backend", "database", "hosting")


def _set_path(spec: dict, path: str, value) -> None:
    node = spec
    parts = path.split(".")
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value


def _warn_dict(w: Warn) -> dict:
    return {"code": w.code, "severity": w.severity, "message": w.message, "fields": list(w.fields)}


def _merge_warnings(*groups) -> list[dict]:
    seen, out = set(), []
    for w in (x for g in groups for x in g):
        key = (w.code, w.severity, w.message, tuple(w.fields))
        if key not in seen:
            seen.add(key)
            out.append(w)
    out.sort(key=lambda w: (SEVERITY_RANK[w.severity], w.code, tuple(w.fields), w.message))
    return [_warn_dict(w) for w in out]


def _dedupe(items) -> list[str]:
    out = []
    for i in items:
        if i not in out:
            out.append(i)
    return out


def _base(raw, status: str) -> dict:
    sid = raw.get("scenario_id") if isinstance(raw, dict) else None
    return {"scenario_id": sid if isinstance(sid, str) else None, "engine_version": ENGINE_VERSION,
            "status": status}


def _tail(out: dict, llm) -> dict:
    out["llm"] = {"enabled": False, "note": LLM_NOTE}
    out["kb_as_of"] = KB_AS_OF
    return out


def _invalid(raw, errors, warnings, assumptions, summary: str, status="invalid_input", relax=None) -> dict:
    out = _base(raw, status)
    out.update({
        "summary": summary,
        "architecture_style": None,
        "stack": None,
        "estimated_monthly_cost_usd": {"min": None, "max": None,
                                       "assumptions": ["Not estimated because the input is " +
                                                       ("invalid." if status == "invalid_input" else "infeasible.")]},
        "warnings": warnings,
        "assumptions": assumptions,
        "validation_errors": [{"field": e.field, "message": e.message} for e in errors],
    })
    if relax is not None:
        out["suggested_relaxations"] = relax
    return out


def _run(raw, llm, trace) -> dict:
    kb = load_kb()
    v = validate.validate(raw)
    vw = _merge_warnings(v.warnings)
    if v.errors:
        first = v.errors[0]
        msg = f"The input is invalid: {len(v.errors)} problem(s), first at '{first.field or 'scenario'}': {first.message}."
        return _tail(_invalid(raw, v.errors, vw, [], msg), llm)

    spec = v.spec
    d = derive.derive(v, kb)
    c = constraints.build(spec, d, kb)
    base_warns = list(v.warnings)

    def infeasible(conf: list[Warn], relax: list[str], assumptions) -> dict:
        names = ", ".join(w.code for w in conf)
        msg = f"No stack can satisfy the requirements together ({names}): {conf[0].message}"
        return _tail(_invalid(raw, [], _merge_warnings(base_warns, conf), assumptions, msg,
                              status="infeasible", relax=relax), llm)

    base_assume = list(v.assumptions) + list(d.assumptions)
    if c.conflicts:
        return infeasible(c.conflicts, list(c.suggestions), base_assume)

    # relaxations decided by the constraint builder and the availability pre-relax
    notes = list(c.notes)
    relax_notes = list(c.relaxations)
    spec2 = copy.deepcopy(spec)
    for path, val in sorted(c.relaxed_requirements.items()):
        _set_path(spec2, path, val)
    spec2, pre_w, pre_notes = conflicts.pre_relax(spec2)
    notes += pre_w
    relax_notes += pre_notes
    if spec2 != spec:
        v2 = validate.Validated(spec2, [], v.assumptions, v.warnings, v.provided)
        d = derive.derive(v2, kb)
        c2 = constraints.build(spec2, d, kb)
        c2.notes = notes + [n for n in c2.notes if n not in notes]
        c2.relaxations = relax_notes + [r for r in c2.relaxations if r not in relax_notes]
        c, spec = c2, spec2
    else:
        c.notes, c.relaxations = notes, relax_notes

    arch = architecture.choose(spec, d)
    try:
        sel = recommend.select(spec, d, c, kb, arch)
    except recommend.NoCandidate as exc:
        warn, relax = conflicts.no_candidate(exc.layer, exc.reasons, ())
        return infeasible([warn], relax, base_assume + c.relaxations)

    est = cost.estimate(sel, spec, d, c, kb)
    budget = float(spec["budget"]["monthly_infra_usd"])
    budget_relax: list[str] = []
    if est.min > budget:
        # over budget: rank by price, at this budget and at a floor of 10, and keep the cheapest result
        for level in (budget, 10):
            try:
                tight = {**spec, "budget": {**spec["budget"], "monthly_infra_usd": level}}
                sel2 = recommend.select(tight, d, c, kb, arch, budget_mode=True)
                est2 = cost.estimate(sel2, spec, d, c, kb)
                if (est2.min, est2.max) < (est.min, est.max):
                    sel, est = sel2, est2
            except recommend.NoCandidate:
                pass
    if est.min > budget:
        budget_relax.append(f"RELAXED: budget.monthly_infra_usd: the lowest-cost stack found that meets the hard "
                            f"constraints costs at least ${est.min:,.0f}/month against a budget of ${budget:,.0f}")

    found = conflicts.check(spec, d, c, sel, est, kb, v.provided)
    warns = _merge_warnings(base_warns, c.notes, found)
    status = "ok_with_warnings" if any(w["severity"] in ("warning", "critical") for w in warns) else "ok"

    def layer_cost(layer: str) -> tuple:
        rows = [r for r in est.breakdown if r["layer"] == layer]
        return round(sum(r["min"] for r in rows), 2), round(sum(r["max"] for r in rows), 2)

    stack = {k: explain.component(getattr(sel, k), spec, d, kb, layer_cost=layer_cost(k)) for k in CORE}
    stack["supporting"] = [explain.component(p, spec, d, kb) for p in sel.supporting]
    drivers = explain.top_drivers(sel, d)
    out = _base(raw, status)
    out.update({
        "summary": explain.summary(sel, est, drivers),
        "architecture_style": sel.arch.style,
        "architecture": explain.arch_block(sel.arch),
        "region": {"zone": sel.zone, "primary": sel.region, "dr": sel.dr_region},
        "stack": stack,
        "estimated_monthly_cost_usd": {"min": est.min, "max": est.max, "assumptions": est.assumptions,
                                       "breakdown": est.breakdown},
        "capacity": explain.capacity(spec, d, sel, kb),
        "warnings": warns,
        "assumptions": _dedupe(base_assume + c.relaxations + sel.notes + budget_relax),
        "validation_errors": [],
        "description_signals": [{"keyword": s.keyword, "field": s.field, "value": s.value,
                                "applied": s.applied, "reason": s.reason} for s in d.signals],
    })
    if trace:
        out["trace"] = {"weights": dict(sorted(d.weights.items())),
                        "chosen": {k: (getattr(sel, k).chosen.score if getattr(sel, k).chosen else None) for k in CORE},
                        "runners_up": {k: [[r.name, r.score] for r in getattr(sel, k).runners_up] for k in CORE}}
    return _tail(out, llm)


def run(raw, *, llm=None, trace: bool = False) -> dict:
    """Never raises; any internal failure becomes an invalid_input result."""
    try:
        return _run(raw, llm, trace)
    except Exception as exc:  # noqa: BLE001
        out = _invalid(raw, [], [], [], "The engine could not process this input.")
        out["validation_errors"] = [{"field": "", "message": f"internal error: {type(exc).__name__}"}]
        return _tail(out, llm)
