"""Raw scenario -> defaulted spec plus errors, assumptions and warnings. Never raises."""
from __future__ import annotations

import json
import math
from typing import Any

from .model import FieldError, Validated, Warn
from .schema import (
    FIELDS, KNOWN_LANGUAGES, KNOWN_LICENCES, PRIORITY_KEYS, SECTIONS, TOP_LEVEL_KEYS,
    FieldSpec, children,
)

_MISSING = object()
_TEXT_LIMIT = 5000
_MAX_SYSTEMS = 50
_MAX_SYSTEM_LEN = 200


def _type_name(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def _show(v: Any) -> str:
    if isinstance(v, str):
        return "string " + repr(v if len(v) <= 40 else v[:37] + "...")
    if isinstance(v, bool) or v is None:
        return _type_name(v)
    if isinstance(v, (int, float)):
        return "number " + repr(v)
    return _type_name(v)


def _fmt(v: Any) -> str:
    if isinstance(v, str):
        return v or '""'
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    return json.dumps(v)


class _Ctx:
    def __init__(self):
        self.errors: list[FieldError] = []
        self.assumptions: list[str] = []
        self.warnings: list[Warn] = []
        self.provided: set[str] = set()

    def err(self, path, msg):
        self.errors.append(FieldError(path, msg))

    def assume(self, msg):
        self.assumptions.append(msg)

    def info(self, code, msg, fields):
        self.warnings.append(Warn(code, "info", msg, tuple(fields)))


def _range_error(f: FieldSpec, v) -> str | None:
    if f.min is not None and v < f.min:
        return f"must be >= {_fmt(f.min)}, got {_fmt(v)}"
    if f.max is not None and v > f.max:
        return f"must be <= {_fmt(f.max)}, got {_fmt(v)}"
    return None


def _enum_message(f: FieldSpec, v: Any, what: str = "") -> str:
    allowed = ", ".join(str(e) for e in f.enum)
    if isinstance(v, str):
        low = v.strip().lower()
        hint = f"; did you mean '{low}'?" if low in f.enum else ""
        return f"{what}'{v}' is not allowed; allowed values: {allowed}{hint}"
    return f"{what}must be one of: {allowed}; got {_show(v)}"


def _parse_scalar(ctx: _Ctx, f: FieldSpec, v: Any) -> Any:
    t = f.type
    if t == "string":
        if not isinstance(v, str):
            ctx.err(f.path, f"string expected, got {_show(v)}")
            return _MISSING
        if len(v) > _TEXT_LIMIT and f.path in ("project.name", "project.description"):
            ctx.assume(f"{f.path} is {len(v)} characters long; truncated to {_TEXT_LIMIT} for parsing")
            v = v[:_TEXT_LIMIT]
        return v
    if t == "integer":
        if isinstance(v, bool) or not isinstance(v, int):
            ctx.err(f.path, f"integer expected, got {_show(v)}")
            return _MISSING
    elif t == "number":
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            ctx.err(f.path, f"number expected, got {_show(v)}")
            return _MISSING
        if isinstance(v, float) and not math.isfinite(v):
            ctx.err(f.path, "finite number expected, got NaN or Infinity")
            return _MISSING
    elif t == "boolean":
        if not isinstance(v, bool):
            ctx.err(f.path, f"boolean expected (true or false), got {_show(v)}")
            return _MISSING
        return v
    elif t == "enum":
        if f.path == "requirements.availability" and isinstance(v, (int, float)) and not isinstance(v, bool):
            hint = ""
            if isinstance(v, float) and math.isfinite(v) and ("%g" % v) in f.enum:
                hint = f'; use the string "{"%g" % v}"'
            elif isinstance(v, int) and str(v) in f.enum:
                hint = f'; use the string "{v}"'
            ctx.err(f.path, "must be one of the strings " + ", ".join(f'"{e}"' for e in f.enum)
                    + f", got {_show(v)}" + hint)
            return _MISSING
        if not isinstance(v, str) or v not in f.enum:
            ctx.err(f.path, _enum_message(f, v))
            return _MISSING
        return v
    problem = _range_error(f, v)
    if problem:
        ctx.err(f.path, problem)
        return _MISSING
    return v


def _dedupe(ctx: _Ctx, path: str, items: list) -> list:
    seen, out, dropped = set(), [], []
    for it in items:
        if it in seen:
            dropped.append(it)
        else:
            seen.add(it)
            out.append(it)
    if dropped:
        ctx.info("DUPLICATE_VALUE",
                 f"{path} lists {', '.join(repr(d) for d in dropped)} more than once; duplicates were removed.",
                 (path,))
    return out


def _parse_list(ctx: _Ctx, f: FieldSpec, v: Any) -> Any:
    if not isinstance(v, list):
        ctx.err(f.path, f"list expected, got {_show(v)}")
        return _MISSING
    out, ok = [], True
    for i, item in enumerate(v):
        if not isinstance(item, str):
            ctx.err(f.path, f"item {i + 1} must be a string, got {_show(item)}")
            ok = False
            continue
        if f.type == "list_enum":
            if item not in f.enum:
                ctx.err(f.path, _enum_message(f, item, f"item {i + 1} "))
                ok = False
                continue
            out.append(item)
            continue
        norm = item.strip().lower() if f.path in ("team.languages", "infrastructure.existing_licences") else item
        if not norm.strip():
            ctx.err(f.path, f"item {i + 1} is an empty string")
            ok = False
            continue
        if f.path == "infrastructure.existing_systems" and len(norm) > _MAX_SYSTEM_LEN:
            ctx.err(f.path, f"item {i + 1} is {len(norm)} characters long; the limit is {_MAX_SYSTEM_LEN}")
            ok = False
            continue
        out.append(norm)
    if f.path == "infrastructure.existing_systems" and len(v) > _MAX_SYSTEMS:
        ctx.err(f.path, f"at most {_MAX_SYSTEMS} items are allowed, got {len(v)}")
        ok = False
    if f.path == "project.platforms" and not v:
        ctx.err(f.path, "at least one platform is required")
        ok = False
    if not ok:
        return _MISSING
    if f.path in ("team.languages", "infrastructure.existing_licences"):
        changed = [a for a, b in zip(v, out) if a != b]
        if changed:
            ctx.assume(f"{f.path}: normalised {', '.join(repr(c) for c in changed)} to lower case without spaces")
        known = KNOWN_LANGUAGES if f.path == "team.languages" else KNOWN_LICENCES
        unknown = sorted({o for o in out if o not in known})
        if unknown:
            ctx.assume(f"{f.path}: {', '.join(repr(u) for u in unknown)} not in the recognised list; "
                       "kept as free text and used only for matching where possible")
    return _dedupe(ctx, f.path, out)


def _derived_default(path: str, spec: dict) -> Any:
    sc = spec.get("scale", {})
    launch, y1 = sc.get("users_at_launch"), sc.get("users_12_months")
    if path == "scale.peak_concurrent_users" and launch is not None and y1 is not None:
        return max(10, int(max(launch, y1) * 0.05))
    if path == "scale.data_volume_gb_12_months" and y1 is not None:
        return max(1.0, round(y1 * 0.002, 3))
    return _MISSING


def _apply_default(ctx: _Ctx, f: FieldSpec, spec: dict, target: dict, key: str) -> None:
    if f.extra:
        return
    if f.path in ("scale.peak_concurrent_users", "scale.data_volume_gb_12_months"):
        value = _derived_default(f.path, spec)
        if value is _MISSING:
            return
    else:
        value = [] if isinstance(f.default, list) else f.default
    target[key] = value
    ctx.assume(f"{f.path} not provided; defaulted to {_fmt(value)} ({f.default_note})")


def _unknown_keys(ctx: _Ctx, raw: dict, section: str, allowed: set) -> None:
    for key in sorted(raw, key=str):
        if key not in allowed:
            path = f"{section}.{key}" if section else str(key)
            ctx.info("UNKNOWN_FIELD", f"Unknown field '{path}' was ignored.", (path,))


def validate(raw: Any) -> Validated:
    ctx = _Ctx()
    spec: dict = {}
    try:
        _run(ctx, raw, spec)
    except Exception as exc:  # last resort: report instead of raising
        ctx.err("", f"internal validation error: {type(exc).__name__}")
    return Validated(spec, ctx.errors, ctx.assumptions, ctx.warnings, frozenset(ctx.provided))


def _run(ctx: _Ctx, raw: Any, spec: dict) -> None:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            ctx.err("", f"Input is not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})")
            return
    if not isinstance(raw, dict):
        ctx.err("", "Scenario must be a JSON object")
        return
    _unknown_keys(ctx, raw, "", set(TOP_LEVEL_KEYS))
    bad: set[str] = set()
    sections: dict[str, dict] = {}
    for name in SECTIONS:
        val = raw.get(name)
        if val is None:
            sections[name] = {}
        elif isinstance(val, dict):
            sections[name] = val
            allowed = {f.path.split(".")[1] for f in children(name)}
            _unknown_keys(ctx, val, name, allowed)
        else:
            ctx.err(name, f"object expected, got {_show(val)}")
            bad.add(name)
            sections[name] = {}
        spec[name] = {}
    for f in FIELDS:
        if f.type == "object":
            continue
        if "." in f.path:
            sec, key = f.path.split(".")
            if sec in bad:
                continue
            source, target = sections[sec], spec[sec]
        else:
            key, source, target = f.path, raw, spec
        val = source.get(key, _MISSING)
        if val is _MISSING or (val is None and not f.nullable):
            if f.required:
                ctx.err(f.path, "required field is missing")
            else:
                _apply_default(ctx, f, spec, target, key)
            continue
        if val is None:
            target[key] = None
            ctx.provided.add(f.path)
            continue
        if f.type in ("list_string", "list_enum"):
            parsed = _parse_list(ctx, f, val)
        else:
            parsed = _parse_scalar(ctx, f, val)
        if parsed is _MISSING:
            continue
        target[key] = parsed
        ctx.provided.add(f.path)
    if not isinstance(spec.get("scenario_id"), str):
        spec["scenario_id"] = "unnamed"
    _cross_checks(ctx, spec)


def _cross_checks(ctx: _Ctx, spec: dict) -> None:
    sc, pr, team = spec["scale"], spec["project"], spec["team"]
    launch, y1, peak = sc.get("users_at_launch"), sc.get("users_12_months"), sc.get("peak_concurrent_users")
    if launch is not None and y1 is not None:
        if "scale.peak_concurrent_users" in ctx.provided and peak > max(launch, y1):
            ctx.warnings.append(Warn(
                "PEAK_EXCEEDS_USERS", "warning",
                f"scale.peak_concurrent_users is {peak}, which is more than the largest user count "
                f"({max(launch, y1)}); concurrent users cannot exceed total users, so check both numbers.",
                ("scale.peak_concurrent_users", "scale.users_12_months")))
        if y1 < launch:
            ctx.info("USERS_DECLINE",
                     f"scale.users_12_months ({y1}) is below scale.users_at_launch ({launch}); "
                     "sizing uses the larger of the two.",
                     ("scale.users_at_launch", "scale.users_12_months"))
    platforms = pr.get("platforms")
    if platforms and "api_only" in platforms and len(platforms) > 1:
        ctx.warnings.append(Warn(
            "API_ONLY_MIXED", "warning",
            "project.platforms combines api_only with " + ", ".join(p for p in platforms if p != "api_only")
            + "; api_only means no client application, so clarify which clients are needed.",
            ("project.platforms",)))
    if team.get("languages") == []:
        ctx.info("NO_TEAM_LANGUAGES",
                 "team.languages is empty; skill fit is treated as neutral and every language is ranked "
                 "on technical merit.",
                 ("team.languages",))
