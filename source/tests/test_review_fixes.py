"""Cloud preference on supporting layers, budget monotonicity, realtime residency, language and capability fit,
region naming, estimate rounding and alternatives."""
import re
import unittest

from stacksense import engine
from stacksense.kb_loader import load_kb

from tests.helpers import edge, public, with_value

KB = load_kb()


def run(base, *changes):
    spec = base
    for path, value in changes:
        spec = with_value(spec, path, value)
    return engine.run(spec)


def codes(out, severity=None):
    return {w["code"] for w in out["warnings"] if severity in (None, w["severity"])}


def supporting(out):
    return {c["layer"]: c["choice"] for c in out["stack"]["supporting"]}


def solo_python_whiteboard():
    spec = public("P3")
    for path, value in (("team.size", 1), ("team.experience", "beginner"), ("team.languages", ["python"]),
                        ("team.has_devops", False), ("timeline.mvp_weeks", 12)):
        spec = with_value(spec, path, value)
    return spec


class CloudPreferenceOnSupportingLayers(unittest.TestCase):
    def test_gcp_preference_keeps_supporting_services_on_gcp_or_says_so(self):
        out = run(public("P4"), ("infrastructure.cloud_preference", "gcp"))
        other_cloud = [c for c in out["stack"]["supporting"]
                       if re.search(r"Amazon|AWS|Azure|Microsoft", c["choice"])]
        if other_cloud:
            self.assertIn("CLOUD_PREFERENCE_RELAXED", codes(out, "critical"))
            self.assertTrue(any(a.startswith("RELAXED: infrastructure.cloud_preference") for a in out["assumptions"]))
        else:
            self.assertNotIn("CLOUD_PREFERENCE_RELAXED", codes(out))

    def test_relaxed_layers_are_named_in_the_warning(self):
        out = run(public("P4"), ("infrastructure.cloud_preference", "gcp"))
        for w in out["warnings"]:
            if w["code"] == "CLOUD_PREFERENCE_RELAXED":
                for c in out["stack"]["supporting"]:
                    for part in c["choice"].split(" + "):
                        if re.search(r"Amazon|AWS", part):
                            self.assertIn(part, w["message"])

    def test_aws_preference_core_stays_on_aws(self):
        out = run(public("P5"), ("infrastructure.cloud_preference", "aws"))
        self.assertRegex(out["stack"]["hosting"]["choice"], r"AWS|Amazon")

    def test_no_preference_warning_when_every_layer_fits(self):
        self.assertNotIn("CLOUD_PREFERENCE_RELAXED", codes(engine.run(public("P3"))))


class BudgetMonotonic(unittest.TestCase):
    def test_lower_budget_never_raises_the_cost_minimum(self):
        mins = [run(public("P2"), ("budget.monthly_infra_usd", b))["estimated_monthly_cost_usd"]["min"]
                for b in (150, 100, 60, 40, 10, 1, 0)]
        self.assertEqual(mins, sorted(mins, reverse=True), mins)

    def test_p2_at_budget_40_is_far_below_the_unconstrained_cost(self):
        low = run(public("P2"), ("budget.monthly_infra_usd", 40))["estimated_monthly_cost_usd"]["min"]
        high = run(public("P2"), ("budget.monthly_infra_usd", 150))["estimated_monthly_cost_usd"]["min"]
        self.assertLessEqual(low, high)
        self.assertLess(low, 200)

    def test_exceeded_text_names_a_found_floor_not_a_proven_cheapest(self):
        out = run(public("P2"), ("budget.monthly_infra_usd", 40))
        text = " ".join(w["message"] for w in out["warnings"]) + " ".join(out["assumptions"])
        self.assertNotIn("cheapest stack", text)
        floor = out["estimated_monthly_cost_usd"]["min"]
        self.assertIn(f"${floor:,.0f}", text)

    def test_over_budget_still_ranks_by_price_on_the_big_spec(self):
        mins = [run(edge("E05"), ("budget.monthly_infra_usd", b))["estimated_monthly_cost_usd"]["min"]
                for b in (250, 100, 10, 1)]
        self.assertEqual(mins, sorted(mins, reverse=True), mins)


