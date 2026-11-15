"""Input sensitivity study: change one input field at a time on each public scenario and count what moves.

For every field in the schema the tool builds variants (other enum values, flipped booleans (both values when absent), x0.1 and x10 for
numbers, 1 and 5 for priorities, each list value toggled), runs the engine, and reports how often the core stack
(architecture, frontend, backend, database, hosting) changed and how often anything in the output changed
(supporting services, warnings or the cost band). Display-only fields such as project.name should never change
anything.

    python3 -B tools/sensitivity.py                 # markdown table on stdout
    python3 -B tools/sensitivity.py --json FILE     # also write the raw numbers
"""
import argparse
import copy
import glob
import json
import os
import sys

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)

from stacksense import engine  # noqa: E402
from stacksense.schema import FIELDS  # noqa: E402

CORE = ("frontend", "backend", "database", "hosting")
LANGS = (["python"], ["javascript", "typescript"], ["java", "kotlin"], ["go"], ["csharp"], ["php"])


def get(spec, path):
    node = spec
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def put(spec, path, value):
    out = copy.deepcopy(spec)
    node = out
    parts = path.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
    return out


def variants(f, cur):
    if f.path == "project.name":
        return ["Renamed project"]
    if f.path == "team.languages":
        return [v for v in LANGS if v != cur]
    if f.type == "enum":
        return [v for v in f.enum if v != cur]
    if f.type == "boolean":
        # an absent flag may be derived either way, so both values are tried
        return [True, False] if cur is None else [not cur]
    if f.type in ("integer", "number"):
        if f.path.startswith("priorities."):
            return [v for v in (1, 5) if v != cur]
        base = cur if isinstance(cur, (int, float)) and cur > 0 else (f.default if isinstance(f.default, (int, float))
                                                                       and f.default > 0 else None)
        if base is None:
            return []
        vals = []
        for k in (0.1, 10):
            v = base * k
            v = int(round(v)) if f.type == "integer" else round(v, 3)
            lo = f.min if f.min is not None else v
            hi = f.max if f.max is not None else v
            v = min(max(v, lo), hi)
            if v != cur and v not in vals:
                vals.append(v)
        return vals
    if f.type == "list_enum":
        cur = list(cur or [])
        out = []
        for v in f.enum:
            nxt = [x for x in cur if x != v] if v in cur else cur + [v]
            if nxt or f.path != "project.platforms":
                out.append(nxt)
        return out
    return []


def signature(out):
    st = out.get("stack") or {}
    core = (out.get("status"), out.get("architecture_style"),
            *((st.get(k) or {}).get("choice") for k in CORE))
    sup = tuple(sorted((x.get("layer"), x.get("choice")) for x in st.get("supporting") or []))
    warn = tuple(sorted({w["code"] for w in out.get("warnings") or []}))
    c = out.get("estimated_monthly_cost_usd") or {}
    return core, (sup, warn, c.get("min"), c.get("max"))


def main(argv=None):
    ap = argparse.ArgumentParser(description="One-field-at-a-time sensitivity study over the public scenarios.")
    ap.add_argument("--scenarios", default=os.path.join(SRC, "scenarios", "public"))
    ap.add_argument("--json", metavar="FILE", default=None)
    args = ap.parse_args(argv)
    bases = []
    for p in sorted(glob.glob(os.path.join(args.scenarios, "*.json"))):
        with open(p, encoding="utf-8-sig") as fh:
            bases.append(json.load(fh))
    rows = []
    for f in FIELDS:
        if f.type in ("object", "string") and f.path != "project.name":
            continue
        if f.type == "list_string" and f.path != "team.languages":
            continue
        n = core_moved = any_moved = 0
        for base in bases:
            ref_core, ref_rest = signature(engine.run(base))
            for v in variants(f, get(base, f.path)):
                c, r = signature(engine.run(put(base, f.path, v)))
                n += 1
                core_moved += c != ref_core
                any_moved += c != ref_core or r != ref_rest
        if n:
            rows.append({"field": f.path, "extra": f.extra, "variants": n,
                         "core_changed": core_moved, "anything_changed": any_moved})
    total = sum(r["variants"] for r in rows)
    print("| Field | Variants | Core stack changed | Any output changed |")
    print("|---|---:|---:|---:|")
    for r in rows:
        mark = " (extra)" if r["extra"] else ""
        print(f"| `{r['field']}`{mark} | {r['variants']} | {r['core_changed'] / r['variants']:.0%} "
              f"| {r['anything_changed'] / r['variants']:.0%} |")
    moving = [r for r in rows if r["anything_changed"]]
    print(f"\n{len(rows)} fields, {total} single-field variants over {len(bases)} scenarios; "
          f"{len(moving)} fields change the output at least once.")
    silent = [r["field"] for r in rows if not r["anything_changed"]]
    if silent:
        print("fields that never changed the output: " + ", ".join(silent))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"scenarios": len(bases), "variants": total, "fields": rows}, fh, indent=1)
            fh.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
