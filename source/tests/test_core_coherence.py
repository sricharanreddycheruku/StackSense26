import glob
import json
import os
import random
import re
import unittest

from stacksense import engine
from stacksense.kb_loader import load_kb

from tests.helpers import EDGE_DIR, PUBLIC_DIR, edge, load_json, minimal, public, random_scenario, with_value

KB = load_kb()
LAYERS = ("frontend", "backend", "database", "hosting", "auth", "cache", "queue", "storage", "cdn",
          "observability", "ci_cd", "search", "payments", "email", "other")
PROVIDER_OF = {}
for _layer in LAYERS:
    for _e in KB.entries(_layer):
        for _o in _e.get("offerings") or []:
            PROVIDER_OF[_o["name"]] = _o["provider"]
HYPER = {p["id"] for p in json.load(open(os.path.join(os.path.dirname(__file__), "..", "stacksense", "kb",
                                                       "providers.json"), encoding="utf-8")) if p["kind"] == "hyperscaler"}


def scenario_files():
    return sorted(glob.glob(os.path.join(PUBLIC_DIR, "*.json")) + glob.glob(os.path.join(EDGE_DIR, "*.json")))


def providers_in(choice):
    """Providers of every known offering named inside a choice string."""
    return {PROVIDER_OF[n] for n in PROVIDER_OF if n in choice}


def core_choices(out):
    st = out["stack"]
    names = [st[k]["choice"] for k in ("backend", "database", "hosting") if st.get(k)]
    names += [s["choice"] for s in st["supporting"] if s.get("layer") == "auth"]
    return names


def clouds(out):
    """Hyperscaler clouds of backend, database and hosting, plus auth unless it states the mismatch itself."""
    st = out["stack"]
    names = [st[k]["choice"] for k in ("backend", "database", "hosting") if st.get(k)]
    for s in st["supporting"]:
        if s.get("layer") == "auth" and not any("different cloud" in t for t in s.get("tradeoffs", [])):
            names.append(s["choice"])
    found = set()
    for name in names:
        found |= providers_in(name) & HYPER
    return found


def vendor_count(out):
    found = set()
    for name in core_choices(out):
        found |= {p for p in providers_in(name) if p != "self"}
    return found


def has_stack(out):
    return out["status"] in ("ok", "ok_with_warnings") and out["stack"].get("hosting")


def warn_text(out, code):
    return " ".join(w["message"] for w in out.get("warnings", []) if w["code"] == code)


class OneCloud(unittest.TestCase):
    def test_public_and_edge_scenarios_stay_on_one_cloud(self):
        seen = 0
        for path in scenario_files():
            try:
                raw = load_json(path)
            except ValueError:
                continue
            if not isinstance(raw, dict):
                continue
            out = engine.run(raw)
            if not has_stack(out):
                continue
            seen += 1
            self.assertLessEqual(len(clouds(out)), 1, (os.path.basename(path), core_choices(out)))
        self.assertGreater(seen, 25)

    def test_random_scenarios_stay_on_one_cloud(self):
        rng = random.Random(2026)
        seen = 0
        for _ in range(150):
            out = engine.run(random_scenario(rng))
            if not has_stack(out):
                continue
            seen += 1
            self.assertLessEqual(len(clouds(out)), 1, core_choices(out))
        self.assertGreater(seen, 40)

    def test_minimal_input_has_at_most_two_vendors(self):
        out = engine.run(minimal())
        self.assertLessEqual(len(vendor_count(out)), 2, core_choices(out))
        out = engine.run(edge("E01"))
        self.assertLessEqual(len(vendor_count(out)), 2, core_choices(out))


class HostRunsBackend(unittest.TestCase):
    def test_static_host_is_not_chosen_for_a_server_backend(self):
        raw = minimal()
        raw["project"].update({"type": "ecommerce", "platforms": ["ios", "desktop"]})
        raw["team"]["languages"] = ["dart", "css"]
        raw["scale"].update({"users_12_months": 231424, "peak_concurrent_users": 121464})
        raw["budget"] = {"monthly_infra_usd": 150.0}
        out = engine.run(raw)
        stack = out["stack"]
        host = stack["hosting"]["choice"]
        self.assertNotIn(host, ("Netlify", "Vercel", "Cloudflare Pages", "Firebase Hosting", "GitHub Pages"), core_choices(out))
        self.assertLessEqual(len(clouds(out)), 1, core_choices(out))


