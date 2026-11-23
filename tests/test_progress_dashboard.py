"""tools/progress_dashboard.py: parsers, classification, the no-game-data rule and the output-folder rule.

    python -I tests/test_progress_dashboard.py

Every input is written here by hand (Markdown tables, JUnit XML, a CTest log, a fake work folder whose shader names
are sentinels). No game files, no GPU, no network: the GitHub path is exercised through its parsers only.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import progress_dashboard as pd  # noqa: E402

TOOL = os.path.join(ROOT, "tools", "progress_dashboard.py")
SENTINEL = "SENTINEL_SHADER_NAME_must_not_appear"


class Markdown(unittest.TestCase):
    def test_tables_keep_pipes_inside_code(self):
        t = pd.md_tables("## Translator status and runtime contract\n\n| Piece | Contract |\n|---|---|\n| A | `x|y` is **GPU-checked** |\n")
        self.assertEqual(len(t), 1)
        heading, header, rows, line = t[0]
        self.assertEqual(heading, "Translator status and runtime contract")
        self.assertEqual(rows[0][1], "`x|y` is **GPU-checked**")
        self.assertEqual(line, 3)

    def test_plain_strips_links_and_emphasis(self):
        self.assertEqual(pd.plain("**GPU-checked** ([measurement](http://x)) `v_mac_f32`"), "GPU-checked (measurement) v_mac_f32")


class Contract(unittest.TestCase):
    def test_classification_order_and_evidence(self):
        cases = {
            "**not yet GPU-verified**; Source: LLVM `SIInstrInfo.cpp`": ("not_gpu", "partial"),
            "**GPU-checked** (TEST-298) on an NVIDIA GeForce RTX 2060": ("gpu", "verified"),
            "Source: LLVM `VOPProfileI2F`": ("reference", "implemented"),
            "a count over the corpus: 0 of the 26,503 shaders": ("corpus", "implemented"),
            "CPU-checked by DenormalFlushTests": ("cpu", "implemented"),
            "The translator emits OpFAdd.": ("unstated", "unknown"),
        }
        for text, (kind, status) in cases.items():
            with self.subTest(text=text):
                k, s, words = pd.classify_contract(text)
                self.assertEqual((k, s), (kind, status))
                if kind != "unstated":
                    self.assertIn(words.lower(), text.lower())

    def test_open_items_are_detected_only_by_explicit_words(self):
        self.assertTrue(pd.OPEN.search("Still open: on some inputs the result is undefined"))
        self.assertFalse(pd.OPEN.search("s_getreg_b32 is unsupported and there are no traps"))


class Tests(unittest.TestCase):
    def test_junit_ctest_and_pytest_flavours(self):
        ctest = '<testsuite><testcase name="loader" classname="loader" status="run" time="0.9"/>' \
                '<testcase name="xmem" classname="xmem" status="fail"><failure message="C:\\Users\\me\\x\\out.txt bad"/></testcase></testsuite>'
        rows = pd.parse_junit(ctest)
        self.assertEqual([(n, r) for n, r, _, _ in rows], [("loader", "pass"), ("xmem", "fail")])
        self.assertNotIn("me", rows[1][3])          # paths are reduced to their last component
        pytest = '<testsuites><testsuite><testcase classname="tests.a" name="t[1-2]"/>' \
                 '<testcase classname="tests.a" name="t[1_2]"><skipped/></testcase></testsuite></testsuites>'
        self.assertEqual([(n, r) for n, r, _, _ in pd.parse_junit(pytest)], [("tests.a::t[1-2]", "pass"), ("tests.a::t[1_2]", "skip")])

    def test_lasttest_log(self):
        log = "Start testing: Nov 22 10:00 X\n1/2 Test: loader\nTest time =   0.91 sec\nTest Passed.\n2/2 Test: xmem\nTest Failed.\n"
        self.assertEqual(pd.parse_lasttest(log), [("loader", "pass", 0.91), ("xmem", "fail", None)])

    def test_ci_selection_matches_the_workflow_rule(self):
        files = pd.ci_test_files()
        self.assertIn("tests/test_progress_dashboard.py", files)
        self.assertFalse(any(f.startswith("tests/semantic/") for f in files))
        for f in files[:20]:
            with open(os.path.join(ROOT, f), encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn("__main__", text)
            self.assertIsNone(pd.PYTEST_MARKERS.search(text))


class Freshness(unittest.TestCase):
    def collect(self, results):
        with tempfile.TemporaryDirectory() as out:
            with open(os.path.join(out, "unit_results.json"), "w", encoding="utf-8", newline="\n") as f:
                json.dump(results, f)
            col = pd.Collector()
            pd.collect_unit(col, out, type("A", (), {"run_tests": False})())
            return {c["name"]: (c["status"], c["freshness"]) for c in col.components}

    def base(self, digest, code):
        return {"ran_at": "2026-11-22T10:00:00Z", "commit": None, "code_digest": code,
                "results": [{"test": "tests/test_progress_dashboard.py", "result": "pass", "digest": digest}]}

    def test_unchanged_test_and_code_is_current(self):
        got = self.collect(self.base(pd.file_digest("tests/test_progress_dashboard.py"), pd.code_digest()))
        self.assertEqual(got["test_progress_dashboard.py"], ("verified", "current"))

    def test_changed_test_file_needs_revalidation(self):
        got = self.collect(self.base("0" * 16, pd.code_digest()))
        self.assertEqual(got["test_progress_dashboard.py"], ("partial", "revalidation_required"))

    def test_changed_tools_need_revalidation(self):
        got = self.collect(self.base(pd.file_digest("tests/test_progress_dashboard.py"), "0" * 16))
        self.assertEqual(got["test_progress_dashboard.py"], ("partial", "revalidation_required"))

    def test_code_edited_during_the_run_is_never_current(self):
        got = self.collect(self.base(pd.file_digest("tests/test_progress_dashboard.py"), None))
        self.assertEqual(got["test_progress_dashboard.py"], ("verified", "unknown"))


class GpuLog(unittest.TestCase):
    LOG = "\n".join([
        "using runtime/build/RelWithDebInfo",
        "cs_bounds            ok",
        "ps_texoff            SKIP (VK_KHR_maintenance8 missing)",
        "cs_bpermute32        lane 3: got 0x00000000, want 0x00000007",
        "2 of 3 pass, 1 skipped",
    ])

    def test_lines_and_summary(self):
        rows, summary = pd.parse_gpu_log(self.LOG)
        self.assertEqual([(n, r) for n, r, _ in rows], [("cs_bounds", "pass"), ("ps_texoff", "skip"), ("cs_bpermute32", "fail")])
        self.assertIn("want 0x00000007", rows[2][2])
        self.assertEqual(summary, (2, 3, 1))

    def test_panel_groups_by_gpu_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "check.txt")
            with open(log, "w", encoding="utf-8", newline="\n") as f:
                f.write(self.LOG + "\n")
            col = pd.Collector()
            pd.collect_gpu(col, [log + "=RTX 4060, 576.02, Windows 11"])
            self.assertEqual({c["group"] for c in col.components}, {"RTX 4060, 576.02, Windows 11"})
            self.assertEqual(sorted(c["status"] for c in col.components)[0], "failed")
            m = next(m for m in col.metrics if m["id"] == "gpu.current")
            self.assertEqual(m["denominator"], 2)                     # the skip is not counted


class Github(unittest.TestCase):
    def test_d3d_states(self):
        self.assertEqual(pd.d3d_state("@a", "#291 merged, #819, #831 on main", "")[0], "partial")
        self.assertEqual(pd.d3d_state("@a", "#1489 merged, #907 closed", "")[0], "implemented")
        self.assertEqual(pd.d3d_state("@a", "—", "")[0], "unknown")
        self.assertEqual(pd.d3d_state("—", "—", "")[0], "missing")

    def test_d3d_states_follow_the_merged_list(self):
        merged, open_prs = {819, 831, 291}, {900}
        # the table still lists #819 without "merged", but it merged: the item is implemented
        self.assertEqual(pd.d3d_state("@a", "#291 merged, #819", "PR open", merged, open_prs)[0], "implemented")
        self.assertEqual(pd.d3d_state("@a", "#819, #900", "", merged, open_prs)[0], "partial")
        # a bare reference that is neither merged nor open was closed without merging: it does not count
        self.assertEqual(pd.d3d_state("@a", "#777", "", merged, open_prs)[0], "unknown")
        self.assertEqual(pd.d3d_state("@a", "—", "PR open", merged, open_prs)[0], "partial")

    def test_item_ids(self):
        self.assertEqual(pd.item_ids("D3D-86: answer D3D12_OPTIONS1 in CheckFeatureSupport"), ["D3D-86"])
        self.assertEqual(pd.item_ids("pknorm_probe: NaN gives 0 (SHADER-339)"), ["SHADER-339"])
        self.assertEqual(pd.item_ids("Docs/Tests: SHADER-4 and SHADER-4 again, TEST-9"), ["SHADER-4", "TEST-9"])
        self.assertEqual(pd.item_ids("hash with SHA-256, write UTF-8, read OPTIONS2-1 and D3D12-3"), [])
        self.assertEqual(pd.item_ids(None), [])

    def test_merged_pull_requests_are_implemented(self):
        merged = [{"number": 10, "title": "D3D-1: a", "mergedAt": "2026-11-22T10:00:00Z", "author": {"login": "x"}},
                  {"number": 12, "title": "docs: b", "mergedAt": "2026-11-22T12:00:00Z", "author": {"login": "y"}}]
        open_prs = [{"number": 13, "title": "D3D-2: c", "isDraft": True, "author": {"login": "z"}}]
        col = pd.Collector()
        latest = pd.collect_pulls(col, "o/r", merged, open_prs)
        got = {c["number"]: (c["status"], c["group"]) for c in col.components}
        self.assertEqual(got, {10: ("implemented", "D3D items"), 12: ("implemented", "no item ID"), 13: ("partial", "D3D items")})
        self.assertEqual([p["number"] for p in latest], [12, 10])          # newest merge first
        m = {m["id"]: (m["numerator"], m["denominator"]) for m in col.metrics}
        self.assertEqual(m["merged.prs"], (2, 3))
        self.assertEqual(m["merged.items"], (1, 2))
        self.assertNotIn("merged_at", next(c for c in col.components if c["number"] == 13))

    def test_pull_request_limit_is_reported(self):
        col = pd.Collector()
        pd.collect_pulls(col, "o/r", [{"number": 1, "title": "a", "mergedAt": "x"}], [], limit=1)
        self.assertIn("limit was reached", col.sources[0]["note"])


class Corpus(unittest.TestCase):
    def test_counts_only_never_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = os.path.join(tmp, "work")
            sh = os.path.join(work, "shaders")      # make_game.py's layout
            os.makedirs(os.path.join(sh, "dxbc"))
            os.makedirs(os.path.join(sh, "spirv"))
            carved = [{"name": f"{SENTINEL}_{i}", "stage": "pixel" if i < 3 else "compute"} for i in range(5)]
            with open(os.path.join(sh, "dxbc", "index.json"), "w", encoding="utf-8", newline="\n") as f:
                json.dump(carved, f)
            with open(os.path.join(sh, "spirv", "index.json"), "w", encoding="utf-8", newline="\n") as f:
                json.dump({f"{SENTINEL}_0": {"stage": "pixel"}, f"{SENTINEL}_3": {"stage": "compute"},
                           f"{SENTINEL}_4": {"error": "x"}}, f)
            col = pd.Collector()
            pd.collect_corpus(col, work)
            got = {(c["group"], c["status"]): c["count"] for c in col.components}
            self.assertEqual(got, {("pixel", "implemented"): 1, ("pixel", "failed"): 2,
                                   ("compute", "implemented"): 1, ("compute", "failed"): 1})
            m = next(m for m in col.metrics if m["id"] == "corpus.validated")
            self.assertEqual((m["numerator"], m["denominator"]), (2, 5))
            # everything the collector holds can reach the page (notes and panels included), so check all of it
            held = json.dumps(vars(col), default=str)
            self.assertNotIn(SENTINEL, held)
            self.assertNotIn(tmp, held)
            self.assertNotIn(json.dumps(tmp)[1:-1], held)


class Scrub(unittest.TestCase):
    def test_paths_outside_the_repository_lose_their_file_name(self):
        sep = os.sep
        work = os.path.join(tempfile.gettempdir(), "work", "shaders", "dxbc", f"{SENTINEL}.fssm")
        self.assertEqual(pd.scrub(f"FAIL: cannot read {work}"), "FAIL: cannot read <path>")
        self.assertEqual(pd.scrub(f"see /home/someone/work/{SENTINEL}.fssm now"), "see <path> now")
        self.assertEqual(pd.scrub("in " + os.path.join(pd.HOME, "work", f"{SENTINEL}.fssm")), "in <path>")
        self.assertEqual(pd.scrub(f"C:{sep}work{sep}{SENTINEL}_a.fssm, C:{sep}{SENTINEL}_b.fssm"), "<path> <path>")

    def test_paths_inside_the_repository_become_relative(self):
        line = "Error: " + os.path.join(ROOT, "tools", "text_tables.py") + " line 3"
        self.assertEqual(pd.scrub(line), "Error: tools/text_tables.py line 3")

    def test_urls_and_relative_paths_stay(self):
        self.assertEqual(pd.scrub("see https://github.com/ItsNotRexed/Leonida/issues/1"),
                         "see https://github.com/ItsNotRexed/Leonida/issues/1")
        self.assertEqual(pd.scrub("tests/test_x.py failed"), "tests/test_x.py failed")


class Outputs(unittest.TestCase):
    def test_out_inside_the_repository_is_refused(self):
        r = subprocess.run([sys.executable, "-I", TOOL, "--out", os.path.join(ROOT, "progress_out_test")],
                           capture_output=True, cwd=ROOT, timeout=120)
        self.assertEqual(r.returncode, 2)
        self.assertFalse(os.path.exists(os.path.join(ROOT, "progress_out_test")))

    def test_page_snapshot_history_and_dedupe(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out")
            cmd = [sys.executable, "-I", TOOL, "--out", out]
            r1 = subprocess.run(cmd, capture_output=True, cwd=ROOT, timeout=300)
            self.assertEqual(r1.returncode, 0, r1.stderr.decode("utf-8", "replace"))
            with open(os.path.join(out, "progress.html"), encoding="utf-8") as f:
                page = f.read()
            self.assertNotIn("/*__PROGRESS_DATA__*/null", page)
            self.assertNotIn("/*__PROGRESS_LOGO__*/", page)
            if os.path.isfile(pd.LOGO):                              # the logo is embedded: the page needs no other file
                self.assertIn('src="data:image/png;base64,', page)
            self.assertIn('"schema": "leonida.progress/1"', page)
            with open(os.path.join(out, "progress.html"), "rb") as f:
                self.assertNotIn(b"\r\n", f.read())             # LF on every platform
            r2 = subprocess.run(cmd, capture_output=True, cwd=ROOT, timeout=300)
            self.assertIn(b"no change since the last snapshot", r2.stdout)
            self.assertEqual(len(os.listdir(os.path.join(out, "snapshots"))), 1)
            with open(os.path.join(out, "history.json"), encoding="utf-8") as f:
                self.assertEqual(len(json.load(f)), 1)

    def test_missing_logo_leaves_no_image(self):
        old = pd.LOGO
        try:
            pd.LOGO = os.path.join(ROOT, "assets", "no_such_logo.png")
            self.assertEqual(pd.logo_src(), "")
            self.assertIn('src=""', pd.render_page({"schema": pd.SCHEMA}, []))
        finally:
            pd.LOGO = old

    def test_script_element_cannot_be_closed_by_data(self):
        snap = {"schema": pd.SCHEMA, "x": "</script><b>"}
        self.assertNotIn("</script><b>", pd.render_page(snap, []))


if __name__ == "__main__":
    unittest.main()
