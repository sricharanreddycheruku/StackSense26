"""Load the knowledge base JSON once and give read-only lookups."""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache

KB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kb")
CORE_LAYERS = ("frontend", "backend", "database", "hosting")
RATING_KEYS = ("dev_speed", "learning_curve", "performance", "scalability", "ops_burden",
               "maturity", "ecosystem", "security", "resource_efficiency")


def _read(name: str):
    with open(os.path.join(KB_DIR, name + ".json"), encoding="utf-8-sig") as fh:
        return json.load(fh)


class KB:
    def __init__(self) -> None:
        self.providers_list = _read("providers")
        self.providers = {p["id"]: p for p in self.providers_list}
        self.layers: dict[str, list[dict]] = {}
        for name in CORE_LAYERS:
            self.layers[name] = _read(name)
        for entry in _read("supporting"):
            self.layers.setdefault(entry["layer"], []).append(entry)
        self.deprecated = _read("deprecated")
        self.existing = _read("existing_systems")
        self.compliance = {c["code"]: c for c in _read("compliance")}
        self.references = _read("references")
        self.sizing = _read("sizing")
        self._index = {(layer, e["id"]): e for layer, items in self.layers.items() for e in items}
        self._dep_re = [(re.compile(d["pattern"], re.I), d) for d in self.deprecated if d.get("pattern")]

    def entries(self, layer: str) -> list[dict]:
        return self.layers.get(layer, [])

    def entry(self, layer: str, entry_id: str) -> dict | None:
        return self._index.get((layer, entry_id))

    def provider(self, provider_id: str) -> dict | None:
        return self.providers.get(provider_id)

    def deprecated_hit(self, text: str) -> dict | None:
        for rx, rec in self._dep_re:
            if rx.search(text):
                return rec
        return None

    def check(self) -> list[str]:
        """Consistency problems of the loaded data; an empty list means the KB is usable."""
        problems: list[str] = []
        for layer, items in self.layers.items():
            seen = set()
            for e in items:
                tag = f"{layer}/{e.get('id')}"
                if e["id"] in seen:
                    problems.append(f"duplicate id {tag}")
                seen.add(e["id"])
                if e.get("status") != "active":
                    problems.append(f"{tag} is not active")
                for key in RATING_KEYS:
                    val = e.get("ratings", {}).get(key)
                    if not isinstance(val, (int, float)) or not 1 <= val <= 5:
                        problems.append(f"{tag} rating {key} missing or out of range")
                if not e.get("sources"):
                    problems.append(f"{tag} has no sources")
                if self.deprecated_hit(e["name"]):
                    problems.append(f"{tag} matches a deprecated pattern")
                for o in e.get("offerings", []):
                    if o.get("provider") not in self.providers:
                        problems.append(f"{tag}/{o.get('id')} has unknown provider {o.get('provider')}")
                    if not o.get("cost_anchors"):
                        problems.append(f"{tag}/{o.get('id')} has no cost anchors")
        for core in CORE_LAYERS:
            if not self.layers.get(core):
                problems.append(f"no entries for layer {core}")
        return problems


@lru_cache(maxsize=1)
def load_kb() -> KB:
    return KB()
