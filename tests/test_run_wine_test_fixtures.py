"""Check how the Wine runner handles CTest fixture metadata with benign child scripts."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

RUNNER = Path(__file__).resolve().parents[1] / "scripts" / "run_wine_tests.py"
WINDOWS_ROOT = "synthetic-checkout"


class WineFixtureSetupTests(unittest.TestCase):
    def run_fixture(self, setup_status: int, *, fixture: bool = True, disabled: bool = False
                    ) -> tuple[int, str, list[str]]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trace = root / "launches.txt"

            def entry(name: str, status: int, properties: dict[str, object]) -> dict[str, object]:
                (root / f"{name}.py").write_text(
                    "from pathlib import Path\n"
                    "with Path(__file__).with_name('launches.txt').open('a', encoding='utf-8', newline='\\n') as f:\n"
                    f"    f.write({name!r} + '\\n')\n"
                    f"raise SystemExit({status})\n",
                    encoding="utf-8", newline="\n",
                )
                return {"name": name, "command": [f"{WINDOWS_ROOT}/{name}.py"],
                        "properties": [{"name": key, "value": value} for key, value in properties.items()]}

            setup_props: dict[str, object] = {"SKIP_RETURN_CODE": 77, "DISABLED": disabled}
            consumer_props: dict[str, object] = {"DEPENDS": ["setup"]}
            if fixture:
                setup_props["FIXTURES_SETUP"] = ["input"]
                consumer_props["FIXTURES_REQUIRED"] = ["input"]
            setup = entry("setup", setup_status, setup_props)
            consumer = entry("consumer", 0, consumer_props)
            tests = [consumer, setup] if fixture else [setup, consumer]
            listing = root / "ctest.json"
            listing.write_text(json.dumps({"tests": tests}), encoding="utf-8", newline="\n")
            result = subprocess.run(
                [sys.executable, "-I", str(RUNNER), "--tests", str(listing), "--windows-root", WINDOWS_ROOT,
                 "--root", directory, "--require", "consumer", "--wine", f'"{Path(sys.executable).as_posix()}"'],
                capture_output=True, text=True, timeout=15,
            )
            return result.returncode, result.stdout + result.stderr, trace.read_text(encoding="utf-8").splitlines()

    def test_fixture_setup_is_run_before_an_earlier_listed_consumer(self) -> None:
        status, output, calls = self.run_fixture(0)
        self.assertEqual(status, 0, output)
        self.assertEqual(calls, ["setup", "consumer"])

    def test_failed_setup_blocks_a_required_consumer(self) -> None:
        status, output, calls = self.run_fixture(3)
        self.assertEqual(status, 1, output)
        self.assertEqual(calls, ["setup"])
        self.assertIn("[FAIL] consumer", output)
        self.assertIn("failed fixture setup: setup", output)

    def test_skipped_or_disabled_setup_does_not_block(self) -> None:
        for status, disabled, expected in ((77, False, ["setup", "consumer"]), (3, True, ["consumer"])):
            with self.subTest(disabled=disabled):
                code, output, calls = self.run_fixture(status, disabled=disabled)
                self.assertEqual(code, 0, output)
                self.assertEqual(calls, expected)

    def test_generic_depends_does_not_turn_an_optional_failure_into_a_block(self) -> None:
        status, output, calls = self.run_fixture(3, fixture=False)
        self.assertEqual(status, 0, output)
        self.assertEqual(calls, ["setup", "consumer"])
        self.assertIn("[FAIL] setup", output)
        self.assertIn("[PASS] consumer", output)


if __name__ == "__main__":
    unittest.main()