class RealtimeResidency(unittest.TestCase):
    def test_durable_objects_kb_is_consistent(self):
        entry = next(e for e in KB.entries("realtime") if e["id"] == "cf_durable_objects")
        self.assertFalse(any("No India or EU" in x for x in entry["limits"]))
        off = entry["offerings"][0]
        self.assertEqual(sorted(off["regions"]), ["eu"])
        self.assertTrue(any("data-location" in s["url"] for s in off["sources"]))

    def test_india_residency_does_not_use_a_service_without_an_india_region(self):
        out = run(public("P3"), ("infrastructure.data_residency", "india"), ("infrastructure.cloud_preference", "azure"))
        self.assertNotIn("Durable Objects", supporting(out)["realtime"])

    def test_eu_and_azure_realtime_has_an_eu_region(self):
        out = run(public("P3"), ("infrastructure.data_residency", "eu"), ("infrastructure.cloud_preference", "azure"))
        rt = next(c for c in out["stack"]["supporting"] if c["layer"] == "realtime")
        self.assertIn(rt["region"], ("eu", "westeurope", "northeurope"))


class LanguageAndCapabilityFit(unittest.TestCase):
    def test_language_reason_cites_only_team_languages(self):
        spec = public("P5")
        team = set(spec["team"]["languages"])
        for out in (engine.run(spec), run(spec, ("infrastructure.cloud_preference", "aws"))):
            text = out["stack"]["backend"]["justification"]
            for m in re.finditer(r"(\w+) like the frontend", text):
                self.assertIn(m.group(1), team, text)

    def test_csharp_team_gets_a_dotnet_backend_on_aws(self):
        out = run(public("P5"), ("infrastructure.cloud_preference", "aws"))
        self.assertEqual(out["stack"]["backend"]["choice"], "ASP.NET Core")

    def test_on_prem_database_behind_a_cloud_host_gets_a_connectivity_warning(self):
        out = run(public("P5"), ("infrastructure.cloud_preference", "aws"))
        if out["stack"]["database"]["choice"].endswith("self-managed") and "existing" in out["stack"]["database"]["choice"]:
            self.assertIn("HYBRID_CONNECTIVITY", codes(out))
            msg = next(w["message"] for w in out["warnings"] if w["code"] == "HYBRID_CONNECTIVITY")
            self.assertRegex(msg, r"VPN|Direct Connect")

    def test_solo_python_whiteboard_does_not_get_a_server_rendered_frontend(self):
        out = engine.run(solo_python_whiteboard())
        self.assertNotRegex(out["stack"]["frontend"]["choice"], r"HTMX|Livewire")
        self.assertIn("SKILL_MISMATCH", codes(out))

    def test_collab_frontend_is_a_client_capable_framework_for_other_teams_too(self):
        for lang in (["ruby"], ["php"], ["python", "go"]):
            out = run(solo_python_whiteboard(), ("team.languages", lang))
            self.assertNotRegex(out["stack"]["frontend"]["choice"], r"HTMX|Livewire", lang)


class PwaIsACapability(unittest.TestCase):
    def test_no_scenario_recommends_pwa_as_a_framework(self):
        for out in (engine.run(edge("E09")), run(edge("E09"), ("requirements.offline_support", True)),
                    run(public("P1"), ("requirements.offline_support", True))):
            self.assertNotIn("Progressive Web App", out["stack"]["frontend"]["choice"])
        self.assertFalse(any(e["id"] == "pwa" for e in KB.entries("frontend")))

    def test_offline_web_app_notes_the_pwa_capability_on_a_real_framework(self):
        out = run(edge("E09"), ("requirements.offline_support", True))
        text = out["stack"]["frontend"]["justification"].lower()
        self.assertIn("progressive web app", text)
        self.assertIn("service worker", text)

    def test_tiny_internal_tool_is_not_called_unrealistic(self):
        out = engine.run(edge("E09"))
        self.assertNotIn("TIMELINE_UNREALISTIC", codes(out))

    def test_large_internal_tool_estimate_is_unchanged_by_the_tiny_rule(self):
        small = engine.run(edge("E09"))
        big = run(edge("E09"), ("scale.users_12_months", 5000), ("scale.users_at_launch", 500))
        weeks = lambda out: re.findall(r"about ([\d.]+) weeks", " ".join(w["message"] for w in out["warnings"]))
        self.assertTrue(weeks(small) == [] or float(weeks(small)[0]) <= 3)
        self.assertTrue(weeks(big) == [] or float(weeks(big)[0]) >= 3)


