"""Membership guard for retired netconsole logs; no host access required."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


_spec = importlib.util.spec_from_file_location(
    "hw_error_scan", Path(__file__).with_name("neuravps-hw-error-scan.py"))
scan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scan)


class LiveMembershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.logs = self.root / "logs"
        self.logs.mkdir()
        self.nodes = self.root / "nodes.json"

    def tearDown(self):
        self.temp.cleanup()

    def _log(self, ip):
        (self.logs / f"{ip}.log").write_text(
            "[0.1] netconsole: network logging started\n"
            "[1.0] mce: [Hardware Error]: Machine check events logged\n")

    def test_valid_inventory_skips_retired_log_but_keeps_live_error(self):
        self._log("2001:db8::1")
        self._log("2001:db8::2")
        self.nodes.write_text(json.dumps({"0000001-live": "2001:db8::1"}))

        report = scan.build_report(str(self.logs), str(self.nodes))

        self.assertEqual(["0000001-live"], [entry["node"] for entry in report])

    def test_empty_inventory_scans_all_logs_conservatively(self):
        self._log("2001:db8::1")
        self.nodes.write_text("{}")

        report = scan.build_report(str(self.logs), str(self.nodes))

        self.assertEqual(["2001:db8::1"], [entry["node"] for entry in report])

    def test_corrupt_inventory_scans_live_error_conservatively(self):
        self._log("2001:db8::1")
        self.nodes.write_text("{not json")

        report = scan.build_report(str(self.logs), str(self.nodes))

        self.assertEqual(["2001:db8::1"], [entry["node"] for entry in report])

    def test_partially_malformed_inventory_scans_all_logs(self):
        self._log("2001:db8::1")
        self._log("2001:db8::2")
        self.nodes.write_text(json.dumps({
            "0000001-live": "2001:db8::1",
            "0000002-invalid": "not-an-ip",
        }))

        report = scan.build_report(str(self.logs), str(self.nodes))

        self.assertEqual(["2001:db8::1", "2001:db8::2"],
                         [entry["node"] for entry in report])


if __name__ == "__main__":
    unittest.main()
