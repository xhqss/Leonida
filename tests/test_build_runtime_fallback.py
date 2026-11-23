"""scripts/build_runtime.ps1's cl.exe fallback builds every GPU test harness the Python tools need (no build run).

The fallback is what runs when Visual Studio has no "C++ CMake tools" component. Without these programs,
check.py / run_gpu_tests.py stop with "missing vk_compute_test", and so do the fuzzers.

    python -I tests/test_build_runtime_fallback.py
"""
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "runtime" / "test" / "shaders"), str(ROOT / "tools")]
import check  # noqa: E402
import image_fuzz  # noqa: E402
import run_gpu_tests  # noqa: E402

SCRIPT = ROOT / "scripts" / "build_runtime.ps1"


def fallback_body(text: str) -> str:
    """The cl.exe branch: from the line-starting `} else {` after the CMake test to the end of its here-string."""
    start = re.search(r"^\} else \{", text, re.M).start()
    return text[start:text.index('"@', text.index('$body = @"', start))]


class FallbackBuildsTheHarnesses(unittest.TestCase):
    def setUp(self):
        self.body = fallback_body(SCRIPT.read_text(encoding="utf-8"))
        self.programs = set(re.findall(r"/Fe:([A-Za-z0-9_]+)\.exe", self.body))

    def test_the_harnesses_the_gpu_tools_require_are_built(self):
        needed = set(run_gpu_tests.REQUIRED_TEST_PROGRAMS) | set(check.TEST_PROGRAMS) | {image_fuzz.HARNESS}
        self.assertEqual(needed - self.programs, set(), "missing from build_runtime.ps1's cl.exe fallback")

    def test_every_harness_the_gpu_suite_starts_is_built(self):
        """A harness run_gpu_tests.py or check.py starts by name, but the fallback did not build, fails its test
        with "could not start" (ps_ssharp: vk_sampler_test.exe, WinError 2)."""
        named = set()
        for path in (ROOT / "scripts" / "run_gpu_tests.py", ROOT / "runtime" / "test" / "shaders" / "check.py"):
            named |= set(re.findall(r'"(vk_[a-z0-9_]+)"', path.read_text(encoding="utf-8")))
        harnesses = {n for n in named if (ROOT / "runtime" / "test" / f"{n}.cpp").is_file()}
        self.assertIn("vk_sampler_test", harnesses)
        self.assertEqual(harnesses - self.programs, set(), "missing from build_runtime.ps1's cl.exe fallback")

    def test_the_launcher_compiles_every_source_cmake_gives_gta6rt(self):
        """BUILD-680: main.cpp calls rt::fs::configure, but the fallback left out shims/file_system.cpp (LNK2019).
        Every source of CMake's add_executable(gta6rt ...) must be in the fallback's launcher list ($files)."""
        cmake = (ROOT / "runtime" / "CMakeLists.txt").read_text(encoding="utf-8")
        block = re.search(r"add_executable\(gta6rt\s+(.*?)\)", cmake, re.S).group(1)
        wanted = {item.replace("/", "\\") for item in block.split() if item.endswith(".cpp")}
        files = re.search(r"\$files = (.*?) \| ForEach-Object", self.body).group(1)
        listed = set(re.findall(r'"([^"]+\.cpp)"', files))
        self.assertTrue(wanted)
        self.assertEqual(wanted - listed, set(), "in CMake's gta6rt sources but not in the cl.exe fallback")

    def test_every_test_source_it_compiles_exists(self):
        sources = re.findall(r'"\$src\\test\\([A-Za-z0-9_]+\.cpp)"', self.body)
        self.assertTrue(sources)
        for name in sources:
            with self.subTest(source=name):
                self.assertTrue((ROOT / "runtime" / "test" / name).is_file())

    def test_build_cmd_sets_utf8_codepage_for_ninja_showincludes(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("chcp 65001 >nul", text)
        self.assertLess(text.index("chcp 65001 >nul"), text.index('call `"$vcvars`"'))


if __name__ == "__main__":
    unittest.main()
