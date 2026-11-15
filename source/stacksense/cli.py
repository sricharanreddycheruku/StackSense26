"""Command line: run scenarios, validate a file, suggest fields from a description."""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import describe, engine, validate


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


_UNREADABLE = object()


def _read(path: str):
    """Parsed JSON, or the raw text when it is not valid JSON (the validator reports where it breaks)."""
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        print(f"cannot read {path}: {exc.strerror or type(exc).__name__}", file=sys.stderr)
        return _UNREADABLE
    try:
        return json.loads(text)
    except ValueError:
        return text


def _items(path: str):
    """(name, scenario) pairs for a file, a directory or a combined file."""
    if os.path.isdir(path):
        for name in sorted(os.listdir(path)):
            if name.lower().endswith(".json"):
                yield from _items(os.path.join(path, name))
        return
    stem = os.path.splitext(os.path.basename(path))[0]
    data = _read(path)
    if data is _UNREADABLE:
        return
    many = data if isinstance(data, list) and data and all(isinstance(x, dict) for x in data) else \
        data.get("scenarios") if isinstance(data, dict) and isinstance(data.get("scenarios"), list) else None
    if many is None:
        yield stem, data
        return
    for i, sc in enumerate(many, 1):
        sid = sc.get("scenario_id") if isinstance(sc, dict) else None
        yield (sid if isinstance(sid, str) and sid else f"{stem}_{i}"), sc


def _safe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name) or "scenario"


def _line(name: str, target: str, out: dict) -> str:
    st = out.get("stack")
    if not st:
        return f"{name} -> {target} [{out['status']}]"
    names = " | ".join(st[k]["choice"] for k in ("frontend", "backend", "database", "hosting"))
    return f"{name} -> {target} [{out['status']}] {out['architecture_style']}: {names}"


def cmd_run(args) -> int:
    pairs = [p for path in args.paths for p in _items(path)]
    if not pairs:
        print("no scenarios found", file=sys.stderr)
        return 1
    if args.output and len(pairs) > 1:
        print("-o needs exactly one scenario; use --out-dir for several", file=sys.stderr)
        return 1
    llm = args.llm
    for name, raw in pairs:
        out = engine.run(raw, llm=llm, trace=args.trace)
        label = out.get("scenario_id") or name
        if args.stdout:
            sys.stdout.write(_dump(out))
            continue
        target = args.output or os.path.join(args.out_dir, _safe(label) + "_output.json")
        folder = os.path.dirname(target)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(_dump(out))
        print(_line(label, target, out))
    return 0


def cmd_validate(args) -> int:
    for name, raw in _items(args.path):
        v = validate.validate(raw)
        print(_dump({"scenario": name, "valid": not v.errors,
                     "errors": [{"field": e.field, "message": e.message} for e in v.errors],
                     "warnings": [{"code": w.code, "message": w.message} for w in v.warnings]}), end="")
    return 0


def cmd_describe(args) -> int:
    sys.stdout.write(_dump(describe.suggest(args.text)))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="stacksense", description="Rule-based tech stack recommendation engine.")
    sub = ap.add_subparsers(dest="cmd")
    run = sub.add_parser("run", help="recommend a stack for scenario JSON files")
    run.add_argument("paths", nargs="+", help="file, directory or combined file")
    run.add_argument("-o", "--output", help="output file (one scenario)")
    run.add_argument("--out-dir", default="out", help="output directory (default: out)")
    run.add_argument("--stdout", action="store_true", help="print JSON instead of writing files")
    run.add_argument("--llm", choices=("on", "off"), default=os.environ.get("STACKSENSE_LLM", "off"),
                     help="accepted for compatibility; no language model is used")
    run.add_argument("--trace", action="store_true", help="add scoring details to the output")
    run.set_defaults(fn=cmd_run)
    val = sub.add_parser("validate", help="check a scenario file")
    val.add_argument("path")
    val.set_defaults(fn=cmd_validate)
    des = sub.add_parser("describe", help="suggest fields from a project description")
    des.add_argument("text")
    des.set_defaults(fn=cmd_describe)
    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        ap.print_usage(sys.stderr)
        return 1
    return args.fn(args)
