"""tools/xcl_compare.py: parsing of the test title's lines and the report it builds.

    python -I tests/test_xcl_compare.py

Nothing here starts XCL or gta6rt, and no game files are read. The run tests start a small Python script that
prints the test title's line format in place of a title.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import xcl_compare as xc  # noqa: E402


class ParseResult(unittest.TestCase):
    KNOWN = ["kernelx: GetConsoleType / XSystemGetDeviceType", "files: T:\\ title scratch storage", "argv"]

    def test_pass(self):
        self.assertEqual(xc.parse_result("[PASS] argv", None, self.KNOWN), ("PASS", "argv", ""))

    def test_fail_with_colon_in_name_uses_the_running_check(self):
        name = "kernelx: GetConsoleType / XSystemGetDeviceType"
        got = xc.parse_result("[FAIL] " + name + ": GetConsoleType = 0", name, self.KNOWN)
        self.assertEqual(got, ("FAIL", name, "GetConsoleType = 0"))

    def test_fail_with_colon_in_name_uses_known_names(self):
        name = "kernelx: GetConsoleType / XSystemGetDeviceType"
        got = xc.parse_result("[FAIL] " + name + ": GetConsoleType = 0", None, self.KNOWN)
        self.assertEqual(got, ("FAIL", name, "GetConsoleType = 0"))

    def test_fail_unknown_name_splits_at_first_colon(self):
        self.assertEqual(xc.parse_result("[FAIL] new check: why", None, []), ("FAIL", "new check", "why"))

    def test_skip(self):
        self.assertEqual(xc.parse_result("[SKIP] argv", None, self.KNOWN), ("SKIP", "argv", ""))

    def test_other_lines(self):
        for line in ("[RUN] argv", "[rt] unimplemented: kernelx.dll!GetConsoleType", "xcl_testtitle: start", ""):
            self.assertIsNone(xc.parse_result(line, None, self.KNOWN))


class Report(unittest.TestCase):
    def runs(self):
        x, l = xc.Run("XCL"), xc.Run("Leonida")
        for n in ("a", "b", "c"):
            x.set(n, status="PASS")
        l.set("a", status="PASS")
        l.set("b", status="FAIL", reason="null", unimplemented=["kernelx.dll!XMemAlloc"])
        l.set("c", status="HANG", reason="no output for 15 s", stack=["main: XTaskQueueTerminate"])
        l.set("d", status="FAIL", reason="only here")
        return x, l

    def test_rows_follow_xcl_order_then_extra_checks(self):
        x, l = self.runs()
        r = xc.make_report(x, l, list(x.order), "gta6rt.exe", ".")
        self.assertEqual([row["check"] for row in r["checks"]], ["a", "b", "c", "d"])

    def test_counts_and_differences(self):
        x, l = self.runs()
        r = xc.make_report(x, l, list(x.order), "gta6rt.exe", ".")
        self.assertEqual(r["summary"]["xcl"]["PASS"], 3)
        self.assertEqual(r["summary"]["xcl"]["NOT RUN"], 1)
        self.assertEqual(r["summary"]["leonida"], {"PASS": 1, "FAIL": 2, "SKIP": 0, "HANG": 1, "NOT RUN": 0})
        self.assertEqual(r["differences"], 3)
        b = r["checks"][1]
        self.assertEqual((b["leonida"], b["unimplemented"]), ("FAIL", ["kernelx.dll!XMemAlloc"]))
        self.assertEqual(r["checks"][2]["stack"], ["main: XTaskQueueTerminate"])

    def test_same_status_different_reason_differs(self):
        x, l = xc.Run("XCL"), xc.Run("Leonida")
        x.set("a", status="FAIL", reason="E_NOTIMPL")
        l.set("a", status="FAIL", reason="null pointer")
        x.set("b", status="FAIL", reason="same")
        l.set("b", status="FAIL", reason="same")
        r = xc.make_report(x, l, list(x.order), "gta6rt.exe", ".")
        self.assertEqual([row["same"] for row in r["checks"]], [False, True])
        self.assertEqual(r["differences"], 1)


class Transcript(unittest.TestCase):
    """run_title streams the title's output to the transcript file and keeps none of it on the Run."""

    def test_output_goes_to_the_file(self):
        class Sink:
            def put(self, event):
                pass

        script = "print('[RUN] a'); print('x' * 256); print('[PASS] a')"
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.txt")
            run = xc.Run("synthetic")
            hung = xc.run_title([sys.executable, "-I", "-c", script], dict(os.environ), tmp, run, ["a"], 15, None,
                                Sink(), [], path)
            with open(path, encoding="utf-8") as f:
                lines = f.read().split("\n")
        self.assertIsNone(hung)
        self.assertEqual(run.checks["a"]["status"], "PASS")
        self.assertTrue(lines[0].startswith("--- "))
        self.assertEqual(lines[1:4], ["[RUN] a", "x" * 256, "[PASS] a"])
        self.assertFalse(hasattr(run, "log"))


class Sink:
    def put(self, event):
        pass


# A stand-in title: checks a, b and c; b hangs unless XCL_TESTTITLE_SKIP names it (or HONOUR_SKIP is 0).
FAKE_TITLE = """
import os, sys, time
skip = os.environ.get("XCL_TESTTITLE_SKIP", "").split(";")
honour = os.environ.get("HONOUR_SKIP", "1") == "1"
for name in ("a", "b", "c"):
    if honour and name in skip:
        print("[SKIP] " + name, flush=True)
        continue
    print("[RUN] " + name, flush=True)
    if name == "b":
        time.sleep(60)
    print("[PASS] " + name, flush=True)
"""


class Restart(unittest.TestCase):
    """run_with_restarts: a hung check is recorded as HANG and the title is run again with it skipped."""

    def run_fake(self, honour):
        env = dict(os.environ, HONOUR_SKIP="1" if honour else "0")
        cmd = [sys.executable, "-I", "-c", FAKE_TITLE]
        attempts = []

        def cmd_for(attempt):
            attempts.append(attempt)
            return cmd

        run = xc.Run("synthetic")
        with tempfile.TemporaryDirectory() as tmp:
            skip = xc.run_with_restarts(cmd_for, env, tmp, run, ["a", "b", "c"], 2, None, Sink(), [], 3,
                                        os.path.join(tmp, "out.txt"))
        return run, skip, attempts

    def test_hang_then_restart_with_the_check_skipped(self):
        run, skip, attempts = self.run_fake(honour=True)
        self.assertEqual(skip, ["b"])
        self.assertEqual(attempts, [0, 1])
        self.assertEqual(run.checks["b"]["status"], "HANG")
        self.assertEqual(run.checks["b"]["reason"], "no output for 2 s")
        self.assertEqual((run.checks["a"]["status"], run.checks["c"]["status"]), ("PASS", "PASS"))

    def test_stops_when_a_skipped_check_hangs_again(self):
        run, skip, attempts = self.run_fake(honour=False)
        self.assertEqual(skip, ["b"])
        self.assertEqual(attempts, [0, 1])
        self.assertEqual(run.checks["b"]["status"], "HANG")
        self.assertNotIn("c", run.checks)  # run_all marks it NOT RUN


if __name__ == "__main__":
    unittest.main()