class ExistingSystems(unittest.TestCase):
    def test_existing_idp_becomes_the_auth_choice(self):
        out = engine.run(edge("E13"))
        auth = [s["choice"] for s in out["stack"]["supporting"] if s.get("layer") == "auth"]
        self.assertEqual(auth, ["Okta (existing tenant)"])
        self.assertIn("Okta", warn_text(out, "EXISTING_SYSTEM_REUSED"))
        for system, expected in (("Keycloak", "Keycloak"), ("Microsoft Entra ID", "Entra")):
            raw = with_value(minimal(), "infrastructure.existing_systems", [system])
            raw = with_value(raw, "requirements.auth", "enterprise_sso")
            out = engine.run(raw)
            auth = [s["choice"] for s in out["stack"]["supporting"] if s.get("layer") == "auth"]
            self.assertTrue(auth and expected in auth[0], (system, auth))

    def test_existing_supported_database_is_kept(self):
        for system, expected in (("PostgreSQL 15", "PostgreSQL"), ("MySQL 8", "MySQL"), ("SQL Server 2019", "SQL")):
            raw = with_value(minimal(), "infrastructure.existing_systems", [system])
            out = engine.run(raw)
            self.assertIn(expected, out["stack"]["database"]["choice"], system)

    def test_end_of_life_systems_get_a_migration_target_not_reuse(self):
        out = engine.run(edge("E13"))
        reused = warn_text(out, "EXISTING_SYSTEM_REUSED")
        for old in ("Oracle", "Java 8", "Spring Boot 2.7", "CentOS", "AngularJS"):
            self.assertNotIn(old, reused)
        eol = " ".join(w["message"] for w in out["warnings"] if "EOL" in w["code"] or "END_OF_LIFE" in w["code"]
                       or "DEPRECATED" in w["code"])
        self.assertIn("PostgreSQL", eol)
        self.assertIn("Spring Boot 3.x on Java 21", eol)
        self.assertIn(out["stack"]["database"]["choice"].split(" ")[0], ("Cloud", "Amazon", "Azure", "PostgreSQL"))


class Defaults(unittest.TestCase):
    def test_relational_database_is_the_default_for_crud_types(self):
        for kind in ("web_app", "saas_b2b", "ecommerce", "internal_tool"):
            raw = with_value(minimal(), "project.type", kind)
            db = engine.run(raw)["stack"]["database"]["choice"]
            for other in ("DynamoDB", "Firestore", "Cosmos", "MongoDB"):
                self.assertNotIn(other, db, kind)

    def test_relational_default_covers_unspecific_types(self):
        for kind in ("other", "social", "content_site"):
            for cost in (4, 5):
                raw = with_value(with_value(minimal(), "project.type", kind), "priorities.cost", cost)
                raw = with_value(raw, "project.platforms", ["android"])
                db = engine.run(raw)["stack"]["database"]["choice"]
                for other in ("DynamoDB", "Firestore", "Cosmos", "MongoDB"):
                    self.assertNotIn(other, db, (kind, cost))

    def test_key_value_signal_in_description_allows_dynamodb_and_is_named(self):
        raw = with_value(minimal(), "project.type", "other")
        raw = with_value(raw, "project.description", "A key-value session cache with event stream ingestion on AWS")
        raw = with_value(raw, "infrastructure.cloud_preference", "aws")
        raw = with_value(raw, "requirements.auth", "email_password")
        db = engine.run(raw)["stack"]["database"]
        self.assertIn("DynamoDB", db["choice"])
        self.assertIn("key-value", db["justification"])

    def test_self_hosted_pick_on_managed_cloud_states_why(self):
        out = engine.run(edge("E22"))
        auth = [s for s in out["stack"]["supporting"] if s.get("layer") == "auth"][0]
        self.assertIn("self-hosted", auth["justification"])
        self.assertIn("deployment is managed_cloud", auth["justification"])
        self.assertIn("india", auth["justification"])

    def test_small_team_without_devops_avoids_self_hosting(self):
        raw = with_value(minimal(), "team.has_devops", False)
        out = engine.run(raw)
        for part in core_choices(out):
            self.assertNotIn("self-hosted", part.lower())


