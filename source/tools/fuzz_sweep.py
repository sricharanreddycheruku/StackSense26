"""Seeded fuzz sweep for the StackSense engine.

    python tools/fuzz_sweep.py --n 10000 --seed 26 [--save-failures DIR]

Generates random valid scenarios, single-field mutations of scenarios/public/P1-P6 and
invalid/garbage inputs, runs stacksense.engine.run on each and checks invariants I1-I13
(see DESIGN.md section 16). Exits 1 when any invariant fails.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import random
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.dont_write_bytecode = True

from stacksense import engine  # noqa: E402
from stacksense.constraints import HYPERSCALERS, SELF_KINDS, cloud_of  # noqa: E402
from stacksense.kb_loader import load_kb  # noqa: E402
from stacksense.schema import FIELDS, KNOWN_LANGUAGES, KNOWN_LICENCES, PRIORITY_KEYS  # noqa: E402

CORE = ("frontend", "backend", "database", "hosting")
STATUSES = ("ok", "ok_with_warnings", "infeasible", "invalid_input")
SEVERITIES = ("info", "warning", "critical")
TOP_KEYS = ("scenario_id", "engine_version", "status", "summary", "architecture_style", "stack",
            "estimated_monthly_cost_usd", "warnings", "assumptions", "validation_errors")
TOLERATED_CRITICAL = ("NO_COMPLIANT_CANDIDATE", "HARD_CONSTRAINT_CONFLICT", "REQUIREMENT_RELAXED",
                      "CLOUD_PREFERENCE_RELAXED")
PATHS = {f.path for f in FIELDS}
PATHS |= {"project.description", "compliance", "scenario_id"}
LEAF = [f for f in FIELDS if f.type != "object"]
OPTIONAL = [f for f in LEAF if not f.required]

INVARIANTS = {
    "I1": "never raises, JSON-serialisable, no internal error",
    "I2": "output matches the spec section 4 shape",
    "I3": "deterministic (rerun and shuffled key order)",
    "I4": "hard constraints honoured or critical warning; cloud coherence",
    "I5": "cost band vs budget carries a BUDGET_* warning",
    "I6": "0 <= min <= max, finite",
    "I7": "status coherence",
    "I8": "no microservices / k8s outside their gates",
    "I9": "static architecture has no backend or database",
    "I10": "name/id/punctuation change nothing",
    "I10b": "priority +-1 changes at most 1 core layer (rate >= 95%)",
    "I11": "each run under 1 s",
    "I12": "each absent optional field has an assumption",
    "I13": "halving the budget does not raise the cost band minimum (rate >= 97%)",
}

DESCRIPTIONS = [
    "", "A small shop for handmade goods with UPI checkout.", "Offline field data collection in villages with 2g signal.",
    "Realtime collaborative whiteboard, chat and presence.", "Loan app with a ledger, KYC upload and credit scoring model.",
    "Hospital booking portal, patient records, email and sms reminders, schedule reports.",
    "Internal tool for warehouse inventory; low-end devices with 2 gb ram.", "Marketing blog, SEO matters, no logins.",
    "IoT sensor ingestion, time series dashboards, write heavy.", "Multiplayer game server with matchmaking, leaderboard.",
    "Uses Firebase Dynamic Links and Google Universal Analytics today.", "Unicode: café 日本語 \U0001f600 emoji, tabs\tand\nnewlines",
    "Document search with LLM RAG over PDFs for lawyers.", "x" * 3000,
]
EXISTING = ["SQL Server 2019", "Okta", "Active Directory", "Keycloak", "PostgreSQL 9.6", "MySQL 5.7", "Redis",
            "Kafka", "Stripe", "Razorpay", "WordPress", "Python 2.7 scripts", "SAP", "unknown thing", "Jenkins"]
NAMES = ["Demo", "Shop", "Bad name", "café", "A" * 80, "x"]
GEO_ALL = ("india", "eu", "us", "apac", "latam", "africa", "middle_east", "global")


def get(spec, path, default=None):
    node = spec
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def put(spec, path, value):
    node = spec
    parts = path.split(".")
    for p in parts[:-1]:
        if not isinstance(node.get(p), dict):
            node[p] = {}
        node = node[p]
    node[parts[-1]] = value


def drop(spec, path):
    node = spec
    parts = path.split(".")
    for p in parts[:-1]:
        node = node.get(p) if isinstance(node, dict) else None
        if node is None:
            return
    if isinstance(node, dict):
        node.pop(parts[-1], None)


# ---------------------------------------------------------------- generators

def _int(rng, lo, hi):
    lo = int(lo)
    hi = int(min(hi, 10 ** 7)) if hi is not None else 10 ** 7
    if lo >= hi:
        return lo
    return int(round(math.exp(rng.uniform(math.log(lo + 1), math.log(hi + 1))))) - 1 if rng.random() < 0.8 \
        else rng.randint(lo, min(hi, lo + 20))


def gen_value(rng, f):
    p = f.path
    if p == "scenario_id":
        return "S%d" % rng.randint(0, 10 ** 6)
    if p == "project.name":
        return rng.choice(NAMES)
    if p == "project.description":
        return rng.choice(DESCRIPTIONS)
    if p == "team.languages":
        return rng.sample(KNOWN_LANGUAGES, rng.randint(0, 4))
    if p == "infrastructure.existing_systems":
        return rng.sample(EXISTING, rng.randint(0, 3))
    if p == "infrastructure.existing_licences":
        return rng.sample(KNOWN_LICENCES, rng.randint(0, 3))
    if p == "requirements.notifications":
        return rng.sample(["email", "sms", "push"], rng.randint(0, 3))
    if p == "requirements.p95_latency_ms":
        return rng.choice([None, 20, 50, 100, 300, 1000, 5000])
    if p == "requirements.min_device_ram_gb":
        return rng.choice([0.5, 1, 2, 4, 8])
    if p == "requirements.rpo_minutes":
        return rng.choice([0, 5, 15, 60, 1440])
    if f.type == "enum":
        return rng.choice(f.enum)
    if f.type == "boolean":
        return rng.random() < 0.4
    if f.type == "list_enum":
        k = rng.randint(1 if f.required else 0, min(3, len(f.enum)))
        return rng.sample(f.enum, k)
    if f.type == "list_string":
        return rng.sample(EXISTING, rng.randint(0, 2))
    if f.type == "integer":
        lo = f.min if f.min is not None else 0
        hi = 520 if p == "timeline.mvp_weeks" else (60 if "team.size" in p else (f.max or 10 ** 7))
        return _int(rng, lo, hi)
    if f.type == "number":
        if p == "budget.monthly_infra_usd":
            return rng.choice([0, 20, 100, 500, 2000, 10000, 100000, 1000000]) * rng.choice([1, 1, 1.5])
        return round(math.exp(rng.uniform(0, math.log(10 ** 6))), 2)
    return "x"


def random_valid(rng):
    spec = {}
    frac = rng.uniform(0.15, 0.95)
    for f in LEAF:
        if f.required or rng.random() < frac:
            put(spec, f.path, gen_value(rng, f))
    if rng.random() < 0.3:
        put(spec, "project.platforms", rng.sample(["web", "android", "ios", "desktop"], rng.randint(1, 3)))
    if not get(spec, "scale.users_12_months", 0) >= get(spec, "scale.users_at_launch", 0) and rng.random() < 0.8:
        put(spec, "scale.users_12_months", get(spec, "scale.users_at_launch", 0) * rng.randint(2, 50))
    if rng.random() < 0.1:
        spec["surprise_key"] = {"a": 1}
    if rng.random() < 0.1:
        put(spec, "project.platforms", ["api_only"])
    return spec


def mutate(rng, spec):
    """One field changed at a time. Returns the mutated copy."""
    out = copy.deepcopy(spec)
    f = rng.choice([x for x in LEAF if not x.path.startswith("priorities.")])
    cur = get(out, f.path)
    if f.type == "enum":
        put(out, f.path, rng.choice([v for v in f.enum if v != cur] or list(f.enum)))
    elif f.type == "boolean":
        put(out, f.path, not bool(cur))
    elif f.type in ("integer", "number"):
        if cur is None or isinstance(cur, bool) or not isinstance(cur, (int, float)):
            put(out, f.path, gen_value(rng, f))
        else:
            v = cur * rng.choice([0.5, 2, 10])
            put(out, f.path, int(v) if f.type == "integer" else v)
            if f.min is not None and get(out, f.path) < f.min:
                put(out, f.path, f.min)
    elif f.type == "list_enum":
        lst = list(cur) if isinstance(cur, list) else []
        if lst and rng.random() < 0.5 and not (f.required and len(lst) == 1):
            lst.remove(rng.choice(lst))
        else:
            rest = [v for v in f.enum if v not in lst]
            if rest:
                lst.append(rng.choice(rest))
        put(out, f.path, lst)
    elif f.type == "list_string":
        lst = list(cur) if isinstance(cur, list) else []
        if lst and rng.random() < 0.5:
            lst.remove(rng.choice(lst))
        else:
            lst.append(rng.choice(EXISTING + list(KNOWN_LANGUAGES)))
        put(out, f.path, lst)
    else:
        put(out, f.path, gen_value(rng, f))
    return out


def garbage(rng, base):
    kind = rng.randint(0, 11)
    out = copy.deepcopy(base)
    leaf = rng.choice(LEAF)
    if kind == 0:
        return rng.choice([[], [1, 2], "text", 42, None, True, 3.5, [{}], "", 0])
    if kind == 1:
        return {}
    if kind == 2:
        put(out, leaf.path, rng.choice(["5", 1.5, True, None, [], {}, "NaN", "Infinity", "", "\x00"]))
    if kind == 3:
        put(out, leaf.path, rng.choice([float("nan"), float("inf"), -float("inf"), 10 ** 30, -10 ** 12, 1e308]))
    if kind == 4:
        out[rng.choice(["project", "scale", "team", "budget", "timeline", "infrastructure", "requirements",
                        "priorities"])] = rng.choice([[], None, "x", 7, {}, [1]])
    if kind == 5:
        drop(out, rng.choice([f for f in LEAF if f.required]).path)
    if kind == 6:
        put(out, leaf.path, "é中\U0001f600" * rng.randint(1, 50))
    if kind == 7:
        deep = []
        node = deep
        for _ in range(rng.randint(50, 300)):
            nxt = []
            node.append(nxt)
            node = nxt
        put(out, leaf.path, deep)
    if kind == 8:
        put(out, "compliance", rng.choice([["GDPR"], ["x"], "gdpr", [1], [None]]))
    if kind == 9:
        put(out, "priorities." + rng.choice(PRIORITY_KEYS), rng.choice([0, 6, -1, 2.5, "3", None, True]))
    if kind == 10:
        out["requirements"] = {"availability": rng.choice([99.9, 99, "100", 0, None])}
    if kind == 11:
        for _ in range(rng.randint(1, 5)):
            put(out, rng.choice(LEAF).path, rng.choice([None, [], {}, "", 0, -1, "?"]))
    return out


def shuffled(rng, obj):
    if isinstance(obj, dict):
        items = list(obj.items())
        rng.shuffle(items)
        return {k: shuffled(rng, v) for k, v in items}
    if isinstance(obj, list):
        return [shuffled(rng, v) for v in obj]
    return obj


# ------------------------------------------------------------------ KB lookup

class Index:
    def __init__(self):
        self.kb = load_kb()
        self.by_name = {}
        for layer, entries in self.kb.layers.items():
            for e in entries:
                self.by_name.setdefault((layer, e["name"].lower()), (e, list(e.get("offerings", []))))
                for o in e.get("offerings", []):
                    self.by_name.setdefault((layer, o["name"].lower()), (e, [o]))
        self.unmatched = 0

    def match(self, layer, choice):
        hit = self.by_name.get((layer, str(choice).lower()))
        if hit is None:
            self.unmatched += 1
        return hit


def _regions(kb, off, zone):
    prov = kb.provider(off["provider"]) or {}
    return set(off.get("regions", {}).get(zone, [])) | set(prov.get("regions", {}).get(zone, []))


def offering_problem(kb, layer, entry, off, spec, comp):
    """First hard-constraint problem for one offering, or None. Written independently of constraints.passes."""
    kind = off["kind"]
    cloud = cloud_of(off["provider"])
    deployment = get(spec, "infrastructure.deployment_model", "managed_cloud")
    pref = get(spec, "infrastructure.cloud_preference", "any")
    zone = get(spec, "infrastructure.data_residency", "none")
    compliance = get(spec, "compliance", []) or []
    prov = kb.provider(off["provider"]) or {}
    if layer in ("backend", "database", "hosting") and deployment in ("on_premise", "air_gapped") \
            and kind not in SELF_KINDS:
        return f"{deployment} but {off['name']} is a {kind} service"
    if layer not in ("frontend", "payments", "notifications") and deployment in ("on_premise", "air_gapped") \
            and kind not in SELF_KINDS:
        return f"{deployment} but {off['name']} ({layer}) is a {kind} service"
    if layer in ("payments", "notifications") and deployment == "air_gapped" and kind not in SELF_KINDS:
        return f"air_gapped but {off['name']} is a {kind} service"
    reused = bool(get(spec, "infrastructure.existing_systems", None)) and layer not in ("hosting", "database")
    if layer != "frontend" and not reused and pref in HYPERSCALERS and kind != "self" and cloud != pref \
            and (layer in ("hosting", "database") or cloud in HYPERSCALERS):
        return f"cloud_preference {pref} but {off['name']} is {cloud}"
    if layer != "frontend" and not reused and pref == "none" and deployment == "managed_cloud" \
            and cloud in HYPERSCALERS and kind != "self":
        return f"cloud_preference none but {off['name']} is {cloud}"
    if zone in ("india", "eu", "us") and kind != "self" and layer in ("backend", "database", "hosting"):
        regs = _regions(kb, off, zone)
        if not off.get("regions", {}).get(zone):
            return f"residency {zone} but {off['name']} lists no {zone} region"
        region = comp.get("region")
        if region and region not in regs:
            return f"residency {zone} but region {region} is not a {zone} region of {off['provider']}"
        if layer != "hosting" and prov.get("full_residency", {}).get(zone) is False:
            return f"residency {zone} but {off['provider']} cannot keep logs and backups there"
    if kind != "self" and layer in ("backend", "database", "hosting"):
        have = set(off.get("attestations", [])) | set(prov.get("attestations", []))
        for code in compliance:
            row = kb.compliance.get(code) if isinstance(code, str) else None
            for att in (row or {}).get("attestation_required", []):
                if att not in have:
                    return f"compliance {code} needs {att} but {off['name']} does not list it"
    return None


def _crit_tolerated(out):
    return any(w.get("severity") == "critical" and w.get("code") in TOLERATED_CRITICAL
               for w in out.get("warnings", []))


# --------------------------------------------------------------------- checks

class Report:
    def __init__(self, save_dir):
        self.checked = {k: 0 for k in INVARIANTS}
        self.failed = {k: 0 for k in INVARIANTS}
        self.examples = {k: [] for k in INVARIANTS}
        self.save_dir = save_dir
        self.saved = 0

    def check(self, inv, ok, msg, scen):
        self.checked[inv] += 1
        if ok:
            return
        self.failed[inv] += 1
        if len(self.examples[inv]) < 3:
            self.examples[inv].append(msg)
            if self.save_dir:
                os.makedirs(self.save_dir, exist_ok=True)
                path = os.path.join(self.save_dir, f"{inv}_{len(self.examples[inv])}.json")
                try:
                    with open(path, "w", encoding="utf-8") as fh:
                        json.dump(scen, fh, indent=1, ensure_ascii=False)
                    self.saved += 1
                except (TypeError, ValueError, RecursionError):
                    pass


def check_shape(out):
    """Return a list of shape problems (empty when the output matches the schema)."""
    errs = []
    if not isinstance(out, dict):
        return ["output is not an object"]
    for k in TOP_KEYS:
        if k not in out:
            errs.append(f"missing key {k}")
    if errs:
        return errs
    if out["status"] not in STATUSES:
        errs.append(f"bad status {out['status']!r}")
    if not isinstance(out["summary"], str) or not out["summary"]:
        errs.append("summary empty")
    for key in ("warnings", "assumptions", "validation_errors"):
        if not isinstance(out[key], list):
            errs.append(f"{key} not a list")
    for w in out["warnings"] if isinstance(out["warnings"], list) else []:
        if not (isinstance(w, dict) and isinstance(w.get("code"), str) and w.get("severity") in SEVERITIES
                and isinstance(w.get("message"), str) and isinstance(w.get("fields"), list)):
            errs.append(f"bad warning {str(w)[:80]}")
            break
    cost = out["estimated_monthly_cost_usd"]
    if not isinstance(cost, dict) or not {"min", "max"} <= set(cost):
        errs.append("cost band malformed")
    stack = out["stack"]
    if out["status"] in ("invalid_input", "infeasible"):
        if stack is not None:
            errs.append("stack must be null")
        return errs
    if not isinstance(stack, dict):
        return errs + ["stack missing"]
    comps = [(k, stack.get(k)) for k in CORE] + [("supporting", c) for c in stack.get("supporting") or []]
    for layer, c in comps:
        if not isinstance(c, dict):
            errs.append(f"{layer} component missing")
            continue
        if layer == "supporting" and not c.get("layer"):
            errs.append("supporting component without layer")
        if not (isinstance(c.get("choice"), str) and c["choice"]):
            errs.append(f"{layer} choice empty")
        if not (isinstance(c.get("justification"), str) and c["justification"]):
            errs.append(f"{layer} justification empty")
        db = c.get("driven_by")
        if not isinstance(db, list) or any(p not in PATHS for p in db):
            errs.append(f"{layer} driven_by invalid {db}")
        alts = c.get("alternatives")
        if not isinstance(alts, list) or any(not (isinstance(a, dict) and a.get("choice") and a.get("why_not"))
                                             for a in alts):
            errs.append(f"{layer} alternatives malformed")
        elif layer in CORE and c.get("choice") != "None" and len(alts) < 1:
            errs.append(f"{layer} has no alternatives")
    return errs


def signature(out):
    stack = out.get("stack")
    return json.dumps({
        "status": out.get("status"), "style": out.get("architecture_style"),
        "stack": stack and {k: (v and v.get("choice")) for k, v in stack.items() if k in CORE},
        "support": stack and [(c.get("layer"), c.get("choice")) for c in stack.get("supporting", [])],
        "cost": out.get("estimated_monthly_cost_usd", {}).get("min"),
        "codes": [w["code"] for w in out.get("warnings", [])],
    }, sort_keys=True, default=str)


def core_choices(out):
    s = out.get("stack")
    return None if not s else {k: (s[k] or {}).get("choice") for k in CORE}


def run_checked(raw):
    t0 = time.perf_counter()
    try:
        out = engine.run(copy.deepcopy(raw))
        err = None
    except BaseException as exc:  # noqa: BLE001
        out, err = None, f"{type(exc).__name__}: {exc}"
    return out, err, time.perf_counter() - t0


def check_one(rep, idx, rng, raw, i):
    out, err, dt = run_checked(raw)
    rep.check("I1", err is None, f"raised {err}", raw)
    if out is None:
        return None
    try:
        text = json.dumps(out, allow_nan=False)
        ok = True
    except (TypeError, ValueError, RecursionError) as exc:
        text, ok = "", False
        rep.check("I1", False, f"not JSON-serialisable: {exc}", raw)
    if ok:
        rep.check("I1", True, "", raw)
    internal = [e for e in out.get("validation_errors", []) if "internal error" in str(e.get("message", ""))] \
        if isinstance(out, dict) and isinstance(out.get("validation_errors"), list) else []
    rep.check("I1", not internal, f"internal error output: {internal[:1]}", raw)
    rep.check("I11", dt < 1.0, f"run took {dt:.2f}s", raw)

    shape = check_shape(out)
    rep.check("I2", not shape, "; ".join(shape[:3]), raw)
    if shape or not ok:
        return out

    # I3 determinism (every third input, to keep the sweep short)
    if i % 3 == 0:
        out2, _, _ = run_checked(raw)
        same = out2 is not None and json.dumps(out2, allow_nan=False) == text
        rep.check("I3", same, "second run differs", raw)
    if i % 3 == 0 and isinstance(raw, dict):
        out3, _, _ = run_checked(shuffled(rng, raw))
        rep.check("I3", out3 is not None and json.dumps(out3, sort_keys=True) == json.dumps(out, sort_keys=True),
                  "shuffled key order changes the output", raw)

    status = out["status"]
    warnings = out["warnings"]
    cost = out["estimated_monthly_cost_usd"]
    codes = {w["code"] for w in warnings}
    # I7 status coherence
    sev = {w["severity"] for w in warnings}
    problems = []
    if (status == "invalid_input") != bool(out["validation_errors"]):
        problems.append("invalid_input iff validation_errors")
    if (out["stack"] is None) != (status in ("invalid_input", "infeasible")):
        problems.append("stack null iff invalid/infeasible")
    if status == "ok" and sev & {"warning", "critical"}:
        problems.append("status ok with warning/critical")
    if status == "ok_with_warnings" and not sev & {"warning", "critical"}:
        problems.append("ok_with_warnings without warning/critical")
    rep.check("I7", not problems, "; ".join(problems), raw)

    if out["stack"] is None:
        return out
    spec = raw
    # I6
    lo, hi = cost.get("min"), cost.get("max")
    good = all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (lo, hi)) \
        and 0 <= lo <= hi
    rep.check("I6", good, f"cost band {lo}..{hi}", raw)
    # I5
    budget = get(spec, "budget.monthly_infra_usd")
    if good and isinstance(budget, (int, float)) and not isinstance(budget, bool):
        over = hi > budget + 1e-9
        has = any(c.startswith("BUDGET_") for c in codes)
        rep.check("I5", (not over) or has, f"cost max {hi} > budget {budget} without BUDGET_* warning", raw)
        rep.check("I5", not (lo > budget + 1e-9) or "BUDGET_EXCEEDED" in codes or _crit_tolerated(out),
                  f"cost min {lo} > budget {budget} without BUDGET_EXCEEDED", raw)
    # I8, I9
    style = out["architecture_style"]
    size = get(spec, "team.size", 1)
    size12 = get(spec, "team.size_in_12_months", size)
    devops = bool(get(spec, "team.has_devops", False))
    k8s_ok = style in ("service_oriented", "microservices") and devops and size >= 8
    if style == "microservices":
        s2 = (size >= 12 or size12 >= 15) and devops and get(spec, "scale.users_12_months", 0) >= 1_000_000 \
            and get(spec, "priorities.scalability", 3) >= 4 and get(spec, "team.experience") in ("senior", "mixed")
        rep.check("I8", s2, "microservices chosen without S2 conditions", raw)
    else:
        rep.check("I8", True, "", raw)
    if style == "static":
        st = out["stack"]
        rep.check("I9", all(((st.get(k) or {}).get("choice") in (None, "None")) for k in ("backend", "database")),
                  "static architecture has a backend or database", raw)
    # I4 hard constraints and cloud coherence
    check_hard(rep, idx, out, spec, k8s_ok)
    # I12 optional-field assumptions
    if isinstance(spec, dict):
        miss = [f.path for f in OPTIONAL if get(spec, f.path, _MISSING) is _MISSING
                and not any(a.startswith(f.path + " not provided") for a in out["assumptions"])]
        rep.check("I12", not miss, "no assumption for " + ", ".join(miss[:4]), raw)
    return out


_MISSING = object()


def check_hard(rep, idx, out, spec, k8s_ok):
    kb = idx.kb
    stack = out["stack"]
    tolerated = _crit_tolerated(out)
    comps = [(k, stack.get(k)) for k in CORE] + [(c.get("layer"), c) for c in stack.get("supporting", [])]
    msgs = []
    clouds = {}
    for layer, c in comps:
        if not c or c.get("choice") in (None, "None"):
            continue
        name = c["choice"]
        hit = kb.deprecated_hit(name)
        if hit:
            msgs.append(f"{layer}: deprecated {name}")
        if layer == "frontend":
            continue
        found = idx.match(layer, name)
        if not found:
            continue
        entry, offs = found
        if layer == "hosting" and "k8s" in entry.get("capabilities", []) and not k8s_ok:
            msgs.append(f"hosting: k8s {name} outside the gate")
        if not offs:
            continue
        bad = [offering_problem(kb, layer, entry, o, spec, c) for o in offs]
        if all(bad):
            msgs.append(f"{layer}: {bad[0]}")
        cl = {cloud_of(o["provider"]) for o in offs if o["kind"] != "self"}
        clouds[layer] = cl.pop() if len(cl) == 1 else None
    bad_deprecated = [m for m in msgs if "deprecated" in m or "k8s" in m]
    other = [m for m in msgs if m not in bad_deprecated]
    rep.check("I4", not bad_deprecated and (not other or tolerated), "; ".join(msgs[:3]), spec)
    # cross-cloud core coherence
    host = set()
    hc = stack.get("hosting") or {}
    name = str(hc.get("choice") or "")
    parts = name.split(" + ")
    cands = [name] + [" + ".join(parts[i:j]) for i in range(len(parts)) for j in range(i + 1, len(parts) + 1)]
    for part in cands:
        found = idx.match("hosting", part.strip()) or idx.match("backend", part.strip())
        if found:
            host |= {cloud_of(o["provider"]) for o in found[1] if o["kind"] != "self"}
    mism = [f"{lay} on {clouds[lay]} but host clouds are {sorted(host) or None}" for lay in ("backend", "database")
            if clouds.get(lay) in HYPERSCALERS and clouds[lay] not in host]
    rep.check("I4", not mism or tolerated, "cross-cloud: " + "; ".join(mism), spec)


# ---------------------------------------------------------------------- sweep

def priority_pair(rng, spec):
    keys = [k for k in PRIORITY_KEYS]
    k = rng.choice(keys)
    cur = get(spec, "priorities." + k, 3)
    if not isinstance(cur, int) or isinstance(cur, bool):
        return None
    opts = [v for v in (cur - 1, cur + 1) if 1 <= v <= 5]
    if not opts:
        return None
    out = copy.deepcopy(spec)
    put(out, "priorities." + k, rng.choice(opts))
    return out


def sensitivity(rep, rng, raw, out):
    """I10: name, scenario_id and description punctuation must not change the recommendation."""
    if not isinstance(raw, dict) or out is None or out.get("status") in (None, "invalid_input"):
        return
    base = signature(out)
    variants = []
    v = copy.deepcopy(raw)
    put(v, "project.name", "Renamed project %d" % rng.randint(0, 999))
    variants.append(("project.name", v))
    v = copy.deepcopy(raw)
    v["scenario_id"] = "other-%d" % rng.randint(0, 999)
    variants.append(("scenario_id", v))
    desc = get(raw, "project.description")
    if isinstance(desc, str) and desc:
        v = copy.deepcopy(raw)
        put(v, "project.description", desc.replace(",", ";").replace(".", "!") + " !")
        variants.append(("description punctuation", v))
    for label, v in variants:
        o, _, _ = run_checked(v)
        rep.check("I10", o is not None and signature(o) == base, f"{label} change altered the result", raw)


BUDGET_RISES: list[float] = []


def budget_monotonic(rep, raw, out):
    """I13: halving the budget should not make the recommended stack cost more (checked as a rate)."""
    budget = get(raw, "budget.monthly_infra_usd")
    if not isinstance(raw, dict) or isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget <= 0:
        return
    if out is None or not out.get("stack"):
        return
    lower = copy.deepcopy(raw)
    put(lower, "budget.monthly_infra_usd", budget / 2)
    o, _, _ = run_checked(lower)
    if o is None or not o.get("stack"):
        return
    a = out["estimated_monthly_cost_usd"]["min"]
    b = o["estimated_monthly_cost_usd"]["min"]
    if a > 0:
        BUDGET_RISES.append(b / a)
    rep.check("I13", b <= a + 1e-9, f"budget {budget} costs min {a}, budget {budget / 2} costs min {b}", raw)


def sweep(args):
    rng = random.Random(args.seed)
    idx = Index()
    rep = Report(args.save_failures)
    base_dir = os.path.join(ROOT, "scenarios", "public")
    bases = {}
    for name in sorted(os.listdir(base_dir)):
        if name.endswith(".json"):
            with open(os.path.join(base_dir, name), encoding="utf-8-sig") as fh:
                bases[name[:-5]] = json.load(fh)
    base_out = {k: engine.run(copy.deepcopy(v)) for k, v in bases.items()}
    stab = {"n": 0, "le1": 0, "le2": 0, "worst": []}
    counts = {"valid": 0, "mutation": 0, "garbage": 0}
    statuses = {}
    t0 = time.perf_counter()

    def stability(spec, out, other):
        o2, _, _ = run_checked(other)
        a, b = core_choices(out), core_choices(o2)
        if a is None or b is None:
            return
        diff = sum(1 for k in CORE if a[k] != b[k])
        stab["n"] += 1
        stab["le1"] += diff <= 1
        stab["le2"] += diff <= 2
        if diff > 1 and len(stab["worst"]) < 3:
            stab["worst"].append((diff, spec, other))

    for i in range(args.n):
        slot = i % 10
        if slot < 4:
            kind, raw = "valid", random_valid(rng)
        elif slot < 8:
            kind = "mutation"
            pid = rng.choice(sorted(bases))
            if rng.random() < 0.25:
                raw = priority_pair(rng, bases[pid]) or mutate(rng, bases[pid])
            else:
                raw = mutate(rng, bases[pid])
        else:
            kind = "garbage"
            raw = garbage(rng, rng.choice(list(bases.values())) if rng.random() < 0.7 else random_valid(rng))
        counts[kind] += 1
        out = check_one(rep, idx, rng, raw, i)
        if out is None:
            continue
        statuses[out.get("status")] = statuses.get(out.get("status"), 0) + 1
        if kind != "garbage" and i % 4 == 0:
            sensitivity(rep, rng, raw, out)
        if kind != "garbage" and i % 5 == 0:
            budget_monotonic(rep, raw, out)
        if kind == "valid" and i % 8 == 0 and out.get("stack"):
            other = priority_pair(rng, raw)
            if other:
                stability(raw, out, other)
        if kind == "mutation" and out.get("stack") and i % 3 == 0:
            other = priority_pair(rng, raw)
            if other:
                stability(raw, out, other)
    # stability over the six public scenarios, every priority +-1
    for pid, spec in sorted(bases.items()):
        for k in PRIORITY_KEYS:
            cur = get(spec, "priorities." + k, 3)
            for v in (cur - 1, cur + 1):
                if 1 <= v <= 5 and base_out[pid].get("stack"):
                    other = copy.deepcopy(spec)
                    put(other, "priorities." + k, v)
                    stability(spec, base_out[pid], other)
    return rep, stab, counts, statuses, idx, time.perf_counter() - t0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fuzz the StackSense engine and check invariants I1-I13.")
    ap.add_argument("--n", type=int, default=10000, help="number of generated inputs (use 200 for a quick run)")
    ap.add_argument("--seed", type=int, default=26)
    ap.add_argument("--save-failures", metavar="DIR", default=None, help="save failing scenarios under DIR")
    ap.add_argument("--summary", metavar="FILE", default=None, help="also write the results as JSON to FILE")
    args = ap.parse_args(argv)
    rep, stab, counts, statuses, idx, secs = sweep(args)

    rate1 = stab["le1"] / stab["n"] if stab["n"] else 1.0
    rate2 = stab["le2"] / stab["n"] if stab["n"] else 1.0
    rep.checked["I10b"] = stab["n"]
    rep.failed["I10b"] = 0 if rate1 >= 0.95 else stab["n"] - stab["le1"]
    for diff, spec, other in stab["worst"]:
        rep.examples["I10b"].append(f"{diff} core layers changed for a priority +-1 step")
        if args.save_failures and len(rep.examples["I10b"]) <= 3:
            os.makedirs(args.save_failures, exist_ok=True)
            with open(os.path.join(args.save_failures, f"I10b_{len(rep.examples['I10b'])}.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({"base": spec, "mutated": other}, fh, indent=1, ensure_ascii=False)

    i13n = rep.checked["I13"]
    i13rate = 1 - rep.failed["I13"] / i13n if i13n else 1.0
    worst = max(BUDGET_RISES, default=1.0)
    if i13rate >= 0.97:
        rep.failed["I13"] = 0

    print(f"StackSense fuzz sweep: n={args.n} seed={args.seed} "
          f"(valid {counts['valid']}, mutations {counts['mutation']}, garbage {counts['garbage']})")
    print("statuses: " + ", ".join(f"{k}={v}" for k, v in sorted(statuses.items(), key=lambda x: str(x[0]))))
    print(f"{'inv':<5}{'checked':>9}{'failed':>8}  description")
    for inv, desc in INVARIANTS.items():
        print(f"{inv:<5}{rep.checked[inv]:>9}{rep.failed[inv]:>8}  {desc}")
    print(f"priority +-1 stability: <=1 layer changed {rate1:.1%}, <=2 layers {rate2:.1%} of {stab['n']} pairs")
    print(f"budget halving: cost minimum not raised in {i13rate:.1%} of {i13n} pairs; largest rise x{worst:.2f}")
    print(f"components without an exact KB name match (not re-checked): {idx.unmatched}")
    failing = [k for k in INVARIANTS if rep.failed[k]]
    for inv in failing:
        print(f"\n{inv} failing examples (first {len(rep.examples[inv])} of {rep.failed[inv]}):")
        for m in rep.examples[inv]:
            print("  - " + m[:300])
    if args.save_failures and rep.saved:
        print(f"\nsaved {rep.saved} failing scenarios under {args.save_failures}")
    print(f"\nruntime {secs:.1f}s ({secs / max(args.n, 1) * 1000:.1f} ms per input)")
    print("RESULT: " + ("FAIL" if failing else "PASS"))
    if args.summary:
        summary = {
            "n": args.n, "seed": args.seed, "inputs": counts, "statuses": {str(k): v for k, v in sorted(
                statuses.items(), key=lambda x: str(x[0]))},
            "invariants": {inv: {"description": desc, "checked": rep.checked[inv], "failed": rep.failed[inv]}
                           for inv, desc in INVARIANTS.items()},
            "priority_stability": {"pairs": stab["n"], "le1_layer": round(rate1, 4), "le2_layers": round(rate2, 4)},
            "budget_halving": {"pairs": i13n, "min_not_raised": round(i13rate, 4), "largest_rise": round(worst, 2)},
            "ms_per_input": round(secs / max(args.n, 1) * 1000, 1),
            "result": "FAIL" if failing else "PASS",
        }
        with open(args.summary, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=1)
            fh.write("\n")
    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(main())
