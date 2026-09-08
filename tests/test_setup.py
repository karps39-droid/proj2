"""Uzstādīšanas un diagnostikas testi (``doctor``, ``mcp-install``)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

from periodika.cli import main
from periodika.config import Config
from periodika.doctor import format_report, run_diagnostics, self_test
from periodika.setup_mcp import (
    SERVER_NAME,
    claude_cli_command,
    config_targets,
    install_into,
    server_entry,
)


class TestSelfTest(unittest.TestCase):
    def setUp(self):
        self.report = self_test()

    def test_every_step_passes_offline(self):
        failed = [c.name for c in self.report.checks if not c.ok]
        self.assertEqual(failed, [], f"neizdevās: {failed}")

    def test_covers_the_whole_pipeline(self):
        names = " ".join(c.name for c in self.report.checks)
        for expected in ("ALTO", "METS", "ortogrāfijas", "Meklēšana", "Datumi", "vietvārdi"):
            self.assertIn(expected, names)

    def test_report_serialises(self):
        payload = self.report.to_json()
        self.assertEqual(payload["kritiskas_kļūdas"], 0)
        self.assertEqual(payload["kopsavilkums"], "viss kārtībā")


class TestDiagnostics(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        cfg = Config()
        cfg.data_dir = self.tmp / "dati"
        self.report = run_diagnostics(cfg, check_network=False)

    def test_no_critical_failures_in_a_working_environment(self):
        self.assertEqual([c.name for c in self.report.critical_failures], [])

    def test_optional_dependencies_are_reported_not_fatal(self):
        names = {c.name for c in self.report.checks}
        self.assertTrue(any("Pillow" in n for n in names))
        self.assertTrue(any("tesseract" in n for n in names))
        for check in self.report.checks:
            if "Pillow" in check.name or "tesseract" in check.name:
                self.assertFalse(check.critical)

    def test_missing_dependencies_carry_an_actionable_hint(self):
        for check in self.report.checks:
            if not check.ok:
                self.assertTrue(check.hint, f"{check.name} bez norādes, ko darīt")

    def test_data_dir_is_created(self):
        self.assertTrue((self.tmp / "dati").exists())

    def test_report_is_human_readable(self):
        text = format_report(self.report, self_test())
        self.assertIn("periodika doctor", text)
        self.assertIn("Pašpārbaude", text)


class TestMcpInstall(unittest.TestCase):
    def test_entry_points_at_this_interpreter(self):
        entry = server_entry()
        self.assertEqual(entry["command"], sys.executable)
        self.assertEqual(entry["args"], ["-m", "periodika.mcp_server"])

    def test_entry_carries_env_when_asked(self):
        entry = server_entry(data_dir="~/periodika-dati")
        self.assertIn("PERIODIKA_HOME", entry["env"])
        self.assertTrue(Path(entry["env"]["PERIODIKA_HOME"]).is_absolute())

    def test_cli_command_is_quoted(self):
        command = claude_cli_command()
        self.assertTrue(command.startswith("claude mcp add periodika"))
        self.assertIn("periodika.mcp_server", command)

    def test_targets_exist_for_this_platform(self):
        targets = config_targets()
        self.assertIn("claude-desktop", targets)
        for path in targets.values():
            self.assertTrue(path.is_absolute())

    def test_dry_run_does_not_touch_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.json"
            result = install_into(target, write=False)
        self.assertFalse(result["rakstīts"])
        self.assertFalse(target.exists())

    def test_write_merges_and_backs_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.json"
            target.write_text(
                json.dumps({"mcpServers": {"cits": {"command": "x"}}, "cita_sadaļa": 1}),
                encoding="utf-8",
            )
            result = install_into(target, write=True)
            data = json.loads(target.read_text(encoding="utf-8"))
            self.assertTrue(result["rakstīts"])
            self.assertTrue(Path(result["dublējums"]).exists())
        # esošie iestatījumi paliek neskarti
        self.assertIn("cits", data["mcpServers"])
        self.assertEqual(data["cita_sadaļa"], 1)
        self.assertIn(SERVER_NAME, data["mcpServers"])

    def test_broken_json_is_reported_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.json"
            target.write_text("{ tas nav json", encoding="utf-8")
            result = install_into(target, write=True)
            self.assertIn("kļūda", result)
            self.assertEqual(target.read_text(encoding="utf-8"), "{ tas nav json")


class TestCliEntryPoints(unittest.TestCase):
    def test_doctor_exits_zero_when_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = main(["doctor", "--offline", "--json", "--data-dir", tmp])
        self.assertEqual(code, 0)

    def test_mcp_install_dry_run_exits_zero(self):
        self.assertEqual(main(["mcp-install", "--print-only"]), 0)

    def test_unknown_command_fails_cleanly(self):
        with self.assertRaises(SystemExit):
            main(["nav-tadas-komandas"])


if __name__ == "__main__":
    unittest.main()
