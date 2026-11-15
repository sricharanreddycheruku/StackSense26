import json
import os
import tempfile
import unittest

from stacksense import describe
from stacksense.derive import derive
from stacksense.schema import FIELDS, REQUIRED_PATHS
from stacksense.validate import validate
from tests import helpers
from tests.helpers import minimal, with_value


def codes(v):
    return [w.code for w in v.warnings]


def error_fields(v):
    return [e.field for e in v.errors]


class ValidationTests(unittest.TestCase):
    def test_minimal_scenario_is_valid_and_fully_defaulted(self):
        v = validate(minimal())
        self.assertEqual(v.errors, [])
        spec = v.spec
        self.assertEqual(spec["scenario_id"], "unnamed")
        self.assertEqual(spec["scale"]["traffic_pattern"], "steady")
        self.assertEqual(spec["scale"]["peak_concurrent_users"], 50)
        self.assertEqual(spec["scale"]["data_volume_gb_12_months"], 2.0)
        self.assertEqual(spec["requirements"]["availability"], "99.9")
        self.assertIsNone(spec["requirements"]["p95_latency_ms"])
        self.assertEqual(spec["priorities"], {k: 3 for k in spec["priorities"]})
        self.assertEqual(spec["compliance"], [])

    def test_every_default_has_an_assumption(self):
        v = validate(minimal())
        optional_standard = [f.path for f in FIELDS
                             if f.type != "object" and not f.required and not f.extra]
        for path in optional_standard:
            self.assertTrue(any(a.startswith(path + " not provided; defaulted to ") for a in v.assumptions),
                            path)

    def test_extras_are_left_absent(self):
        v = validate(minimal())
        self.assertNotIn("peak_rps", v.spec["scale"])
        self.assertNotIn("connectivity", v.spec["requirements"])

    def test_each_required_field_reports_its_own_error(self):
        for path in REQUIRED_PATHS:
            v = validate(with_value(minimal(), path, ...))
            self.assertEqual(error_fields(v), [path], path)
            self.assertEqual(v.errors[0].message, "required field is missing")

    def test_all_missing_required_fields_are_reported_together(self):
        v = validate({})
        self.assertEqual(sorted(error_fields(v)), sorted(REQUIRED_PATHS))

    def test_empty_platforms_is_an_error(self):
        v = validate(with_value(minimal(), "project.platforms", []))
        self.assertEqual(error_fields(v), ["project.platforms"])

    def test_empty_languages_is_allowed_with_info(self):
        v = validate(with_value(minimal(), "team.languages", []))
        self.assertEqual(v.errors, [])
        self.assertIn("NO_TEAM_LANGUAGES", codes(v))

    def test_root_must_be_an_object(self):
        for bad in ([], 5, None, [{"a": 1}]):
            v = validate(bad)
            self.assertEqual([(e.field, e.message) for e in v.errors],
                             [("", "Scenario must be a JSON object")])

    def test_malformed_json_text_reports_line_and_column(self):
        v = validate('{"project": {"type": "web_app",, "platforms": ["web"]}}')
        self.assertEqual([(e.field, e.message) for e in v.errors],
                         [("", "Input is not valid JSON: Expecting property name enclosed in double quotes "
                               "(line 1, column 32)")])
        self.assertEqual([(e.field, e.message) for e in validate('"text"').errors],
                         [("", "Scenario must be a JSON object")])

    def test_e03_reports_every_problem(self):
        v = validate(helpers.edge("E03"))
        expected = {
            "project.type", "project.platforms", "scale.users_at_launch", "scale.users_12_months",
            "scale.traffic_pattern", "team.size", "team.experience", "team.languages",
            "team.has_devops", "budget.monthly_infra_usd", "timeline.mvp_weeks",
            "infrastructure.cloud_preference", "infrastructure.deployment_model",
            "requirements.realtime", "requirements.auth", "requirements.availability",
            "requirements.p95_latency_ms", "compliance", "priorities.cost",
            "priorities.security", "priorities.performance",
        }
        self.assertEqual(set(error_fields(v)), expected)
        by_field = {e.field: e.message for e in v.errors}
        self.assertIn("did you mean 'web_app'", by_field["project.type"])
        self.assertIn("did you mean 'aws'", by_field["infrastructure.cloud_preference"])
        self.assertIn("integer expected, got string '5000'", by_field["scale.users_at_launch"])
        self.assertIn('use the string "99.9"', by_field["requirements.availability"])

    def test_integer_fields_reject_bool_float_and_string(self):
        for bad in (True, 5.5, "5", None):
            v = validate(with_value(minimal(), "team.size", bad))
            self.assertEqual(len(v.errors), 1, bad)
            self.assertEqual(v.errors[0].field, "team.size")

    def test_number_fields_reject_nan_infinity_and_bool(self):
        for text in ("NaN", "Infinity", "-Infinity"):
            raw = json.loads('{"budget": {"monthly_infra_usd": %s}}' % text)
            v = validate(raw)
            self.assertIn("budget.monthly_infra_usd", error_fields(v))
        v = validate(with_value(minimal(), "budget.monthly_infra_usd", True))
        self.assertEqual(error_fields(v), ["budget.monthly_infra_usd"])

    def test_ranges(self):
        cases = [("team.size", 0), ("team.size", 10001), ("timeline.mvp_weeks", 0),
                 ("timeline.mvp_weeks", 521), ("budget.monthly_infra_usd", -1),
                 ("budget.monthly_infra_usd", 10_000_001), ("scale.users_12_months", -1),
                 ("priorities.cost", 0), ("priorities.cost", 6)]
        for path, value in cases:
            v = validate(with_value(minimal(), path, value))
            self.assertEqual(error_fields(v), [path], (path, value))
        for path, value in [("team.size", 10000), ("timeline.mvp_weeks", 520),
                            ("budget.monthly_infra_usd", 0), ("priorities.cost", 5)]:
            self.assertEqual(validate(with_value(minimal(), path, value)).errors, [])

    def test_availability_must_be_a_string_value(self):
        v = validate(with_value(minimal(), "requirements.availability", 99.9))
        self.assertIn('use the string "99.9"', v.errors[0].message)
        v = validate(with_value(minimal(), "requirements.availability", "99.5"))
        self.assertEqual(error_fields(v), ["requirements.availability"])
        v = validate(with_value(minimal(), "requirements.availability", "99.99"))
        self.assertEqual(v.errors, [])

    def test_p95_accepts_null_or_positive_integer(self):
        self.assertEqual(validate(with_value(minimal(), "requirements.p95_latency_ms", None)).errors, [])
        self.assertEqual(validate(with_value(minimal(), "requirements.p95_latency_ms", 250)).errors, [])
        for bad in (0, -5, 1.5, "100"):
            v = validate(with_value(minimal(), "requirements.p95_latency_ms", bad))
            self.assertEqual(error_fields(v), ["requirements.p95_latency_ms"], bad)

    def test_enum_values_are_exact_lower_case(self):
        v = validate(with_value(minimal(), "project.type", "Web_App"))
        self.assertEqual(error_fields(v), ["project.type"])
        v = validate(with_value(minimal(), "project.type", 7))
        self.assertEqual(error_fields(v), ["project.type"])

    def test_wrong_section_type_skips_child_checks(self):
        raw = minimal()
        raw["scale"] = []
        v = validate(raw)
        self.assertEqual(error_fields(v), ["scale"])

    def test_unknown_fields_are_ignored_with_info(self):
        raw = with_value(minimal(), "scale.regions_planned", 3)
        raw["extra_root"] = {"a": 1}
        v = validate(raw)
        self.assertEqual(v.errors, [])
        unknown = sorted(w.fields[0] for w in v.warnings if w.code == "UNKNOWN_FIELD")
        self.assertEqual(unknown, ["extra_root", "scale.regions_planned"])
        self.assertNotIn("regions_planned", v.spec["scale"])

    def test_duplicates_are_removed_with_info(self):
        raw = with_value(minimal(), "project.platforms", ["web", "web", "android"])
        raw["compliance"] = ["gdpr", "gdpr"]
        v = validate(raw)
        self.assertEqual(v.spec["project"]["platforms"], ["web", "android"])
        self.assertEqual(v.spec["compliance"], ["gdpr"])
        dup = sorted(w.fields[0] for w in v.warnings if w.code == "DUPLICATE_VALUE")
        self.assertEqual(dup, ["compliance", "project.platforms"])

    def test_e26_messy_export(self):
        v = validate(helpers.edge("E26"))
        self.assertEqual(v.errors, [])
        self.assertEqual(v.spec["team"]["languages"], ["python", "typescript"])
        self.assertTrue(any("normalised" in a for a in v.assumptions))
        self.assertIn("UNKNOWN_FIELD", codes(v))
        self.assertIn("DUPLICATE_VALUE", codes(v))

    def test_languages_are_normalised_and_unknown_ones_noted(self):
        v = validate(with_value(minimal(), "team.languages", [" Python ", "Zig"]))
        self.assertEqual(v.spec["team"]["languages"], ["python", "zig"])
        self.assertTrue(any("'zig'" in a and "free text" in a for a in v.assumptions))

    def test_long_text_is_truncated_with_assumption(self):
        v = validate(with_value(minimal(), "project.description", "x" * 6000))
        self.assertEqual(len(v.spec["project"]["description"]), 5000)
        self.assertTrue(any("truncated" in a for a in v.assumptions))

    def test_existing_systems_limits(self):
        v = validate(with_value(minimal(), "infrastructure.existing_systems", ["a"] * 51))
        self.assertIn("infrastructure.existing_systems", error_fields(v))
        v = validate(with_value(minimal(), "infrastructure.existing_systems", ["x" * 201]))
        self.assertIn("infrastructure.existing_systems", error_fields(v))

    def test_cross_field_warnings(self):
        raw = with_value(minimal(), "scale.peak_concurrent_users", 5000)
        self.assertIn("PEAK_EXCEEDS_USERS", codes(validate(raw)))
        raw = with_value(minimal(), "scale.users_12_months", 50)
        self.assertIn("USERS_DECLINE", codes(validate(raw)))
        raw = with_value(minimal(), "project.platforms", ["api_only", "web"])
        self.assertIn("API_ONLY_MIXED", codes(validate(raw)))

    def test_extras_are_validated_when_given(self):
        raw = with_value(minimal(), "infrastructure.vendor_lock_in_tolerance", "none")
        raw = with_value(raw, "team.size_in_12_months", 0)
        raw = with_value(raw, "requirements.notifications", ["email", "fax"])
        v = validate(raw)
        self.assertEqual(sorted(error_fields(v)), sorted([
            "infrastructure.vendor_lock_in_tolerance", "team.size_in_12_months",
            "requirements.notifications"]))
        raw = with_value(minimal(), "requirements.connectivity", "poor")
        v = validate(raw)
        self.assertEqual(v.errors, [])
        self.assertEqual(v.spec["requirements"]["connectivity"], "poor")
        self.assertIn("requirements.connectivity", v.provided)

    def test_bom_file_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bom.json")
            with open(path, "w", encoding="utf-8-sig") as fh:
                json.dump(minimal(), fh)
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(3), b"\xef\xbb\xbf")
            v = validate(helpers.load_json(path))
        self.assertEqual(v.errors, [])

    def test_never_raises_on_garbage(self):
        garbage = [None, 0, "", [], {}, {"project": 5}, {"scale": {"users_at_launch": 10 ** 400}},
                   {"team": {"languages": [1, None, {}]}}, {"compliance": {"a": 1}},
                   {"priorities": []}, {"project": {"platforms": [["web"]]}},
                   {"infrastructure": {"existing_systems": [None, 3]}}]
        for item in garbage:
            v = validate(item)
            self.assertIsInstance(v.errors, list)

    def test_all_edge_files_validate_without_raising(self):
        for name in sorted(os.listdir(helpers.EDGE_DIR)):
            try:
                raw = helpers.load_json(os.path.join(helpers.EDGE_DIR, name))
            except ValueError:
                continue  # E04b is deliberately malformed JSON
            validate(raw)

    def test_public_scenarios_are_valid(self):
        for n in range(1, 7):
            v = validate(helpers.public("P%d" % n))
            self.assertEqual(v.errors, [], n)
            self.assertEqual(v.spec["scenario_id"], "P%d" % n)

    def test_key_order_does_not_matter(self):
        raw = helpers.public("P2")
        shuffled = {k: raw[k] for k in reversed(list(raw))}
        shuffled["scale"] = {k: raw["scale"][k] for k in reversed(list(raw["scale"]))}
        a, b = validate(raw), validate(shuffled)
        self.assertEqual(a.spec, b.spec)
        self.assertEqual(a.assumptions, b.assumptions)


