"""Exercise the Windows diagnostics script with a synthetic inspector and loader."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "first_look.ps1"
POWERSHELL = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]


@unittest.skipUnless(os.name == "nt", "Windows PowerShell diagnostics script")
class FirstLookExitTest(unittest.TestCase):
    def test_reports_are_written_and_dry_run_exit_status_is_returned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / "scripts"
            tools = root / "tools"
            build = root / "runtime" / "build"
            for folder in (scripts, tools, build):
                folder.mkdir(parents=True)
            wrapper = Path(shutil.copyfile(SCRIPT, scripts / SCRIPT.name))
            (tools / "pe_inspect.py").write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "report = Path(sys.argv[-1]) / (Path(sys.argv[1]).name + '.json')\n"
                "report.write_text('{\"imports\":[{\"where\":\"shim\",\"dll\":\"stub.dll\","
                "\"functions\":[\"Example\"]}],\"sections\":[]}', encoding='utf-8', newline='\\n')\n",
                encoding="utf-8", newline="\n",
            )
            compile_loader = (
                "$ErrorActionPreference = 'Stop'; "
                "Add-Type -OutputAssembly gta6rt.exe -OutputType ConsoleApplication -TypeDefinition @'\n"
                "public class Loader { public static int Main() {\n"
                "    int code = int.Parse(System.Environment.GetEnvironmentVariable(\"LEONIDA_TEST_LOADER_EXIT\"));\n"
                "    (code == 0 ? System.Console.Out : System.Console.Error).WriteLine(\"synthetic dry run\");\n"
                "    return code;\n"
                "} }\n'@"
            )
            env = {**os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]}
            # Keep Core's modules out of Windows PowerShell's search path when Python was started from pwsh.
            env["PSModulePath"] = str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/Modules")
            subprocess.run([*POWERSHELL, "-Command", compile_loader], cwd=build, env=env, check=True, timeout=30)
            for code in (0, 17):
                with self.subTest(loader_exit_code=code):
                    image = root / f"fixture-{code}.exe"
                    image.write_bytes(b"MZ")
                    result = subprocess.run(
                        [*POWERSHELL, "-File", str(wrapper), "-Exe", str(image)],
                        env={**env, "LEONIDA_TEST_LOADER_EXIT": str(code)}, capture_output=True, text=True, timeout=30,
                    )
                    report = root / "reports" / "exe" / image.stem
                    self.assertTrue((report / "shim-todo.txt").is_file(), result.stdout + result.stderr)
                    self.assertIn("synthetic dry run", (report / "dryrun.txt").read_text(encoding="utf-16"))
                    self.assertEqual(result.returncode, code, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
