"""scripts/first_look.ps1 rebuilds runtime\\build\\gta6rt.exe when it is older than the runtime sources.

    python -I tests/test_first_look_rebuild.py

A gta6rt.exe from before a pull still runs, but its dry run reports the old shim table: the day-one import to-do list
would be wrong without any error. The script used to build the loader only when it was missing. No game files.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "first_look.ps1"
PE_INSPECT_STUB = (
    "from pathlib import Path\n"
    "import sys\n"
    "report = Path(sys.argv[-1]) / (Path(sys.argv[1]).name + '.json')\n"
    "report.write_text('{\"imports\":[],\"sections\":[]}', encoding='utf-8', newline='\\n')\n"
)
# The build script leaves a marker; the loader is replaced by an alias that echoes its arguments.
BUILD_STUB = "Set-Content -Path (Join-Path $PSScriptRoot 'built.txt') -Value 'built'\n"
RUN = r"""
$ErrorActionPreference = 'Stop'
Set-Alias -Name (Join-Path $PWD 'runtime\build\gta6rt.exe') -Value Write-Output
& .\scripts\first_look.ps1 -Exe (Join-Path $PWD 'fixture.exe')
"""


@unittest.skipUnless(os.name == "nt", "Windows PowerShell diagnostics script")
class FirstLookRebuildTest(unittest.TestCase):
    def run_case(self, source_newer):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for folder in ("scripts", "tools", "runtime/build", "runtime/src"):
                (root / folder).mkdir(parents=True)
            shutil.copyfile(SCRIPT, root / "scripts" / SCRIPT.name)
            (root / "scripts" / "build_runtime.ps1").write_text(BUILD_STUB, encoding="utf-8")
            (root / "tools" / "pe_inspect.py").write_text(PE_INSPECT_STUB, encoding="utf-8", newline="\n")
            (root / "fixture.exe").write_bytes(b"MZ")
            loader = root / "runtime" / "build" / "gta6rt.exe"
            source = root / "runtime" / "src" / "pe_loader.cpp"
            loader.write_bytes(b"")
            source.write_text("// source\n", encoding="utf-8")
            now = time.time()
            older, newer = now - 3600, now
            os.utime(loader, (newer, newer) if not source_newer else (older, older))
            os.utime(source, (newer, newer) if source_newer else (older, older))
            env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
            env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
            run = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                  "-Command", RUN], cwd=root, env=env, capture_output=True, text=True, timeout=60)
            return (root / "scripts" / "built.txt").is_file(), run.stdout + run.stderr

    def test_a_loader_older_than_the_sources_is_rebuilt(self):
        built, out = self.run_case(source_newer=True)
        self.assertTrue(built, out)
        self.assertIn("rebuilding it first", out)

    def test_an_up_to_date_loader_is_used_as_it_is(self):
        built, out = self.run_case(source_newer=False)
        self.assertFalse(built, out)


if __name__ == "__main__":
    unittest.main()
