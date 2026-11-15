import unittest

from stacksense import engine

from tests.helpers import minimal, with_value


def base():
    b = minimal()
    b["requirements"] = {"auth": "email_password"}
    return b


def parts(out):
    st = out["stack"]
    return {
        "stack": [(k, st[k]["choice"]) for k in ("frontend", "backend", "database", "hosting")]
                 + [(s["layer"], s["choice"]) for s in st["supporting"]],
        "cost": out["estimated_monthly_cost_usd"]["min"],
        "style": out["architecture_style"],
        "region": out["region"],
        "warnings": sorted(w["code"] for w in out["warnings"]),
    }


CASES = [
    ("scale.peak_rps", 5000, "stack"),
    ("scale.user_geography", ["eu"], "region"),
    ("team.size_in_12_months", 20, "style"),
    ("infrastructure.vendor_lock_in_tolerance", "low", "stack"),
    ("infrastructure.existing_licences", ["microsoft_365"], "stack"),
    ("requirements.data_sensitivity", "health", "cost"),
    ("requirements.consistency", "strong", "stack", {"project.type": "gaming"}),
    ("requirements.background_jobs", True, "stack", {"requirements.background_jobs": False}),
    ("requirements.notifications", ["sms", "push"], "stack"),
    ("requirements.rpo_minutes", 5, "region"),
]


class ExtraInputs(unittest.TestCase):
    def test_each_extra_changes_a_named_part(self):
        for path, value, part, *pre in CASES:
            with self.subTest(path):
                start = base()
                for k, v in (pre[0] if pre else {}).items():
                    start = with_value(start, k, v)
                ref = parts(engine.run(start))
                got = parts(engine.run(with_value(start, path, value)))
                self.assertNotEqual(ref[part], got[part])

    def test_connectivity_and_device_ram_are_recorded(self):
        for path, value in (("requirements.connectivity", "poor"), ("requirements.min_device_ram_gb", 1)):
            out = engine.run(with_value(base(), path, value))
            self.assertIn(out["status"], ("ok", "ok_with_warnings"))
            self.assertFalse(any(path in a and "not provided" in a for a in out["assumptions"]))

    def test_absent_extras_are_listed_as_assumptions(self):
        out = engine.run(base())
        for path in ("requirements.rpo_minutes", "requirements.connectivity", "scale.user_geography"):
            self.assertTrue(any(path in a for a in out["assumptions"]), path)


if __name__ == "__main__":
    unittest.main()
