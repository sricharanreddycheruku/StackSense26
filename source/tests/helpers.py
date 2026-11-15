"""Shared helpers for the test suite."""
import copy
import glob
import json
import os

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLIC_DIR = os.path.join(SRC, "scenarios", "public")
EDGE_DIR = os.path.join(SRC, "scenarios", "edge")


def load_json(path):
    """Read a JSON file; a UTF-8 BOM (common on Windows) is accepted."""
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def public(name):
    return load_json(os.path.join(PUBLIC_DIR, name + ".json"))


def edge_path(prefix):
    matches = sorted(glob.glob(os.path.join(EDGE_DIR, prefix + "*.json")))
    if not matches:
        raise FileNotFoundError(prefix)
    return matches[0]


def edge(prefix):
    return load_json(edge_path(prefix))


def minimal():
    """Smallest scenario that has every required field."""
    return {
        "project": {"type": "web_app", "platforms": ["web"]},
        "scale": {"users_at_launch": 100, "users_12_months": 1000},
        "team": {"size": 2, "experience": "intermediate", "languages": ["python"]},
        "budget": {"monthly_infra_usd": 100},
        "timeline": {"mvp_weeks": 8},
    }


def with_value(base, path, value):
    """Copy of base with the dotted path set; value `...` removes the key."""
    out = copy.deepcopy(base)
    node = out
    parts = path.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    if value is Ellipsis:
        node.pop(parts[-1], None)
    else:
        node[parts[-1]] = value
    return out


def random_scenario(rng):
    """A random valid scenario built from the field table."""
    from stacksense.schema import FIELDS

    out = {}
    for f in FIELDS:
        if f.type == "object" or "." not in f.path:
            continue
        if not f.required and rng.random() < 0.45:
            continue
        if f.type == "enum":
            val = rng.choice(f.enum)
        elif f.type == "list_enum":
            val = rng.sample(list(f.enum), rng.randint(1, min(3, len(f.enum))))
        elif f.type == "boolean":
            val = rng.random() < 0.5
        elif f.type in ("integer", "number"):
            lo = f.min if f.min is not None else 0
            hi = f.max if f.max is not None else 10 ** 6
            val = int(10 ** rng.uniform(0, 5)) if hi - lo > 1000 else rng.randint(int(lo), int(hi))
            val = int(min(max(val, lo), hi))
        elif f.type == "list_string":
            val = rng.sample(["python", "javascript", "typescript", "java", "go", "php", "csharp"], rng.randint(1, 3)) \
                if "language" in f.path else ["SQL Server 2019", "Slack"][: rng.randint(0, 2)]
        else:
            val = "A project to test the engine"
        node = out
        parts = f.path.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = val
    return out
