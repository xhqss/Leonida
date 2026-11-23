"""CPU-only unit tests for tools/buffer_store_probe.py (SHADER-364)."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(REPO, "tools")
PROBE = os.path.join(TOOLS, "buffer_store_probe.py")

sys.dont_write_bytecode = True
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import buffer_store_probe as probe  # noqa: E402
from hw_oracle import asm as hw_asm, runner as hw_runner  # noqa: E402


class BufferStoreProbeTests(unittest.TestCase):
    def test_self_test(self):
        proc = subprocess.run([sys.executable, "-I", PROBE, "--self-test"],
                              capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        self.assertIn("self-test OK", proc.stdout)

    def test_non_gfx10_or_missing_oracle_exits_77(self):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("LEONIDA_HW_") and k != "LEONIDA_AMD_CLANG"}
        env["LEONIDA_AMD_CLANG"] = os.path.join(REPO, "does_not_exist_clang")
        proc = subprocess.run([sys.executable, "-I", PROBE], env=env,
                              capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 77, proc.stderr or proc.stdout)

    def test_invalid_waves_rejected_before_open_oracle(self):
        for bad in ("", "   ", "abc", "32,foo", "16", "32,128"):
            with self.subTest(waves=bad):
                err = io.StringIO()
                with unittest.mock.patch.object(hw_runner, "open_oracle",
                                                side_effect=AssertionError("open_oracle called")), \
                     redirect_stderr(err):
                    rc = probe.main(["--waves", bad])
                self.assertEqual(rc, 2)
                self.assertIn("error:", err.getvalue())

    def test_invalid_json_rejected_before_open_oracle(self):
        with tempfile.TemporaryDirectory() as tmp:
            file_ancestor = os.path.join(tmp, "not_a_dir")
            with open(file_ancestor, "w", encoding="utf-8") as f:
                f.write("x")
            bad_paths = [
                "",
                "   ",
                "bad\x00.json",
                tmp,
                os.path.join(tmp, "sub") + os.sep,
                os.path.join(file_ancestor, "out.json"),
                os.path.join(file_ancestor, "nested", "out.json"),
            ]
            for bad in bad_paths:
                with self.subTest(json_path=bad):
                    err = io.StringIO()
                    with unittest.mock.patch.object(hw_runner, "open_oracle",
                                                    side_effect=AssertionError("open_oracle called")), \
                         redirect_stderr(err):
                        rc = probe.main(["--json", bad])
                    self.assertEqual(rc, 2)
                    self.assertIn("error:", err.getvalue())

    def test_valid_nested_json_does_not_create_dirs_on_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = os.path.join(tmp, "a", "b", "out.json")
            err = io.StringIO()
            with unittest.mock.patch.object(hw_runner, "open_oracle",
                                            side_effect=hw_runner.Unavailable("no GPU")), \
                 redirect_stderr(err):
                rc = probe.main(["--json", nested])
            self.assertEqual(rc, 77)
            self.assertFalse(os.path.exists(os.path.join(tmp, "a")))

    def test_write_json_report_creates_dirs_and_uses_lf(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = os.path.join(tmp, "sub", "dir", "report.json")
            err = probe.write_json_report(nested, {"arch": "gfx1035", "waves": {"32": {}}})
            self.assertIsNone(err)
            with open(nested, "rb") as f:
                raw = f.read()
            self.assertNotIn(b"\r\n", raw)
            self.assertEqual(json.loads(raw.decode("utf-8"))["arch"], "gfx1035")

    def test_write_json_oserror_returns_1_without_traceback(self):
        fake_oracle = unittest.mock.MagicMock(arch="gfx1035")
        fake_norm = {name: {"inst": {"exact": [0], "inputs": [0]}} for name, *_ in probe.NORM_FORMATS}
        fake_norm["snorm2_ties"] = {"exact": [0], "inputs": [0]}
        fake_two = {"formats_ok": 12, "formats_total": 12, "triples_ok": 216, "triples_total": 216}
        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, "report.json")
            out, err = io.StringIO(), io.StringIO()
            with unittest.mock.patch.object(hw_runner, "open_oracle", return_value=fake_oracle), \
                 unittest.mock.patch.object(hw_asm, "family", return_value="gfx10"), \
                 unittest.mock.patch.object(probe, "probe_norm", return_value=fake_norm), \
                 unittest.mock.patch.object(probe, "probe_two_comp", return_value=fake_two), \
                 unittest.mock.patch("builtins.open", side_effect=OSError("disk full")), \
                 redirect_stdout(out), redirect_stderr(err):
                rc = probe.main(["--waves", "32", "--json", out_path])
            self.assertEqual(rc, 1)
            self.assertIn("error:", err.getvalue())
            self.assertIn("disk full", err.getvalue())
            self.assertNotIn("Traceback", err.getvalue())


if __name__ == "__main__":
    unittest.main()