class StaticSiteAtScale(unittest.TestCase):
    def test_free_unlimited_bandwidth_host_wins_for_a_viral_static_site(self):
        out = run(public("P1"), ("scale.users_12_months", 5_000_000))
        self.assertIn("Cloudflare", out["stack"]["hosting"]["choice"])

    def test_ci_text_matches_the_chosen_host(self):
        for users in (1000, 5_000_000):
            out = run(public("P1"), ("scale.users_12_months", users))
            ci = next((c for c in out["stack"]["supporting"] if c["layer"] == "ci_cd"), None)
            if ci and "git integration" in ci["choice"]:
                host = out["stack"]["hosting"]["choice"].split(" + ")[0]
                self.assertTrue(ci["choice"].startswith(host.split(" and ")[0]), (ci["choice"], host))
                self.assertNotIn("Vercel, Netlify", ci["choice"])


class TextPolish(unittest.TestCase):
    def test_zero_cost_band_reads_as_zero(self):
        out = engine.run(public("P1"))
        self.assertNotRegex(out["summary"], r"\$0-0")

    def test_peak_text_quotes_peak_concurrent_users(self):
        spec = public("P3")
        out = engine.run(spec)
        peak = f"{spec['scale']['peak_concurrent_users']:,}"
        found = []
        for key in ("frontend", "backend", "database", "hosting"):
            found += re.findall(r"about ([\d,]+) concurrent at peak", out["stack"][key]["justification"])
        self.assertTrue(found)
        self.assertTrue(all(f == peak for f in found), (found, peak))
        self.assertNotIn("peak about", " ".join(c["justification"] for c in out["stack"]["supporting"]))

    def test_regions_use_the_providers_own_names(self):
        out = engine.run(public("P2"))
        self.assertNotRegex(out["summary"], r"Google Cloud Run in ap-south-1")
        self.assertNotRegex(out["summary"], r"Firebase Hosting in us-east-1")
        gcp = run(public("P4"), ("infrastructure.cloud_preference", "gcp"))
        for c in gcp["stack"]["supporting"]:
            if re.search(r"Amazon", c["choice"]) and c["region"]:
                self.assertRegex(c["region"], r"^[a-z]{2}-[a-z]+-\d$", c)

    def test_effort_estimates_are_whole_or_half_weeks(self):
        for out in (engine.run(public(n)) for n in ("P1", "P2", "P3", "P4", "P5", "P6")):
            for w in out["warnings"]:
                for n in re.findall(r"about ([\d.]+) weeks", w["message"]):
                    self.assertEqual(float(n) * 2, int(float(n) * 2), w["message"])


class Alternatives(unittest.TestCase):
    def test_blog_has_no_kubernetes_hosting_alternative(self):
        out = engine.run(public("P1"))
        names = [a["choice"] for a in out["stack"]["hosting"]["alternatives"]]
        self.assertFalse(any("EKS" in n for n in names), names)

    def test_no_alternative_is_only_a_bare_project_type_match(self):
        for n in ("P1", "P2", "P3", "P4", "P5", "P6"):
            out = engine.run(public(n))
            for key in ("frontend", "backend", "database", "hosting"):
                for alt in (out["stack"].get(key) or {}).get("alternatives", []):
                    self.assertNotRegex(alt["why_not"], r"is preferred because it is suited to project\.type", (n, key, alt))

    def test_oracle_migration_text_does_not_call_self_hosted_postgres_managed(self):
        out = engine.run(edge("E14"))
        text = " ".join(w["message"] for w in out["warnings"])
        self.assertNotIn("supported managed database", text)
        self.assertNotIn("supported managed database", " ".join(
            e["migration"]["text"] for e in KB.existing if e.get("migration")))


if __name__ == "__main__":
    unittest.main()


