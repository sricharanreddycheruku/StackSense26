import random
import unittest

from stacksense import engine
from stacksense.kb_loader import load_kb

from tests.helpers import EDGE_DIR, PUBLIC_DIR, load_json, random_scenario

import glob
import os

CORE = ("frontend", "backend", "database", "hosting")
CLOUD_WORDS = {"aws": ("Azure", "Google", "Firebase"), "azure": ("AWS", "Amazon", "Google", "Firebase"),
               "gcp": ("AWS", "Amazon", "Azure")}


def scenarios():
    for path in sorted(glob.glob(os.path.join(PUBLIC_DIR, "*.json")) + glob.glob(os.path.join(EDGE_DIR, "*.json"))):
        try:
            yield os.path.basename(path), load_json(path)
        except ValueError:
            continue
    rng = random.Random(26)
    for i in range(300):
        yield f"random{i}", random_scenario(rng)


def components(out):
    st = out["stack"]
    for k in CORE:
        yield k, st[k]
    for s in st["supporting"]:
        yield s["layer"], s


class HardConstraints(unittest.TestCase):
    def test_invariants_over_many_scenarios(self):
        kb = load_kb()
        for name, raw in scenarios():
            out = engine.run(raw)
            with self.subTest(name):
                self.assertNotIn("internal error", str(out["validation_errors"]))
                if out["status"] in ("invalid_input", "infeasible"):
                    self.assertIsNone(out["stack"])
                    continue
                spec = raw
                infra = spec.get("infrastructure") or {}
                c = out["estimated_monthly_cost_usd"]
                self.assertTrue(0 <= c["min"] <= c["max"])
                budget = (spec.get("budget") or {}).get("monthly_infra_usd")
                if out["status"] == "ok" and isinstance(budget, (int, float)):
                    self.assertLessEqual(c["min"], budget)
                self.assertEqual(out["status"] == "ok",
                                 not any(w["severity"] in ("warning", "critical") for w in out["warnings"]))
                for layer, comp in components(out):
                    self.assertIsNone(kb.deprecated_hit(comp["choice"]), comp["choice"])
                    self.assertTrue(comp["justification"])
                if out["architecture_style"] == "static":
                    self.assertEqual(out["stack"]["backend"]["choice"], "None")
                    self.assertEqual(out["stack"]["database"]["choice"], "None")
                pref = infra.get("cloud_preference")
                if pref in CLOUD_WORDS and infra.get("deployment_model", "managed_cloud") == "managed_cloud" \
                        and out["stack"]["hosting"]["choice"] != "None":
                    host = out["stack"]["hosting"]["choice"]
                    for word in CLOUD_WORDS[pref]:
                        self.assertNotIn(word, host)
                if infra.get("deployment_model") == "air_gapped" and out["stack"]["hosting"]["choice"] != "None":
                    self.assertRegex(out["stack"]["hosting"]["choice"], r"air-gapped|Air-gapped|own servers|RKE2")


if __name__ == "__main__":
    unittest.main()
