"""Integrity checks for the knowledge base JSON files."""
import json
import os
import re
import unittest

KB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "stacksense", "kb")
CORE = ["frontend", "backend", "database", "hosting"]
ZONES = {"india", "eu", "us", "apac"}
COMPLIANCE_CODES = ["dpdp", "rbi_localisation", "pci_dss", "hipaa", "gdpr", "soc2", "iso27001"]
RATING_KEYS = ["dev_speed", "learning_curve", "performance", "scalability", "ops_burden",
               "maturity", "ecosystem", "security", "resource_efficiency"]


def load(name):
    with open(os.path.join(KB_DIR, name + ".json"), encoding="utf-8") as f:
        return json.load(f)


def entries():
    out = []
    for name in CORE + ["supporting"]:
        out.extend(load(name))
    return out


def offerings(entry):
    return entry.get("offerings", [])


def check_sources(testcase, sources, label):
    testcase.assertTrue(sources, label + ": no sources")
    for s in sources:
        testcase.assertTrue(s.get("title"), label + ": source without title")
        testcase.assertTrue(str(s.get("url", "")).startswith("https://"), label + ": non-https source")


class TestKB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.providers = load("providers")
        cls.entries = entries()
        cls.deprecated = load("deprecated")
        cls.compliance = load("compliance")
        cls.existing = load("existing_systems")
        cls.references = load("references")

    def test_core_layers_have_enough_active_entries(self):
        for layer in CORE:
            active = [e for e in load(layer) if e["status"] == "active"]
            self.assertGreaterEqual(len(active), 8, layer)
        web = [e for e in load("frontend") if e["status"] == "active" and "web" in e["platforms"]]
        self.assertGreaterEqual(len(web), 8)

    def test_supporting_layers_have_four_options(self):
        counts = {}
        for e in load("supporting"):
            counts[e["layer"]] = counts.get(e["layer"], 0) + 1
        for layer, n in counts.items():
            self.assertGreaterEqual(n, 4, layer)

    def test_unique_ids(self):
        for name in CORE + ["supporting"]:
            ids = [e["id"] for e in load(name)]
            self.assertEqual(len(ids), len(set(ids)), name)
        offs = [o["id"] for e in self.entries for o in offerings(e)]
        self.assertEqual(len(offs), len(set(offs)), "offering ids")
        pids = [p["id"] for p in self.providers]
        self.assertEqual(len(pids), len(set(pids)), "provider ids")

    def test_ratings_in_range(self):
        for e in self.entries:
            self.assertEqual(sorted(e["ratings"]), sorted(RATING_KEYS), e["id"])
            for k, v in e["ratings"].items():
                self.assertIn(v, (1, 2, 3, 4, 5), e["id"] + "." + k)

    def test_sources_present_and_https(self):
        for e in self.entries:
            check_sources(self, e.get("sources"), e["id"])
            for o in offerings(e):
                check_sources(self, o.get("sources"), o["id"])
        for p in self.providers:
            check_sources(self, p.get("sources"), p["id"])
        for d in self.deprecated:
            check_sources(self, d.get("sources"), d["name"])
        for c in self.compliance:
            check_sources(self, c.get("sources"), c["code"])
        for x in self.existing:
            check_sources(self, x.get("sources"), x["id"])
        for r in self.references:
            self.assertTrue(r["url"].startswith("https://"), r["id"])

    def test_region_keys_valid(self):
        for e in self.entries:
            for o in offerings(e):
                self.assertTrue(set(o["regions"]) <= ZONES, o["id"])
        for p in self.providers:
            self.assertTrue(set(p["regions"]) <= ZONES, p["id"])
            self.assertTrue(set(p["full_residency"]) <= ZONES, p["id"])

    def test_offering_providers_exist(self):
        known = {p["id"] for p in self.providers}
        for e in self.entries:
            for o in offerings(e):
                self.assertIn(o["provider"], known, o["id"])

    def test_cost_anchors_shape(self):
        for e in self.entries:
            for o in offerings(e):
                a = o["cost_anchors"]
                # the four standard points, with lower ones allowed for per-seat tools priced for small teams
                pts = [r[0] for r in a]
                self.assertEqual(pts[-4:], [1000, 10000, 100000, 1000000], o["id"])
                self.assertEqual(pts, sorted(set(pts)), o["id"])
                for _, lo, hi in a:
                    self.assertTrue(0 <= lo <= hi, o["id"])

    def test_no_active_entry_matches_deprecated(self):
        pats = [(re.compile(d["pattern"], re.I), d["name"]) for d in self.deprecated]
        for e in self.entries:
            for text in (e["name"], e["id"], e.get("recommended_version") or ""):
                for rx, name in pats:
                    self.assertIsNone(rx.search(text), "%s (%r) matches deprecated %s" % (e["id"], text, name))

    def test_deprecated_entries_dated(self):
        statuses = {"eol", "retired", "shutdown", "maintenance", "renamed", "closed_to_new"}
        for d in self.deprecated:
            self.assertIn(d["status"], statuses, d["name"])
            self.assertRegex(d["date"], r"^\d{4}-\d{2}-\d{2}$", d["name"])
            self.assertIn(d["severity_rule"], ("by_date", "warning", "info"), d["name"])
            re.compile(d["pattern"], re.I)

    def test_compliance_rows_cover_all_codes(self):
        codes = {c["code"] for c in self.compliance}
        self.assertEqual(codes, set(COMPLIANCE_CODES))

    def test_existing_systems_patterns_and_prefs(self):
        ids = {e["id"] for e in self.entries}
        text = " ".join(x["pattern"] for x in self.existing)
        for needle in ("entra", "sql", "365"):
            self.assertIn(needle, text.lower())
        for x in self.existing:
            re.compile(x["pattern"], re.I)
            for layer, eff in x["effects"].items():
                self.assertIn(eff["prefer"], ids, x["id"])

    def test_existing_systems_not_hidden_by_deprecated(self):
        rx = [re.compile(d["pattern"], re.I) for d in self.deprecated]
        for sample in ("SQL Server 2012", "Azure AD", "Node.js 18", ".NET Framework 4.5.2"):
            self.assertTrue(any(r.search(sample) for r in rx), sample)
        for sample in ("SQL Server 2022", ".NET 10 LTS", "Docker Compose", "AWS IoT Core", "Azure Managed Redis"):
            self.assertFalse(any(r.search(sample) for r in rx), sample)

    def test_self_hosted_provider_documented(self):
        self.assertIn("self", {p["id"] for p in self.providers})


if __name__ == "__main__":
    unittest.main()