class Justifications(unittest.TestCase):
    def test_no_generic_score_text(self):
        pat = re.compile(r"\bfit\b[^.]*\d(\.\d)?\s*/\s*5")
        names = ["P1", "P2", "P3", "P4", "P5", "P6"]
        outs = [engine.run(public(n)) for n in names] + [engine.run(edge(p)) for p in ("E13", "E21", "E22", "E25")]
        for out in outs:
            st = out["stack"]
            texts = [st[k]["justification"] for k in ("frontend", "backend", "database", "hosting") if st.get(k)]
            texts += [s["justification"] for s in st["supporting"] if s.get("justification")]
            for t in texts:
                self.assertIsNone(pat.search(t), t)


def all_outputs():
    for path in scenario_files():
        try:
            raw = load_json(path)
        except ValueError:
            continue
        if isinstance(raw, dict):
            out = engine.run(raw)
            if has_stack(out):
                yield os.path.basename(path), out


HOSTING_ENTRY = {}
for _e in KB.entries("hosting"):
    for _o in _e.get("offerings") or []:
        HOSTING_ENTRY[_o["name"]] = _e


class RuntimeAndCost(unittest.TestCase):
    def test_hosting_matches_a_functions_backend(self):
        faas = {o["name"] for e in KB.entries("backend") if e.get("sub_layer") == "faas" for o in e["offerings"]}
        checked = 0
        typescript_team = engine.run(with_value(edge("E21"), "team.languages", ["typescript"]))
        for name, out in list(all_outputs()) + [("E21 typescript team", typescript_team)]:
            if out["stack"]["backend"]["choice"] not in faas:
                continue
            checked += 1
            parts = [e for n, e in HOSTING_ENTRY.items() if n in out["stack"]["hosting"]["choice"]]
            self.assertTrue(parts, name)
            self.assertTrue(any("faas" in e["runs_on"] for e in parts), (name, out["stack"]["hosting"]["choice"]))
            for e in parts:
                self.assertNotIn(e.get("sub_layer"), ("container", "vm", "kubernetes"), (name, e["id"]))
        self.assertGreater(checked, 0)

    def test_layer_cost_in_justification_matches_breakdown(self):
        pat = re.compile(r"about \$([\d,]+)-([\d,]+) a month for this layer")
        seen = 0
        for name, out in all_outputs():
            for layer in ("frontend", "backend", "database", "hosting"):
                m = pat.search(out["stack"][layer]["justification"])
                if not m:
                    continue
                rows = [r for r in out["estimated_monthly_cost_usd"]["breakdown"] if r["layer"] == layer]
                lo, hi = sum(r["min"] for r in rows), sum(r["max"] for r in rows)
                self.assertEqual((m.group(1), m.group(2)), (f"{lo:,.0f}", f"{hi:,.0f}"), (name, layer))
                seen += 1
        self.assertGreater(seen, 10)

    def test_duplicate_database_rows_are_labelled(self):
        rows = [r["component"] for r in engine.run(edge("E21"))["estimated_monthly_cost_usd"]["breakdown"]
                if r["layer"] == "database"]
        self.assertEqual(len(rows), len(set(rows)), rows)
        self.assertTrue(any(r.endswith("(storage)") for r in rows), rows)


