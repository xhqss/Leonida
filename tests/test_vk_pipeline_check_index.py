"""vk_pipeline_check: a missing, unreadable or empty index.json is an error, not an empty corpus (TEST-257).

It used to exit 0 after checking nothing. The index is read before Vulkan is loaded, so the failing cases need no GPU
and no game data. A valid `{}` stays an intentionally empty index: it gets past the index (the run may then stop at
Vulkan on a machine without a device, which is not what is checked here). Skips when vk_pipeline_check isn't built.
Set LEONIDA_VK_PIPELINE_CHECK to use a binary elsewhere.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def find_binary():
    env = os.environ.get("LEONIDA_VK_PIPELINE_CHECK")
    if env:
        return Path(env) if Path(env).is_file() else None
    build = ROOT / "runtime" / "build"
    for p in (build / "linux" / "vk_pipeline_check", build / "vk_pipeline_check", build / "vk_pipeline_check.exe",
              build / "Release" / "vk_pipeline_check.exe"):
        if p.is_file():
            return p
    return None


class IndexTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exe = find_binary()
        if cls.exe is None:
            raise unittest.SkipTest("vk_pipeline_check isn't built (cmake -S runtime -B runtime/build/linux)")

    def run_tool(self, index_text):
        with tempfile.TemporaryDirectory() as d:
            if index_text is not None:
                Path(d, "index.json").write_text(index_text, encoding="utf-8")
            return subprocess.run([str(self.exe), d], capture_output=True, text=True, timeout=60)

    def assert_index_error(self, r, text):
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn(text, r.stderr)
        self.assertIn("index.json", r.stderr)

    def test_missing_index_is_an_error(self):
        self.assert_index_error(self.run_tool(None), "cannot read")

    # Malformed input keeps the "malformed JSON" wording (open PR #1735 asserts it).
    def test_empty_index_is_an_error(self):
        self.assert_index_error(self.run_tool(""), "malformed JSON")

    def test_whitespace_only_index_is_an_error(self):
        self.assert_index_error(self.run_tool(" \n\t \n"), "malformed JSON")

    def test_malformed_index_is_still_an_error(self):
        self.assert_index_error(self.run_tool('{"a": '), "malformed JSON")

    def test_trailing_bytes_after_the_object_are_an_error(self):
        self.assert_index_error(self.run_tool("{}garbage"), "malformed JSON")
        self.assert_index_error(self.run_tool("{} {}"), "malformed JSON")

    def test_trailing_whitespace_after_the_object_is_fine(self):
        r = self.run_tool("{}\n\n \t")
        self.assertNotIn("malformed JSON", r.stderr)
        self.assertIn("0 shaders in index", r.stderr)

    def test_valid_empty_object_is_an_empty_corpus(self):
        r = self.run_tool("{}")
        self.assertNotIn("cannot use", r.stderr)
        self.assertIn("0 shaders in index", r.stderr)


def main():
    """unittest.main(), except that a file whose tests all skipped succeeds: unittest exits 5 for an empty run, and
    CI runs each file as `python -I tests/test_*.py || exit 1`, so a runner without vk_pipeline_check would fail the
    job here. A file that has no tests at all (not even skips) is still a failure."""
    result = unittest.main(exit=False).result
    if result.failures or result.errors or result.unexpectedSuccesses:
        return 1
    if result.testsRun == 0 and not result.skipped:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
