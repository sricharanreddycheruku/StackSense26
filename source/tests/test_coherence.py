import unittest

from stacksense import engine
from stacksense.kb_loader import load_kb

from tests.helpers import public, with_value

KB = load_kb()
HOST_GIT = ("Vercel", "Netlify", "Cloudflare Pages", "AWS Amplify", "Render", "Railway", "DigitalOcean App Platform",
            "GitHub Pages", "Azure Static Web Apps")
CORE = ("frontend", "backend", "database", "hosting")


def run(name, *edits):
    raw = public(name)
    for path, value in edits:
        raw = with_value(raw, path, value)
    return engine.run(raw)


def supporting(out):
    return [(s.get("layer"), s.get("sub_layer"), s["choice"]) for s in out["stack"]["supporting"]]


def text(out, layers=None):
    return " | ".join(c for layer, _, c in supporting(out) if layers is None or layer in layers)


def vendors(out):
    """Provider names of every chosen offering, from the knowledge base by component name."""
    names = {}
    for layer in ("hosting", "database", "backend", "cache", "queue", "storage", "cdn", "observability", "ci_cd", "other"):
        for e in KB.entries(layer):
            for off in e.get("offerings") or []:
                names[off["name"]] = off["provider"]
    return names


class VendorCoherence(unittest.TestCase):
    def test_baas_backend_uses_builtins(self):
        for n in ("P2", "P6"):
            o = run(n)
            self.assertIn("Supabase", o["stack"]["backend"]["choice"], n)
            self.assertNotRegex(text(o), r"Pub/Sub|Amazon SNS", n)
            self.assertRegex(text(o, ("queue",)), r"Supabase", n)
            self.assertRegex(text(o, ("storage",)), r"Supabase", n)

    def test_cache_follows_the_host_cloud(self):
        self.assertRegex(text(run("P3"), ("cache",)), r"Memorystore")
        self.assertRegex(text(run("P4"), ("cache",)), r"ElastiCache")

    def test_self_hosted_cache_only_for_self_hosted_deployments(self):
        for n in ("P3", "P4"):
            self.assertNotRegex(text(run(n), ("cache",)), r"self-hosted", n)
        o = run("P3", ("infrastructure.deployment_model", "self_hosted_cloud_vm"))
        self.assertEqual(o["status"] in ("ok", "ok_with_warnings"), True)

    def test_few_distinct_vendors_for_small_teams(self):
        o = run("P2")
        prov = vendors(o)
        picked = set()
        for layer in ("queue", "storage", "cdn", "observability"):
            for _, _, c in [s for s in supporting(o) if s[0] == layer]:
                for part in c.split(" + "):
                    if part in prov:
                        picked.add(prov[part])
        self.assertLessEqual(len(picked), 2, picked)


class CiCd(unittest.TestCase):
    def test_host_git_integration_only_on_hosts_that_have_it(self):
        for n in ("P1", "P2", "P3", "P4", "P5", "P6"):
            o = run(n)
            host = o["stack"]["hosting"]["choice"]
            if " git integration" in text(o, ("ci_cd",)):
                self.assertTrue(host.startswith(HOST_GIT), (n, host))

    def test_firebase_and_cloud_run_get_a_real_pipeline(self):
        self.assertNotIn(" git integration", text(run("P1"), ("ci_cd",)))
        self.assertNotIn(" git integration", text(run("P3"), ("ci_cd",)))

    def test_static_site_on_a_git_host_uses_host_pipeline(self):
        o = run("P1", ("infrastructure.cloud_preference", "none"))
        if o["stack"]["hosting"]["choice"].startswith(HOST_GIT):
            self.assertIn(" git integration", text(o, ("ci_cd",)))


class Notifications(unittest.TestCase):
    def test_no_second_sms_provider_when_otp_covers_sms(self):
        o = run("P2")
        sms = [c for _, _, c in supporting(o) if "MSG91" in c or "SNS" in c]
        self.assertEqual(len(sms), 1, sms)

    def test_no_push_for_web_only(self):
        o = run("P3")
        self.assertNotRegex(text(o), r"OneSignal|Firebase Cloud Messaging|SNS")

    def test_push_for_mobile(self):
        self.assertRegex(text(run("P2")), r"Firebase Cloud Messaging")


class Cdn(unittest.TestCase):
    def test_no_public_cdn_for_internal_tool(self):
        o = run("P5")
        self.assertEqual([c for l, _, c in supporting(o) if l == "cdn"], [])


class Hosting(unittest.TestCase):
    def test_ssr_frontend_is_not_on_add_on_function_hosting(self):
        o = run("P2")
        self.assertNotIn("Firebase Hosting", o["stack"]["hosting"]["choice"])
        self.assertEqual(o["region"]["zone"], "india")

    def test_static_site_may_use_firebase_hosting(self):
        self.assertIn("Firebase", run("P1")["stack"]["hosting"]["choice"])


class CostBand(unittest.TestCase):
    def test_spread_is_bounded(self):
        for n in ("P2", "P3", "P4", "P5", "P6"):
            c = run(n)["estimated_monthly_cost_usd"]
            self.assertLessEqual(c["max"], 4 * c["min"] + 10, n)

    def test_zero_budget_free_tier_site_costs_nothing(self):
        o = run("P1")
        c = o["estimated_monthly_cost_usd"]
        self.assertEqual((c["min"], c["max"]), (0, 0))
        codes = {w["code"] for w in o["warnings"]}
        self.assertIn("FREE_TIER_LIMITS", codes)
        self.assertNotIn("BUDGET_TIGHT", codes)
        self.assertNotIn("BUDGET_EXCEEDED", codes)

    def test_p4_is_not_an_order_of_magnitude_over_its_budget(self):
        c = run("P4")["estimated_monthly_cost_usd"]
        self.assertLessEqual(c["max"], 10000)
        self.assertLessEqual(c["min"], 6000)


class ExistingSystems(unittest.TestCase):
    def test_no_rename_warning_when_new_name_is_in_the_input(self):
        o = run("P5")
        msgs = [w["message"] for w in o["warnings"] if w["code"] == "EOL_EXISTING_SYSTEM"]
        self.assertFalse([m for m in msgs if "Entra" in m], msgs)

    def test_reuse_warning_once_per_system(self):
        o = run("P5")
        reused = [w["message"].split(" is reused")[0] for w in o["warnings"] if w["code"] == "EXISTING_SYSTEM_REUSED"]
        self.assertEqual(len(reused), len(set(reused)), reused)

    def test_sql_server_2019_wording(self):
        o = run("P5")
        msg = next(w["message"] for w in o["warnings"] if w["code"] == "EOL_EXISTING_SYSTEM" and "SQL Server" in w["message"])
        self.assertIn("extended support until 2030-01-08", msg)
        self.assertIn("plan an upgrade", msg)
        self.assertNotIn("is eol", msg)

    def test_old_name_still_flagged(self):
        o = run("P5", ("infrastructure.existing_systems", ["Azure AD", "Microsoft 365"]))
        msgs = [w["message"] for w in o["warnings"] if w["code"] == "EOL_EXISTING_SYSTEM"]
        self.assertTrue([m for m in msgs if "Azure AD" in m and "Entra" in m], msgs)


if __name__ == "__main__":
    unittest.main()
