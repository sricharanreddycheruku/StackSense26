import glob
import os
import re
import unittest

from stacksense import engine
from stacksense.model import article

from tests.helpers import EDGE_DIR, PUBLIC_DIR, edge, load_json, public

LANG_CLAIM = re.compile(r"which the team already (?:uses|writes)|like the frontend")
WEEKS = re.compile(r"\b(an?) (\d[\d,]*)-week")


def outputs():
    for path in sorted(glob.glob(os.path.join(PUBLIC_DIR, "*.json")) + glob.glob(os.path.join(EDGE_DIR, "*.json"))):
        try:
            raw = load_json(path)
        except ValueError:
            continue
        out = engine.run(raw)
        if out["status"] in ("ok", "ok_with_warnings"):
            yield os.path.basename(path), out


def components(out):
    for key, value in out["stack"].items():
        for comp in (value if isinstance(value, list) else [value]):
            if isinstance(comp, dict) and "justification" in comp:
                yield key, comp


def texts(out):
    for key, comp in components(out):
        yield key, comp["justification"]
        for alt in comp["alternatives"]:
            yield key, alt["why_not"]
        for line in comp["tradeoffs"]:
            yield key, line


class Wording(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.outs = list(outputs())

    def test_article_follows_the_spoken_number(self):
        for n, want in ((2, "a"), (6, "a"), (8, "an"), (10, "a"), (11, "an"), (12, "a"), (18, "an"), (20, "a"),
                        (80, "an"), (89, "an"), (100, "a"), (800, "an")):
            self.assertEqual(article(n), want, n)

    def test_every_week_count_has_the_right_article(self):
        for name, out in self.outs:
            for key, text in texts(out):
                for art, n in WEEKS.findall(text):
                    self.assertEqual(art, article(int(n.replace(",", ""))), f"{name} {key}: {text}")

    def test_reasons_after_because_are_clauses(self):
        bad = re.compile(r"because (?:suited|fits|built|largest|built-in|vector|written)\b")
        for name, out in self.outs:
            for key, text in texts(out):
                self.assertNotRegex(text, bad, f"{name} {key}")

    def test_contribution_above_the_gap_is_not_called_part_of_it(self):
        pat = re.compile(r"which accounts for ([\d.]+) of the ([\d.]+)-point gap")
        for name, out in self.outs:
            for key, text in texts(out):
                for x, y in pat.findall(text):
                    self.assertLessEqual(float(x), float(y) + 0.05, f"{name} {key}: {text}")

    def test_eight_week_mvp_reads_an_eight_week_mvp(self):
        raw = public("P1")
        raw["timeline"]["mvp_weeks"] = 8
        text = " ".join(t for _, t in texts(engine.run(raw)))
        self.assertNotIn("a 8-week", text)

    def test_no_justification_repeats_a_language_fit_claim(self):
        for name, out in self.outs:
            for key, comp in components(out):
                self.assertLessEqual(len(LANG_CLAIM.findall(comp["justification"])), 1,
                                     f"{name} {key}: {comp['justification']}")

    def test_no_why_not_prints_a_zero_point_gap(self):
        for name, out in self.outs:
            for key, comp in components(out):
                for alt in comp["alternatives"]:
                    self.assertNotRegex(alt["why_not"], r"\b0\.0 points", f"{name} {key}")
                    self.assertTrue(alt["why_not"].strip(), f"{name} {key}")

    def test_near_tie_names_the_deciding_point(self):
        alts = {a["choice"]: a["why_not"] for a in engine.run(public("P1"))["stack"]["hosting"]["alternatives"]}
        self.assertIn("about equal", alts["Netlify"])
        self.assertIn("free-tier", alts["Netlify"])

    def test_chosen_component_never_lists_a_poor_fit(self):
        for name, out in self.outs:
            for key, comp in components(out):
                for line in comp["tradeoffs"]:
                    self.assertNotIn("poor fit", line.lower(), f"{name} {key}")

    def test_poor_fit_choice_says_why_it_still_wins(self):
        raw = edge("E05")
        raw["project"]["type"] = "ecommerce"
        text = engine.run(raw)["stack"]["database"]["justification"]
        self.assertIn("weaker fit for project.type ecommerce", text)
        self.assertRegex(text, r"still ranks first here, \d+\.\d points ahead of ")
        raw = public("P1")
        raw["project"]["type"] = "fintech"
        text = engine.run(raw)["stack"]["hosting"]["justification"]
        self.assertIn("still ranks first here.", text)
        self.assertNotIn("points ahead", text)

    def test_auth_names_the_cloud_not_the_product_vendor(self):
        auth = next(c["justification"] for c in engine.run(public("P3"))["stack"]["supporting"] if c["layer"] == "auth")
        self.assertIn("same cloud (Google", auth)
        for name, out in self.outs:
            for key, text in texts(out):
                self.assertNotIn("same vendor (firebase)", text, f"{name} {key}")

    def test_memory_tradeoff_is_relative_for_the_chosen_option(self):
        front = engine.run(public("P6"))["stack"]["frontend"]
        self.assertTrue(any("Heavier than native; keep the bundle lean for 2 GB" in t for t in front["tradeoffs"]))
        self.assertFalse(any(t.startswith("Heavy on memory") for t in front["tradeoffs"]))

    def test_existing_sql_server_on_managed_cloud_is_a_migration(self):
        out = engine.run(public("P5"))
        self.assertIn("migrates the existing system", out["stack"]["database"]["justification"])
        msg = next(w["message"] for w in out["warnings"]
                   if w["code"] == "EXISTING_SYSTEM_REUSED" and "SQL Server" in w["message"])
        self.assertIn("Azure Database Migration Service", msg)
        self.assertNotIn("system of record", msg)
        self.assertTrue(any(w["code"] == "EOL_EXISTING_SYSTEM" and "SQL Server" in w["message"] for w in out["warnings"]))

    def test_host_does_not_claim_to_reuse_an_identity_provider(self):
        host = engine.run(public("P5"))["stack"]["hosting"]["justification"]
        self.assertNotIn("reuses the existing system", host)
        self.assertIn("integrates with the existing Azure tenant", host)

    def test_two_part_hosting_explains_the_api_host_and_offers_an_alternative(self):
        seen = 0
        for name, out in self.outs:
            host = out["stack"]["hosting"]
            if " + " not in host["choice"]:
                continue
            seen += 1
            web, api = host["choice"].split(" + ", 1)
            self.assertIn(f"The web build runs on {web} and the API on {api} (", host["justification"], name)
            self.assertTrue(any(not a["why_not"].startswith("Excluded") for a in host["alternatives"]), name)
        self.assertGreater(seen, 0)

    def test_single_host_says_how_web_and_api_share_it(self):
        host = engine.run(public("P4"))["stack"]["hosting"]
        self.assertNotIn(" + ", host["choice"])
        self.assertIn("web server and the Spring Boot API run as separate services on it", host["justification"])

    def test_mobile_only_hosting_does_not_suggest_a_web_host(self):
        host = engine.run(public("P6"))["stack"]["hosting"]["justification"]
        self.assertNotIn("web host", host)


if __name__ == "__main__":
    unittest.main()
