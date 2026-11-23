"""vk_pipeline_check --isolate: a module that kills the driver is recorded as failed and the pass goes on.

No game data: five tiny compute shaders are assembled here with spirv-as. The crash is simulated with the tool's test
hook VK_PIPELINE_CHECK_SIMULATE_CRASH (a signal raised just before the pipeline of the named module), so no crashing
driver is needed. Skips (not fails) when vk_pipeline_check isn't built, spirv-as is missing, the platform is not POSIX
or there is no usable Vulkan device. Pin a GPU with VK_LOADER_DRIVERS_SELECT / LEONIDA_VK_DEVICE as for the other
Vulkan tools. Set LEONIDA_VK_PIPELINE_CHECK to use a binary elsewhere.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAMES = ["m_alpha", "m_bravo", "m_charlie", "m_delta", "m_echo"]
CRASHER = "m_charlie"

ASM = """OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize {size} 1 1
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
OpReturn
OpFunctionEnd
"""


def find_binary():
    env = os.environ.get("LEONIDA_VK_PIPELINE_CHECK")
    if env:
        return Path(env) if Path(env).is_file() else None
    build = ROOT / "runtime" / "build"
    for p in (build / "linux" / "vk_pipeline_check", build / "vk_pipeline_check"):
        if p.is_file():
            return p
    return None


class IsolateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.name != "posix":
            raise unittest.SkipTest("--isolate is POSIX only")
        cls.exe = find_binary()
        if cls.exe is None:
            raise unittest.SkipTest("vk_pipeline_check isn't built (cmake -S runtime -B runtime/build/linux)")
        spirv_as = shutil.which("spirv-as")
        if not spirv_as:
            raise unittest.SkipTest("spirv-as not found")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.dir = Path(cls.tmp.name) / "spirv"
        cls.dir.mkdir()
        index = {}
        for i, name in enumerate(NAMES):
            asm = Path(cls.tmp.name) / (name + ".spvasm")
            asm.write_text(ASM.format(size=1 << i))
            subprocess.run([spirv_as, "--target-env", "vulkan1.1", str(asm), "-o", str(cls.dir / (name + ".spv"))],
                           check=True)
            index[name] = {"stage": "cs", "subgroup_size": None, "features": []}
        (cls.dir / "index.json").write_text(json.dumps(index))
        probe = cls.run_tool([], cache=Path(cls.tmp.name) / "probe.cache")
        if probe.returncode != 0 or "cs pipelines: 5 ok, 0 failed" not in probe.stdout:
            raise unittest.SkipTest("no usable Vulkan device (exit %d): %s" % (probe.returncode, probe.stderr.strip()[-300:]))

    @classmethod
    def run_tool(cls, args, crash=None, cache=None):
        env = dict(os.environ)
        env.pop("VK_PIPELINE_CHECK_SIMULATE_CRASH", None)
        if crash:
            env["VK_PIPELINE_CHECK_SIMULATE_CRASH"] = crash
        cmd = [str(cls.exe), str(cls.dir)] + ([str(cache)] if cache else []) + ["--jobs", "2"] + args
        return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)

    def cache(self, name):
        return Path(self.tmp.name) / name

    def test_normal_run_passes(self):
        r = self.run_tool([], cache=self.cache("normal.cache"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("cs: 5 modules ok, 0 failed", r.stdout)
        self.assertIn("cs pipelines: 5 ok, 0 failed, 0 skipped", r.stdout)

    def test_isolate_without_a_crash_matches_normal_counts(self):
        r = self.run_tool(["--isolate", "--isolate-batch", "2"], cache=self.cache("iso_clean.cache"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("cs: 5 modules ok, 0 failed", r.stdout)
        self.assertIn("cs pipelines: 5 ok, 0 failed, 0 skipped", r.stdout)
        self.assertNotIn("CRASH", r.stdout)

    def test_without_isolate_the_driver_crash_ends_the_process(self):
        r = self.run_tool([], crash=CRASHER, cache=self.cache("dies.cache"))
        self.assertEqual(r.returncode, -8, r.stdout + r.stderr)   # killed by SIGFPE: no summary, no exit status

    def test_isolate_records_the_crashed_module_and_goes_on(self):
        cache = self.cache("iso.cache")
        for batch in ("5", "2", "1"):   # one batch bisected down, several batches, one module at a time
            with self.subTest(batch=batch):
                r = self.run_tool(["--isolate", "--isolate-batch", batch], crash=CRASHER + ":SIGFPE", cache=cache)
                self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertGreater(r.returncode, 0, "the parent itself must not die: " + r.stdout + r.stderr)
                self.assertIn("MODULE CRASH %s (cs, driver crashed: SIGFPE)" % CRASHER, r.stdout)
                self.assertEqual(r.stdout.count("MODULE CRASH"), 1, r.stdout)
                self.assertIn("cs: 4 modules ok, 0 failed", r.stdout)
                self.assertIn("cs pipelines: 4 ok, 1 failed, 0 skipped", r.stdout)
                self.assertIn("cs crashed the driver: 1", r.stdout)
                for name in NAMES:
                    if name != CRASHER:
                        self.assertNotIn("FAIL " + name, r.stdout)
                self.assertTrue(cache.is_file(), "the shared pipeline cache must survive the crash")

    def test_other_signal(self):
        r = self.run_tool(["--isolate"], crash=CRASHER + ":SEGV")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("MODULE CRASH %s (cs, driver crashed: SIGSEGV)" % CRASHER, r.stdout)

    def test_isolate_rejects_stats(self):
        r = self.run_tool(["--isolate", "--stats", str(self.cache("s.csv"))])
        self.assertEqual(r.returncode, 2)


def main():
    """unittest.main(), except that a file whose tests all skipped succeeds: unittest exits 5 for an empty run, and
    CI runs each file as `python -I tests/test_*.py || exit 1`, so a runner without vk_pipeline_check or on Windows
    would fail the job here. A file that has no tests at all (not even skips) is still a failure."""
    result = unittest.main(exit=False).result
    if result.failures or result.errors or result.unexpectedSuccesses:
        return 1
    if result.testsRun == 0 and not result.skipped:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
