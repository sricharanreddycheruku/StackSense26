"""Compliance-process, on-call and device-condition warnings."""
import unittest

from stacksense import engine

from tests.helpers import minimal, with_value


def run(*changes):
    spec = minimal()
    for path, value in changes:
        spec = with_value(spec, path, value)
    return engine.run(spec)


def codes(out):
    return {w["code"] for w in out["warnings"]}


class ProcessWarnings(unittest.TestCase):
    def test_payments_without_pci_dss_get_a_scope_note(self):
        self.assertIn("PCI_SCOPE", codes(run(("requirements.payments", True))))
        self.assertNotIn("PCI_SCOPE", codes(run(("requirements.payments", True), ("compliance", ["pci_dss"]))))
        self.assertNotIn("PCI_SCOPE", codes(run()))

    def test_certification_is_described_as_an_organisational_process(self):
        out = run(("compliance", ["iso27001"]))
        msg = next(w["message"] for w in out["warnings"] if w["code"] == "CERTIFICATION_IS_PROCESS")
        self.assertIn("ISO 27001", msg)
        self.assertNotIn("CERTIFICATION_IS_PROCESS", codes(run(("compliance", ["gdpr"]))))

    def test_india_residency_gets_cert_in_log_retention(self):
        self.assertIn("CERT_IN_LOGS", codes(run(("infrastructure.data_residency", "india"))))
        self.assertNotIn("CERT_IN_LOGS", codes(run(("infrastructure.data_residency", "eu"))))

    def test_high_availability_with_a_small_team_needs_on_call(self):
        out = run(("requirements.availability", "99.95"), ("budget.monthly_infra_usd", 5000))
        self.assertIn("ON_CALL_GAP", codes(out))
        big = run(("requirements.availability", "99.95"), ("budget.monthly_infra_usd", 5000),
                  ("team.size", 10), ("team.has_devops", True))
        self.assertNotIn("ON_CALL_GAP", codes(big))

    def test_device_and_network_conditions(self):
        mobile = ("project.platforms", ["android"])
        self.assertIn("LOW_END_DEVICE", codes(run(mobile, ("requirements.min_device_ram_gb", 2))))
        self.assertNotIn("LOW_END_DEVICE", codes(run(mobile, ("requirements.min_device_ram_gb", 6))))
        self.assertIn("LOW_BANDWIDTH_MEDIA", codes(run(mobile, ("requirements.connectivity", "poor"))))

    def test_location_data_under_privacy_law(self):
        out = run(("project.description", "Field agents log GPS location of each visit"), ("compliance", ["dpdp"]))
        self.assertIn("LOCATION_DATA", codes(out))


if __name__ == "__main__":
    unittest.main()
