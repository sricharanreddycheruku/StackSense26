import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from stacksense import cli

from tests.helpers import EDGE_DIR, PUBLIC_DIR, public


def run_cli(*args, cwd=None):
    buf = io.StringIO()
    old = os.getcwd()
    if cwd:
        os.chdir(cwd)
    try:
        with redirect_stdout(buf):
            code = cli.main(list(args))
    finally:
        os.chdir(old)
    return code, buf.getvalue()


def read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class Cli(unittest.TestCase):
    def test_single_file_default_output_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, text = run_cli("run", os.path.join(PUBLIC_DIR, "P1.json"), cwd=tmp)
            self.assertEqual(code, 0)
            out = read(os.path.join(tmp, "out", "P1_output.json"))
            self.assertEqual(out["scenario_id"], "P1")
            self.assertIn("P1 ->", text)

    def test_directory_runs_every_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _ = run_cli("run", PUBLIC_DIR, "--out-dir", tmp)
            self.assertEqual(code, 0)
            self.assertEqual(sorted(os.listdir(tmp)), [f"P{i}_output.json" for i in range(1, 7)])

    def test_combined_array_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            combined = os.path.join(tmp, "all.json")
            with open(combined, "w", encoding="utf-8") as fh:
                json.dump([public("P1"), public("P5")], fh)
            code, _ = run_cli("run", combined, "--out-dir", os.path.join(tmp, "o"))
            self.assertEqual(code, 0)
            self.assertEqual(sorted(os.listdir(os.path.join(tmp, "o"))), ["P1_output.json", "P5_output.json"])

    def test_malformed_file_still_writes_invalid_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _ = run_cli("run", os.path.join(EDGE_DIR, "E04b_malformed.json"), "-o", os.path.join(tmp, "x.json"))
            self.assertEqual(code, 0)
            out = read(os.path.join(tmp, "x.json"))
            self.assertEqual(out["status"], "invalid_input")
            self.assertIsNone(out["stack"])

    def test_stdout_and_byte_stability(self):
        _, a = run_cli("run", os.path.join(PUBLIC_DIR, "P3.json"), "--stdout")
        _, b = run_cli("run", os.path.join(PUBLIC_DIR, "P3.json"), "--stdout", "--llm", "on")
        self.assertEqual(json.loads(a)["stack"], json.loads(b)["stack"])
        self.assertTrue(a.endswith("}\n"))

    def test_validate_and_describe(self):
        code, text = run_cli("validate", os.path.join(EDGE_DIR, "E02_missing_required.json"))
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(text)["valid"])
        code, text = run_cli("describe", "farmers get paid and chat")
        self.assertTrue(any(s["field"] == "requirements.payments" for s in json.loads(text)))

    def test_usage_errors_exit_one(self):
        self.assertEqual(run_cli()[0], 1)
        self.assertEqual(run_cli("run", os.path.join(tempfile.gettempdir(), "no_such_dir_zz"))[0], 1)


if __name__ == "__main__":
    unittest.main()