class DescribeTests(unittest.TestCase):
    def fields(self, text):
        return {(s.field, s.value) for s in describe.parse(text)}

    def test_describe_p2_realtime_and_payments(self):
        found = self.fields(helpers.public("P2")["project"]["description"])
        self.assertIn(("requirements.realtime", True), found)
        self.assertIn(("requirements.payments", True), found)

    def test_describe_p6_offline_intermittent_and_ram(self):
        found = self.fields(helpers.public("P6")["project"]["description"])
        self.assertIn(("requirements.offline_support", True), found)
        self.assertIn(("requirements.connectivity", "intermittent"), found)
        self.assertIn(("requirements.min_device_ram_gb", 2), found)
        self.assertIn(("requirements.file_media_storage", True), found)
        self.assertIn(("derived.geo", True), found)

    def test_describe_india_detected(self):
        found = self.fields("Home services in Bengaluru and Pune, paid through UPI.")
        self.assertIn(("scale.user_geography", "india"), found)
        for name in ("P2", "P6"):
            d = derive(validate(helpers.public(name)))
            self.assertEqual(d.user_geography, ("india",))
            self.assertEqual(d.preferred_zone, "india")

    def test_describe_residency_needs_a_storage_phrase(self):
        self.assertIn(("infrastructure.data_residency", "india"), self.fields("Patient data must stay in India."))
        self.assertIn(("infrastructure.data_residency", "eu"), self.fields("Records are stored only within the EU."))
        self.assertIn(("infrastructure.data_residency", "us"), self.fields("Hosted in US data centers."))
        plain = self.fields("Users in India and Europe; data localisation rules apply.")
        self.assertFalse(any(f == "infrastructure.data_residency" for f, _ in plain))

    def test_describe_europe_and_health_and_finance(self):
        found = self.fields("Clinic booking for patients in Germany, with loan EMI plans.")
        self.assertIn(("scale.user_geography", "eu"), found)
        self.assertIn(("requirements.data_sensitivity", "health"), found)
        self.assertIn(("requirements.data_sensitivity", "financial"), found)

    def test_describe_signals_are_sorted_unapplied_and_repeatable(self):
        text = helpers.public("P4")["project"]["description"]
        a, b = describe.parse(text), describe.parse(text)
        self.assertEqual(a, b)
        self.assertEqual(a, sorted(a, key=lambda s: (s.field, s.keyword, repr(s.value))))
        self.assertTrue(a and not any(s.applied for s in a))

    def test_describe_uses_word_boundaries(self):
        self.assertNotIn(("requirements.ml_features", "inference_api"),
                         self.fields("amlodipine html"))
        self.assertIn(("requirements.ml_features", "inference_api"), self.fields("an ML model"))

    def test_describe_handles_empty_and_non_text(self):
        for bad in ("", "   ", None, 5):
            self.assertEqual(describe.parse(bad), [])
            self.assertEqual(describe.suggest(bad), [])

    def test_describe_suggest_lists_only_input_fields(self):
        rows = describe.suggest(helpers.public("P6")["project"]["description"])
        self.assertTrue(rows)
        from stacksense.schema import FIELD_INDEX
        for row in rows:
            self.assertIn(row["field"], FIELD_INDEX)
            self.assertTrue(row["keywords"])

    def test_describe_low_end_without_number_means_two_gb(self):
        self.assertIn(("requirements.min_device_ram_gb", 2), self.fields("runs on low-end phones"))
        self.assertIn(("requirements.min_device_ram_gb", 4), self.fields("needs 4 GB RAM"))


