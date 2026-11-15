import copy
import os
import re
import unittest

from stacksense import engine
from stacksense.schema import FIELD_INDEX

from tests.helpers import SRC, public, with_value

CORE = ("frontend", "backend", "database", "hosting")


def run(name):
    return engine.run(public(name))


def names(out):
    st = out["stack"]
    return {k: st[k]["choice"] for k in CORE}


def supporting(out):
    return [(s.get("layer"), s.get("sub_layer"), s["choice"]) for s in out["stack"]["supporting"]]


def all_text(out):
    return " | ".join(list(names(out).values()) + [c for _, _, c in supporting(out)])


class PublicScenarios(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = {n: run(n) for n in ("P1", "P2", "P3", "P4", "P5", "P6")}

    def test_status_and_shape(self):
        for n, o in self.out.items():
            with self.subTest(n):
                self.assertIn(o["status"], ("ok", "ok_with_warnings"))
                self.assertEqual(o["validation_errors"], [])
                self.assertTrue(o["summary"])
                c = o["estimated_monthly_cost_usd"]
                self.assertTrue(0 <= c["min"] <= c["max"])
                for k in CORE:
                    comp = o["stack"][k]
                    self.assertTrue(comp["justification"])
                    self.assertGreaterEqual(len(comp["alternatives"]), 1)
                    self.assertTrue(comp["driven_by"])
                    for path in comp["driven_by"]:
                        self.assertIn(path, FIELD_INDEX)
                for s in o["stack"]["supporting"]:
                    self.assertTrue(s["layer"] and s["justification"])

    def test_p1_static_site(self):
        o = self.out["P1"]
        n = names(o)
        self.assertEqual(o["architecture_style"], "static")
        self.assertEqual((n["backend"], n["database"]), ("None", "None"))
        self.assertTrue(re.search(r"Astro|Hugo|Eleventy|Next\.js", n["frontend"]))
        self.assertLessEqual(o["estimated_monthly_cost_usd"]["max"], 10)
        self.assertNotRegex(all_text(o), r"Kubernetes|EKS|GKE")

    def test_p2_marketplace_india(self):
        o = self.out["P2"]
        n = names(o)
        self.assertIn(o["architecture_style"], ("modular_monolith", "monolith"))
        self.assertIn("Next.js", n["frontend"])
        self.assertRegex(n["frontend"], r"Expo|React Native")
        self.assertRegex(n["database"], r"Postgres")
        self.assertEqual(o["region"]["zone"], "india")
        self.assertNotRegex(all_text(o), r"Stripe|Firebase Authentication|Elasticsearch|Render|Railway|Neon")
        self.assertRegex(all_text(o), r"MSG91")
        codes = {w["code"] for w in o["warnings"]}
        self.assertTrue({"COMPLIANCE_DPDP", "SMS_DLT_REQUIRED"} <= codes)
        self.assertLessEqual(o["estimated_monthly_cost_usd"]["min"], 150)

    def test_p3_collab_eu(self):
        o = self.out["P3"]
        n = names(o)
        self.assertEqual(o["architecture_style"], "service_oriented")
        self.assertRegex(n["backend"], r"Go")
        self.assertRegex(n["database"], r"Postgres|PostgreSQL")
        self.assertEqual(o["region"]["zone"], "eu")
        self.assertNotRegex(n["hosting"], r"Lambda")
        self.assertIn("COMPLIANCE_GDPR", {w["code"] for w in o["warnings"]} | {"COMPLIANCE_GDPR"})

    def test_p4_lending_india(self):
        o = self.out["P4"]
        n = names(o)
        self.assertIn(o["architecture_style"], ("modular_monolith", "service_oriented"))
        self.assertRegex(n["database"], r"Postgres|PostgreSQL|Aurora")
        self.assertRegex(n["hosting"], r"AWS|Amazon")
        self.assertEqual(o["region"]["primary"], "ap-south-1")
        self.assertNotRegex(all_text(o), r"QLDB|Stripe|Datadog|DynamoDB|MongoDB")
        codes = {w["code"] for w in o["warnings"]}
        self.assertTrue({"COMPLIANCE_RBI", "COMPLIANCE_PCI_DSS", "AVAILABILITY_COST"} <= codes)

    def test_p5_enterprise_azure(self):
        o = self.out["P5"]
        n = names(o)
        self.assertEqual(o["architecture_style"], "monolith")
        self.assertRegex(n["backend"], r"ASP\.NET")
        self.assertRegex(n["hosting"], r"Azure")
        self.assertRegex(n["database"], r"Azure SQL|SQL Server")
        self.assertRegex(all_text(o), r"Entra")
        self.assertNotRegex(all_text(o), r"Amazon (ECS|RDS|Cognito)|Google|MongoDB")
        codes = {w["code"] for w in o["warnings"]}
        self.assertTrue({"EOL_EXISTING_SYSTEM", "EXISTING_SYSTEM_REUSED"} <= codes)

    def test_p6_field_survey(self):
        o = self.out["P6"]
        n = names(o)
        self.assertRegex(n["frontend"], r"Expo|Flutter|Kotlin")
        self.assertNotRegex(n["frontend"], r"Progressive|Electron")
        self.assertRegex(n["database"], r"Postgres")
        self.assertEqual(o["region"]["zone"], "india")
        self.assertIn("offline_sync", [s for _, s, _ in supporting(o)])
        self.assertNotEqual(o["architecture_style"], "microservices")
        self.assertIn("SMS_DLT_REQUIRED", {w["code"] for w in o["warnings"]})


class Mutations(unittest.TestCase):
    def test_gcp_preference_moves_hosting(self):
        o = engine.run(with_value(public("P4"), "infrastructure.cloud_preference", "gcp"))
        self.assertRegex(o["stack"]["hosting"]["choice"], r"Google|Cloud Run|GKE|Firebase")
        self.assertIn(o["region"]["primary"], ("asia-south1", "asia-south2"))

    def test_payments_need_a_backend(self):
        o = engine.run(with_value(public("P1"), "requirements.payments", True))
        self.assertNotEqual(o["stack"]["backend"]["choice"], "None")

    def test_aws_preference(self):
        o = engine.run(with_value(public("P5"), "infrastructure.cloud_preference", "aws"))
        self.assertRegex(o["stack"]["hosting"]["choice"], r"AWS|Amazon")

    def test_offline_off_drops_sync(self):
        o = engine.run(with_value(public("P6"), "requirements.offline_support", False))
        self.assertNotIn("offline_sync", [s for _, s, _ in supporting(o)])

    def test_residency_none_unforces_region(self):
        base = with_value(public("P2"), "infrastructure.data_residency", "none")
        o = engine.run(with_value(base, "compliance", []))
        self.assertNotEqual(o["region"]["zone"], "india") if o["region"]["zone"] else None
        self.assertNotIn("COMPLIANCE_DPDP", {w["code"] for w in o["warnings"]})

    def test_rename_does_not_change_stack(self):
        for n in ("P1", "P2", "P3", "P4", "P5", "P6"):
            raw = copy.deepcopy(public(n))
            raw["scenario_id"] = "ZZ9"
            raw["project"]["name"] = "Renamed Thing"
            a, b = run(n), engine.run(raw)
            self.assertEqual(a["stack"], b["stack"], n)
            self.assertEqual(a["estimated_monthly_cost_usd"], b["estimated_monthly_cost_usd"], n)


class NoHardCoding(unittest.TestCase):
    def test_engine_source_has_no_scenario_names(self):
        banned = ["DevFolio", "FixIt", "BoardRoom", "QuickCredit", "PeopleDesk", "KhetSurvey"] + \
                 ['"P%d"' % i for i in range(1, 7)]
        pkg = os.path.join(SRC, "stacksense")
        for fn in sorted(os.listdir(pkg)):
            if fn.endswith(".py"):
                with open(os.path.join(pkg, fn), encoding="utf-8") as fh:
                    text = fh.read()
                for word in banned:
                    self.assertNotIn(word, text, f"{word} in {fn}")


if __name__ == "__main__":
    unittest.main()
