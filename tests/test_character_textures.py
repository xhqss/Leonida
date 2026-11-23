"""character_textures.py on synthetic decoded model JSON.

Run with ``python -I tests/test_character_textures.py``. Covers a head material (diffusetex +
bumptex), a garment material (speculartex only, must flag missing albedo), an unknown slot, the
JSON/CSV reports and the report-path guard. Every fixture is hand-written; no game data is used.
"""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "character_textures.py"


def model(materials, name="synthetic_model"):
    return {"Name": name, "Materials": materials}


def write_model(folder, document, name="model.json"):
    folder = pathlib.Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def run_tool(*args):
    return subprocess.run([sys.executable, "-I", str(TOOL), *map(str, args)],
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60)


HEADER = "model\tmaterial\tslot\trole\ttexture"


def table(output):
    """The data rows of the tab-separated table (header and FLAG lines excluded)."""
    return [line.split("\t") for line in output.splitlines()
            if line.count("\t") == 4 and line != HEADER]


class CharacterTextureTests(unittest.TestCase):
    def test_head_material_reports_diffusetex_and_bumptex(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_model(td, model([{
                "Name": "head",
                "Shader": "a1b2c3d4",
                "Textures": [
                    {"Param": "0xC8E8C282", "Name": "face_diffusetex"},
                    {"Param": "0x1E77BF4C", "Name": "face_bumptex"},
                ],
            }]))
            result = run_tool(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = {row[3]: row for row in table(result.stdout)}
            self.assertEqual(rows["diffusetex"], [
                "synthetic_model", "head", "0xC8E8C282", "diffusetex", "face_diffusetex"])
            self.assertEqual(rows["bumptex"], [
                "synthetic_model", "head", "0x1E77BF4C", "bumptex", "face_bumptex"])
            self.assertNotIn("FLAG", result.stdout)

    def test_garment_without_albedo_is_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_model(td, model([{
                "Name": "jacket",
                "Shader": "D5ED50F4",
                "Textures": [{"Param": "0xC6F46E79", "Name": "jacket_speculartex"}],
            }]))
            result = run_tool(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = table(result.stdout)
            self.assertEqual(len(rows), 1, result.stdout)
            self.assertEqual(rows[0][2:], ["0xC6F46E79", "speculartex", "jacket_speculartex"])
            self.assertIn("FLAG garment-no-albedo", result.stdout)
            self.assertIn("d5ed50f4", result.stdout)
            self.assertIn("speculartex is not colour", result.stdout)
            self.assertNotIn("diffusetex", result.stdout)

    def test_unknown_slot_is_not_guessed_from_its_name(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_model(td, model([{
                "Name": "mystery",
                "Shader": "a1b2c3d4",
                "Textures": [{"Param": 0xDEADBEEF, "Name": "diffusetex"}],
            }]))
            result = run_tool(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = table(result.stdout)
            self.assertEqual(rows[0][2], "0xDEADBEEF")
            self.assertEqual(rows[0][3], "unknown 0xDEADBEEF")
            self.assertEqual(rows[0][4], "diffusetex")
            self.assertIn("unknown slots", result.stdout)

    def test_every_verified_slot_maps_to_its_name(self):
        slots = {
            "0xC8E8C282": "diffusetex",
            "0xC6F46E79": "speculartex",
            "0x1E77BF4C": "bumptex",
            "0x739E3D0D": "speculartex2",
            "0x8ADB8480": "materialid",
        }
        with tempfile.TemporaryDirectory() as td:
            path = write_model(td, model([{
                "Name": "slots",
                "Shader": "a1b2c3d4",
                "Textures": [{"Param": slot, "Name": role} for slot, role in slots.items()],
            }]))
            result = run_tool(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual({row[2]: row[3] for row in table(result.stdout)}, slots)
            self.assertFalse([row for row in table(result.stdout) if row[3].startswith("unknown")])

    def test_missing_albedo_outside_a_garment_shader_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_model(td, model([{
                "Name": "trim",
                "Shader": "a1b2c3d4",
                "Textures": [{"Param": "0x1E77BF4C", "Name": "trim_normal"}],
            }]), name="model.json")
            result = run_tool(path, "--json", pathlib.Path(td) / "report.json")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("garment-no-albedo", result.stdout)
            report = json.loads((pathlib.Path(td) / "report.json").read_text(encoding="utf-8"))
            material = report["models"][0]["materials"][0]
            self.assertTrue(material["missing_albedo"])
            self.assertEqual(material["flags"], [])

    def test_json_and_csv_reports(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_model(td, model([
                {"Name": "head", "Shader": "a1b2c3d4", "Textures": [
                    {"Param": "C8E8C282", "Name": "face_diffusetex"}]},
                {"Name": "jacket", "Shader": "ea8d09d0", "Textures": [
                    {"Param": "C6F46E79", "Name": "jacket_speculartex"}]},
            ]))
            report_path = pathlib.Path(td) / "out" / "report.json"
            csv_path = pathlib.Path(td) / "out" / "report.csv"
            result = run_tool(path, "--json", report_path, "--csv", csv_path)
            self.assertEqual(result.returncode, 0, result.stderr)

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["summary"]["materials"], 2)
            self.assertEqual(report["summary"]["bindings"], 2)
            self.assertEqual(report["summary"]["garment_without_albedo"], 1)
            head, jacket = report["models"][0]["materials"]
            self.assertEqual(head["bindings"][0]["role"], "diffusetex")
            self.assertTrue(jacket["garment"])
            self.assertTrue(jacket["missing_albedo"])
            self.assertEqual(jacket["flags"], ["garment-no-albedo"])
            self.assertTrue(report["warnings"][0].startswith("garment-no-albedo"))

            data = csv_path.read_bytes()
            self.assertNotIn(b"\r", data)
            lines = data.decode("utf-8").splitlines()
            self.assertEqual(lines[0], "source,model,material,shader,slot,role,texture,flags")
            self.assertIn(",head,a1b2c3d4,0xC8E8C282,diffusetex,face_diffusetex,", lines[1])
            self.assertIn(",jacket,ea8d09d0,0xC6F46E79,speculartex,jacket_speculartex,garment-no-albedo",
                          lines[2])

    def test_directory_is_searched_recursively(self):
        with tempfile.TemporaryDirectory() as td:
            write_model(pathlib.Path(td) / "a" / "b", model([{
                "Name": "head", "Shader": "a1b2c3d4",
                "Textures": [{"Param": "0xC8E8C282", "Name": "face"}]}], name="first"))
            write_model(pathlib.Path(td) / "c", model([{
                "Name": "jacket", "Shader": "39ddb9ca",
                "Textures": [{"Param": "0xC6F46E79", "Name": "spec"}]}], name="second"))
            result = run_tool(td)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("first", result.stdout)
            self.assertIn("second", result.stdout)
            self.assertEqual(result.stdout.count("FLAG garment-no-albedo"), 1)

    def test_json_cannot_overwrite_an_input(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_model(td, model([{"Name": "head", "Shader": "a1b2c3d4", "Textures": []}]))
            before = path.read_bytes()
            result = run_tool(path, "--json", path)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("would overwrite the input", result.stderr)
            self.assertEqual(path.read_bytes(), before)

    def test_csv_cannot_overwrite_an_input(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_model(td, model([{"Name": "head", "Shader": "a1b2c3d4", "Textures": []}]))
            before = path.read_bytes()
            result = run_tool(path, "--csv", path)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("would overwrite the input", result.stderr)
            self.assertEqual(path.read_bytes(), before)

    def test_json_and_csv_must_use_different_paths(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_model(td, model([{"Name": "head", "Shader": "a1b2c3d4", "Textures": []}]))
            out = pathlib.Path(td) / "report.bin"
            result = run_tool(path, "--json", out, "--csv", out)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("must use different paths", result.stderr)


if __name__ == "__main__":
    unittest.main()