class AlternativesNeverEmpty(unittest.TestCase):
    def test_aws_static_host_keeps_an_alternative(self):
        out = engine.run({
            "project": {"type": "content_site", "platforms": ["web", "android"]},
            "scale": {"users_at_launch": 13, "users_12_months": 839485, "peak_concurrent_users": 101},
            "team": {"size": 1, "experience": "senior", "languages": ["elixir"]},
            "budget": {"monthly_infra_usd": 10000},
            "timeline": {"mvp_weeks": 339},
            "infrastructure": {"cloud_preference": "aws"},
            "requirements": {"seo_critical": True, "consistency": "eventual"},
        })
        self.assertIsNotNone(out["stack"])
        for comp in [out["stack"][k] for k in ("frontend", "backend", "database", "hosting")]:
            if comp["choice"] != "None":
                self.assertTrue(comp["alternatives"], comp["choice"])


def scenario(ptype, desc, platforms, users, team, langs, devops, budget, weeks, cloud="any", deployment="managed_cloud",
             residency="none", existing=(), req=None, compliance=(), workload="balanced", gb=10):
    """A full scenario of a kind the public set does not cover."""
    r = {"realtime": False, "offline_support": False, "full_text_search": False, "file_media_storage": False,
         "payments": False, "auth": "email_password", "multi_tenant": False, "ml_features": "none",
         "seo_critical": False, "availability": "99.9", "p95_latency_ms": None}
    r.update(req or {})
    return {
        "project": {"name": ptype, "type": ptype, "description": desc, "platforms": list(platforms)},
        "scale": {"traffic_pattern": "steady", "workload": workload, "users_at_launch": max(1, users // 10),
                  "users_12_months": users, "peak_concurrent_users": max(1, users // 20), "data_volume_gb_12_months": gb},
        "team": {"has_devops": devops, "size": team, "experience": "intermediate", "languages": list(langs)},
        "budget": {"monthly_infra_usd": budget}, "timeline": {"mvp_weeks": weeks},
        "infrastructure": {"cloud_preference": cloud, "deployment_model": deployment, "data_residency": residency,
                           "existing_systems": list(existing)},
        "requirements": r, "compliance": list(compliance),
        "priorities": {k: 3 for k in ("cost", "time_to_market", "scalability", "performance", "maintainability", "security")},
    }


def telemedicine():
    return scenario("healthtech", "Telemedicine: patients book video consults with doctors and keep medical records.",
                    ["web", "android", "ios"], 40000, 5, ["python", "typescript"], True, 3000, 14, cloud="aws",
                    residency="us", req={"realtime": True, "file_media_storage": True, "payments": True},
                    compliance=["hipaa", "soc2"], gb=500)


def bi_dashboards(cloud="any", budget=300, gb=800):
    return scenario("data_analytics", "Internal BI dashboards over sales data from 40 stores; nightly ETL from POS exports.",
                    ["web"], 200, 2, ["python", "sql"], False, budget, 8, cloud=cloud, workload="read_heavy", gb=gb,
                    req={"auth": "enterprise_sso"})


def plant_lan():
    return scenario("internal_tool", "Factory maintenance ticketing on the plant LAN; no internet allowed.", ["web"], 150, 1,
                    ["java"], False, 200, 6, cloud="none", deployment="on_premise",
                    existing=["Active Directory", "Oracle Database 19c"], req={"auth": "enterprise_sso"})


def stack_text(out):
    return [c["choice"] for k in ("frontend", "backend", "database", "hosting") for c in [out["stack"][k]]] + \
        [c["choice"] for c in out["stack"]["supporting"]]


class HiddenStyleScenarios(unittest.TestCase):
    def test_hipaa_with_payments_is_feasible(self):
        out = engine.run(telemedicine())
        self.assertIn(out["status"], ("ok", "ok_with_warnings"))
        self.assertIn("payments", supporting(out))

    def test_email_does_not_leave_a_locked_cloud(self):
        gcp_pci = scenario("fintech", "Expense management for SMBs with corporate cards.", ["web"], 30000, 6,
                           ["java", "typescript"], True, 4000, 20, cloud="gcp", residency="eu",
                           req={"payments": True, "auth": "enterprise_sso"}, compliance=["gdpr", "pci_dss", "soc2"])
        azure_iso = with_value(public("P5"), "infrastructure.existing_systems", [])
        for spec in (gcp_pci, azure_iso):
            out = engine.run(spec)
            self.assertNotIn("CLOUD_PREFERENCE_RELAXED", codes(out))

    def test_on_prem_active_directory_is_reused(self):
        out = engine.run(plant_lan())
        msgs = [w["message"] for w in out["warnings"] if w["code"] == "EXISTING_SYSTEM_REUSED"]
        self.assertTrue(any(m.startswith("Active Directory is reused") for m in msgs), msgs)

    def test_on_prem_sends_email_through_a_self_hosted_relay(self):
        out = engine.run(plant_lan())
        for name in stack_text(out):
            self.assertNotRegex(name, r"Resend|SendGrid|Amazon SES|Postmark|Azure Communication")
        self.assertTrue(any("Postfix" in n for n in stack_text(out)))

    def test_mobile_word_game_with_leaderboards_uses_a_game_engine(self):
        game = scenario("gaming", "Casual turn-based multiplayer word game with leaderboards.", ["android", "ios"],
                        100000, 3, ["csharp"], False, 600, 12, req={"realtime": True, "auth": "social"})
        self.assertEqual(engine.run(game)["stack"]["frontend"]["choice"], "Unity")

    def test_news_site_with_editors_gets_a_cms_and_p1_does_not(self):
        news = scenario("content_site", "News site for a regional newspaper, editors publish via CMS.", ["web"],
                        2000000, 3, ["php", "javascript"], False, 800, 8, req={"seo_critical": True, "auth": "none"},
                        workload="read_heavy", gb=200)
        cms = ("Decap", "Sanity", "Strapi", "WordPress")
        self.assertTrue(any(any(c in n for c in cms) for n in stack_text(engine.run(news))))
        self.assertFalse(any(any(c in n for c in cms) for n in stack_text(engine.run(public("P1")))))


class ModifiedPublicScenarios(unittest.TestCase):
    def test_p2_on_aws_uses_a_managed_database_and_no_socket_server_on_amplify(self):
        out = run(public("P2"), ("infrastructure.cloud_preference", "aws"))
        self.assertNotIn("self-hosted", out["stack"]["database"]["choice"])
        if "Amplify" in out["stack"]["hosting"]["choice"] and "Fargate" not in out["stack"]["hosting"]["choice"]:
            self.assertNotIn("Socket.IO", supporting(out).get("realtime", ""))

    def test_p4_any_cloud_auth_is_not_cognito_on_an_azure_core(self):
        out = run(public("P4"), ("infrastructure.cloud_preference", "any"))
        if "Azure" in out["stack"]["hosting"]["choice"]:
            self.assertNotIn("Cognito", supporting(out)["auth"])

    def test_p6_on_gcp_has_no_critical_warning(self):
        out = run(public("P6"), ("infrastructure.cloud_preference", "gcp"))
        self.assertFalse(codes(out, "critical"))
        self.assertNotIn("Cognito", supporting(out)["auth"])

    def test_p5_variants_stay_feasible(self):
        for changes in ((("infrastructure.existing_systems", []),), (("infrastructure.cloud_preference", "aws"),),
                        (("infrastructure.cloud_preference", "gcp"),)):
            self.assertNotIn(run(public("P5"), *changes)["status"], ("infeasible", "invalid_input"), changes)


class AnalyticsWorkloads(unittest.TestCase):
    BI = ("Metabase", "Superset", "Power BI", "Quick Sight", "Data Studio")

    def test_bi_tool_sql_store_and_long_running_backend(self):
        for cloud in ("any", "aws", "azure", "gcp"):
            for budget, gb in ((300, 800), (50, 200), (30, 50)):
                out = engine.run(bi_dashboards(cloud, budget, gb))
                label = (cloud, budget, gb)
                self.assertTrue(any(any(b in n for b in self.BI) for n in stack_text(out)), label)
                self.assertNotRegex(out["stack"]["database"]["choice"], r"SQLite|DynamoDB|Keyspaces|Cassandra", label)
                self.assertNotRegex(out["stack"]["backend"]["choice"], r"Lambda|Functions|Workers", label)

    def test_aws_bi_stays_within_budget_on_a_managed_database(self):
        out = engine.run(bi_dashboards("aws", 300, 800))
        self.assertRegex(out["stack"]["database"]["choice"], r"^Amazon (RDS|Aurora)")
        self.assertTrue(any("Metabase" in n for n in stack_text(out)))
        self.assertLessEqual(out["estimated_monthly_cost_usd"]["max"], 300)

    def test_self_run_database_disks_are_billed_only_in_the_cloud(self):
        from stacksense.cost import gb_rate
        off = next(o for o in KB.entry("database", "postgresql")["offerings"] if o["id"] == "postgres_self")
        self.assertGreater(gb_rate(off, "managed_cloud"), 0)
        self.assertEqual(gb_rate(off, "on_premise"), 0)
        self.assertEqual(gb_rate(off, "air_gapped"), 0)


class OutputText(unittest.TestCase):
    def test_public_outputs_read_cleanly(self):
        for name in ("P1", "P2", "P3", "P4", "P5", "P6"):
            out = engine.run(public(name))
            for k in ("frontend", "backend", "database", "hosting"):
                comp = out["stack"][k]
                self.assertLessEqual(comp["justification"].count("-week MVP"), 1, (name, k))
                for alt in comp["alternatives"]:
                    self.assertNotIn("Excluded: .", alt["why_not"], (name, k))
            for w in out["warnings"]:
                if w["code"] == "FREE_TIER_LIMITS":
                    self.assertNotIn("Supabase", w["message"], name)
                    self.assertNotIn("Software is free", w["message"], name)


class CapacityAndDomainNotes(unittest.TestCase):
    def test_capacity_block_matches_derived_load(self):
        out = engine.run(public("P4"))
        cap = out["capacity"]
        self.assertEqual(cap["peak_users"], 300000)
        self.assertEqual(cap["peak_rps"], 3000.0)
        self.assertEqual(cap["api_instances_at_peak"], 12)
        self.assertEqual(cap["data_volume_gb_12_months"], 500)

    def test_capacity_has_no_instances_without_a_server(self):
        self.assertIsNone(engine.run(public("P1"))["capacity"]["api_instances_at_peak"])
        self.assertIsNone(engine.run(public("P6"))["capacity"]["api_instances_at_peak"])

    def test_high_availability_keeps_two_instances(self):
        out = run(public("P5"), ("requirements.availability", "99.99"))
        self.assertGreaterEqual(out["capacity"]["api_instances_at_peak"], 2)

    def test_given_rps_on_split_host_is_sized_in_cost(self):
        out = run(public("P4"), ("scale.peak_rps", 2500))
        rows = [r for r in out["estimated_monthly_cost_usd"]["breakdown"] if "sizing" in r["component"]]
        self.assertTrue(rows)
        self.assertIn("10 instances", rows[0]["component"])

    def test_scale_headroom_named_once_for_bundled_backend(self):
        out = run(public("P6"), ("scale.users_12_months", 3000000))
        lines = [t for k in ("backend", "database", "hosting") for t in out["stack"][k]["tradeoffs"]
                 if t.startswith("Scale headroom")]
        self.assertEqual(len(lines), 1)
        self.assertIn("3,000,000", lines[0])
        self.assertFalse(any(t.startswith("Scale headroom") for k in ("backend", "database", "hosting")
                             for t in engine.run(public("P6"))["stack"][k]["tradeoffs"]))

    def test_payment_and_stock_notes(self):
        out = engine.run(public("P2"))
        self.assertTrue({"PAYMENT_IDEMPOTENCY", "OVERSELL_RISK"} <= codes(out, "info"))
        fin = engine.run(public("P4"))
        msg = next(w["message"] for w in fin["warnings"] if w["code"] == "PAYMENT_IDEMPOTENCY")
        self.assertIn("double-entry ledger", msg)
        self.assertNotIn("PAYMENT_IDEMPOTENCY", codes(engine.run(public("P1"))))

    def test_moderation_note_names_only_present_stores(self):
        out = run(public("P2"), ("project.type", "social"))
        msg = next(w["message"] for w in out["warnings"] if w["code"] == "UGC_MODERATION")
        self.assertIn("Google Play", msg)
        self.assertNotIn("App Store", msg)
        self.assertNotIn("UGC_MODERATION", codes(run(public("P3"), ("project.type", "social"))))