class DeriveTests(unittest.TestCase):
    def d(self, raw):
        return derive(validate(raw))

    def test_p1_needs_no_backend(self):
        d = self.d(helpers.public("P1"))
        self.assertFalse(d.needs_backend)
        self.assertFalse(d.needs_database)
        self.assertTrue(d.static_hint)

    def test_p2_p3_p4_p5_p6_need_backend_and_database(self):
        for name in ("P2", "P3", "P4", "P5", "P6"):
            d = self.d(helpers.public(name))
            self.assertTrue(d.needs_backend and d.needs_database, name)

    def test_p6_connectivity_is_intermittent_and_logged(self):
        d = self.d(helpers.public("P6"))
        self.assertEqual(d.connectivity, "intermittent")
        self.assertEqual(d.min_device_ram_gb, 2)
        self.assertTrue(any(a.startswith("requirements.connectivity not provided; derived as intermittent")
                            for a in d.assumptions))
        applied = {(s.field, s.applied) for s in d.signals if s.field == "requirements.connectivity"}
        self.assertEqual(applied, {("requirements.connectivity", True)})

    def test_p4_security_weight_is_five(self):
        d = self.d(helpers.public("P4"))
        self.assertEqual(d.weights["security"], 5.0)
        self.assertEqual(d.data_sensitivity, "financial")
        self.assertEqual(d.zone, "india")

    def test_security_floor_applies_for_regulated_scope(self):
        raw = with_value(minimal(), "compliance", ["hipaa"])
        raw = with_value(raw, "priorities", {"security": 1})
        d = self.d(raw)
        self.assertEqual(d.weights["security"], 4.0)
        self.assertTrue(any("priorities.security is 1; raised to 4" in a for a in d.assumptions))

    def test_performance_floor_for_tight_latency(self):
        raw = with_value(minimal(), "requirements.p95_latency_ms", 80)
        raw = with_value(raw, "priorities", {"performance": 2})
        self.assertEqual(self.d(raw).weights["performance"], 4.0)

    def test_signals_never_change_standard_fields(self):
        v = validate(helpers.public("P2"))
        before = json.dumps(v.spec, sort_keys=True)
        raw = with_value(minimal(), "project.description", "live chat with payments and offline sync")
        v2 = validate(raw)
        derive(v2)
        self.assertEqual(before, json.dumps(validate(helpers.public("P2")).spec, sort_keys=True))
        self.assertFalse(v2.spec["requirements"]["realtime"])
        self.assertFalse(v2.spec["requirements"]["offline_support"])

    def test_given_extras_win_over_description(self):
        raw = with_value(minimal(), "project.description", "villages with intermittent signal, 2 GB RAM")
        raw = with_value(raw, "requirements.connectivity", "reliable")
        raw = with_value(raw, "requirements.min_device_ram_gb", 8)
        d = self.d(raw)
        self.assertEqual(d.connectivity, "reliable")
        self.assertEqual(d.min_device_ram_gb, 8)

    def test_every_absent_extra_has_an_assumption(self):
        d = self.d(minimal())
        for f in FIELDS:
            if f.extra:
                self.assertTrue(any(a.startswith(f.path + " not provided; derived as ")
                                    for a in d.assumptions), f.path)

    def test_extras_given_are_used(self):
        raw = with_value(minimal(), "scale.peak_rps", 900)
        raw = with_value(raw, "team.size_in_12_months", 9)
        raw = with_value(raw, "infrastructure.vendor_lock_in_tolerance", "low")
        raw = with_value(raw, "infrastructure.existing_licences", ["microsoft_365"])
        raw = with_value(raw, "requirements.rpo_minutes", 5)
        d = self.d(raw)
        self.assertEqual(d.peak_rps, 900.0)
        self.assertEqual(d.team_size_12m, 9)
        self.assertEqual(d.lock_in_tolerance, "low")
        self.assertEqual(d.licences, frozenset({"microsoft_365"}))
        self.assertEqual(d.rpo_minutes, 5)
        self.assertGreater(d.req_scale_rating, self.d(minimal()).req_scale_rating)

    def test_rpo_follows_availability(self):
        for avail, rpo in (("99", 1440), ("99.9", 60), ("99.95", 15), ("99.99", 5)):
            self.assertEqual(self.d(with_value(minimal(), "requirements.availability", avail)).rpo_minutes, rpo)

    def test_scale_rating_is_smooth_and_bounded(self):
        last = 0
        for users in (10, 1000, 10 ** 4, 10 ** 5, 10 ** 6, 10 ** 8, 10 ** 10):
            raw = with_value(minimal(), "scale.users_12_months", users)
            raw = with_value(raw, "scale.users_at_launch", 0)
            r = self.d(raw).req_scale_rating
            self.assertTrue(1.0 <= r <= 5.0)
            self.assertGreaterEqual(r, last)
            last = r

    def test_zone_and_preferred_zone(self):
        self.assertIsNone(self.d(minimal()).zone)
        d = self.d(minimal())
        self.assertEqual(d.preferred_zone, "us")
        self.assertTrue(any("defaulting to a US region" in a for a in d.assumptions))
        d = self.d(with_value(minimal(), "compliance", ["rbi_localisation"]))
        self.assertEqual(d.zone, "india")
        d = self.d(with_value(minimal(), "compliance", ["gdpr"]))
        self.assertIsNone(d.zone)
        self.assertEqual(d.preferred_zone, "eu")
        d = self.d(with_value(minimal(), "infrastructure.data_residency", "eu"))
        self.assertEqual((d.zone, d.preferred_zone), ("eu", "eu"))

    def test_effort_estimate_flags_p2_as_tight_but_not_impossible(self):
        d = self.d(helpers.public("P2"))
        mvp = 8
        self.assertTrue(0.5 * d.est_mvp_weeks <= mvp < d.est_mvp_weeks)
        for name in ("P1", "P3", "P4", "P5", "P6"):
            d = self.d(helpers.public(name))
            mvp = helpers.public(name)["timeline"]["mvp_weeks"]
            self.assertGreaterEqual(mvp, d.est_mvp_weeks - 1e-9, name)

    def test_existing_identity_provider_lowers_sso_effort(self):
        base = with_value(minimal(), "requirements.auth", "enterprise_sso")
        reuse = with_value(base, "infrastructure.existing_systems", ["Microsoft Entra ID (Azure AD)"])
        self.assertEqual(self.d(base).effort_points - self.d(reuse).effort_points, 1)

    def test_existing_systems_matched_against_kb_when_given(self):
        class FakeKB:
            existing = [{"id": "sql_server", "pattern": r"sql server"}]
            deprecated = [{"pattern": r"sql server 2019", "name": "SQL Server 2019"}]

        raw = with_value(minimal(), "infrastructure.existing_systems", ["On-prem SQL Server 2019", "Tally"])
        d = derive(validate(raw), FakeKB())
        self.assertEqual(len(d.existing), 1)
        self.assertEqual(d.existing[0]["input"], "On-prem SQL Server 2019")
        self.assertEqual(len(d.existing[0]["deprecated"]), 1)

    def test_derive_is_repeatable(self):
        a, b = self.d(helpers.public("P3")), self.d(helpers.public("P3"))
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
