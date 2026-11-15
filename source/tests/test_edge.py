import json
import unittest

from stacksense import engine

from tests.helpers import edge, edge_path

CORE = ("frontend", "backend", "database", "hosting")


def run(prefix):
    return engine.run(edge(prefix))


def codes(out):
    return {w["code"] for w in out["warnings"]}


class EdgeCases(unittest.TestCase):
    def test_invalid_inputs(self):
        for prefix in ("E02", "E03", "E04_", "E04b"):
            try:
                with open(edge_path(prefix), encoding="utf-8-sig") as fh:
                    raw = json.load(fh)
            except ValueError:
                raw = "{ not json"
            out = engine.run(raw)
            with self.subTest(prefix):
                self.assertEqual(out["status"], "invalid_input")
                self.assertIsNone(out["stack"])
                self.assertTrue(out["validation_errors"])
                self.assertIsNone(out["estimated_monthly_cost_usd"]["min"])

    def test_minimal_input_gets_assumptions(self):
        out = run("E01")
        self.assertIn(out["status"], ("ok", "ok_with_warnings"))
        self.assertTrue(out["assumptions"])
        self.assertTrue(all(out["stack"][k]["choice"] for k in CORE))

    def test_zero_budget_huge_scale(self):
        out = run("E05")
        self.assertIn("BUDGET_EXCEEDED", codes(out))
        self.assertEqual(out["status"], "ok_with_warnings")
        self.assertTrue(any(a.startswith("RELAXED: budget") for a in out["assumptions"]))

    def test_enterprise_availability_for_solo_beginner(self):
        out = run("E06")
        self.assertIn("AVAILABILITY_UNREALISTIC", codes(out))
        self.assertTrue(any("99.95" in a for a in out["assumptions"]))

    def test_short_timeline_many_platforms(self):
        out = run("E07")
        self.assertIn("PLATFORM_SPREAD", codes(out))
        self.assertTrue(codes(out) & {"TIMELINE_UNREALISTIC", "TIMELINE_TIGHT"})

    def test_static_high_traffic(self):
        out = run("E08")
        self.assertEqual(out["architecture_style"], "static")
        self.assertEqual(out["stack"]["database"]["choice"], "None")

    def test_hipaa_in_us(self):
        out = run("E10")
        self.assertIn(out["status"], ("ok", "ok_with_warnings"))
        self.assertIn("COMPLIANCE_HIPAA", codes(out))

    def test_infeasible(self):
        for prefix in ("E11", "E16"):
            out = run(prefix)
            with self.subTest(prefix):
                self.assertEqual(out["status"], "infeasible")
                self.assertIsNone(out["stack"])
                self.assertTrue(out["suggested_relaxations"])
                self.assertTrue(any(w["severity"] == "critical" for w in out["warnings"]))

    def test_gdpr_region_is_eu(self):
        out = run("E12")
        self.assertEqual(out["region"]["zone"], "eu")

    def test_legacy_systems(self):
        out = run("E13")
        self.assertTrue({"EOL_EXISTING_SYSTEM", "EXISTING_SYSTEM_REUSED"} <= codes(out))

    def test_on_premise_and_air_gapped(self):
        out = run("E14")
        self.assertRegex(out["stack"]["hosting"]["choice"], r"Docker|Kubernetes|own servers|RKE2|k3s")
        out = run("E15")
        self.assertIn("REQUIREMENT_RELAXED", codes(out))
        self.assertTrue(out["assumptions"])

    def test_skill_mismatch(self):
        self.assertIn("SKILL_MISMATCH", codes(run("E17")))

    def test_deprecated_services(self):
        out = run("E18")
        self.assertIn("DEPRECATED_TECH_MENTIONED", codes(out))

    def test_api_only_has_no_frontend(self):
        self.assertEqual(run("E19")["stack"]["frontend"]["choice"], "None")

    def test_remaining_files_complete(self):
        for prefix in ("E09", "E20", "E21", "E22", "E23", "E24", "E25", "E26"):
            out = run(prefix)
            with self.subTest(prefix):
                self.assertIn(out["status"], ("ok", "ok_with_warnings"))
                for k in CORE:
                    self.assertTrue(out["stack"][k]["justification"])
        self.assertIn("REGION_UNVERIFIED", codes(run("E24")))


if __name__ == "__main__":
    unittest.main()