class TeamLanguages(unittest.TestCase):
    def test_empty_languages_keep_one_language(self):
        out = engine.run(edge("E01"))
        self.assertIn("one language", out["stack"]["backend"]["justification"])
        back = [e for e in KB.entries("backend") if e["name"] == out["stack"]["backend"]["choice"]][0]
        self.assertTrue({"typescript", "javascript"} & set(back["languages"]))

    def test_php_team_gets_a_php_backend_and_no_kotlin(self):
        out = engine.run(edge("E17"))
        self.assertEqual(out["stack"]["backend"]["choice"], "Laravel")
        self.assertNotIn("Kotlin", out["stack"]["frontend"]["choice"])
        n = sum(1 for w in out["warnings"] if w["code"] == "SKILL_MISMATCH")
        self.assertLessEqual(n, 1)


def high_rps():
    raw = minimal()
    raw["project"] = {"type": "ecommerce", "platforms": ["api_only"]}
    raw["scale"] = {"users_at_launch": 64307, "users_12_months": 100000, "peak_rps": 350934}
    raw["budget"] = {"monthly_infra_usd": 20}
    raw["infrastructure"] = {"deployment_model": "self_hosted_cloud_vm", "data_residency": "india"}
    return raw


class ScaleAndEmbedded(unittest.TestCase):
    def test_embedded_database_is_not_the_server_database_at_scale(self):
        out = engine.run(high_rps())
        self.assertNotIn("SQLite", out["stack"]["database"]["choice"])
        why = [a["why_not"] for a in out["stack"]["database"]["alternatives"] if "SQLite" in a["choice"]]
        self.assertTrue(why and "requests per second" in why[0], why)
        self.assertIn("BUDGET_EXCEEDED", [w["code"] for w in out["warnings"]])

    def test_concurrent_write_types_exclude_embedded_database(self):
        raw = with_value(minimal(), "project.type", "marketplace")
        self.assertNotIn("SQLite", engine.run(raw)["stack"]["database"]["choice"])

    def test_cost_scales_with_peak_rps(self):
        cost = engine.run(high_rps())["estimated_monthly_cost_usd"]
        self.assertGreater(cost["min"], 10000)
        self.assertTrue(any("scale.peak_rps is 350,934" in a for a in cost["assumptions"]))
        self.assertTrue(any("sizing" in r["component"] for r in cost["breakdown"]))

    def test_no_rps_sizing_without_peak_rps(self):
        for n in ("P1", "P2", "P3", "P4", "P5", "P6"):
            cost = engine.run(public(n))["estimated_monthly_cost_usd"]
            self.assertFalse(any("sizing" in r["component"] for r in cost["breakdown"]), n)
            self.assertFalse(any("peak_rps" in a for a in cost["assumptions"]), n)

    def game(self, description, langs=("csharp",), platforms=("android", "ios")):
        raw = with_value(minimal(), "project.type", "gaming")
        raw = with_value(raw, "project.platforms", list(platforms))
        raw = with_value(raw, "project.description", description)
        return engine.run(with_value(raw, "team.languages", list(langs)))

    def test_game_client_gets_a_game_engine(self):
        front = self.game("Real-time multiplayer shooter for Android and iOS")["stack"]["frontend"]
        self.assertIn(front["choice"], ("Unity", "Godot"))
        self.assertIn("game client needs an engine", front["justification"])
        self.assertTrue(any("not a real-time game loop" in a["why_not"] for a in front["alternatives"]))

    def test_game_client_without_csharp_still_gets_an_engine(self):
        front = self.game("2D platformer for Android and iOS", langs=("python",))["stack"]["frontend"]
        self.assertIn(front["choice"], ("Unity", "Godot"))

    def test_lobby_or_dashboard_for_a_game_keeps_an_app_framework(self):
        for text in ("Matchmaking lobby and live leaderboard for a mobile game",
                     "Admin dashboard for a multiplayer shooter"):
            with self.subTest(text):
                choice = self.game(text)["stack"]["frontend"]["choice"]
                self.assertNotIn(choice, ("Unity", "Godot"))

    def test_game_engines_stay_out_of_other_project_types(self):
        raw = with_value(minimal(), "project.description", "multiplayer shooter")
        raw = with_value(raw, "project.platforms", ["android", "ios"])
        self.assertNotIn(engine.run(raw)["stack"]["frontend"]["choice"], ("Unity", "Godot"))


if __name__ == "__main__":
    unittest.main()
