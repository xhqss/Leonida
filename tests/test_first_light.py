"""Package-gated first-light test: draw the real model a first_light.py run assembled.

tools/first_light.py writes a directory with vs.spv, ps.spv, mesh.bin and manifest.json from the user's own
package. This test runs vk_harness on that directory and checks the pixels against the manifest: a coverage
window, a corner the mesh does not cover (the harness clear value) and the UV the model's last triangle writes at
the centre pixel. The oracle is also exercised on synthetic images, without a GPU or a package, so the
all-opaque and swapped-UV negatives stay red.

    LEONIDA_FIRST_LIGHT_DIR=~/leonida-work/first_light/out python -I tests/test_first_light.py

The image is written to the directory as image_first_light.raw/.ppm; no game data is in the repository.
"""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import first_light as fl
from spirv_stubs import build_ps_stub, build_vs_stub

CLEAR = fl.CLEAR
check_image = fl.check_image
write_ppm = fl.write_ppm


def harness() -> str | None:
    exe = ".exe" if os.name == "nt" else ""
    for path in (
        os.environ.get("LEONIDA_VK_HARNESS"),
        os.path.join(ROOT, "runtime", "build", "linux", f"vk_harness{exe}"),
        os.path.join(ROOT, "runtime", "build", "windows", f"vk_harness{exe}"),
        os.path.join(ROOT, "runtime", "build", f"vk_harness{exe}"),
    ):
        if path and os.path.isfile(path):
            return path
    return None


class MeshOutputTests(unittest.TestCase):
    def test_fit_keeps_relative_depth_for_an_offset_mesh(self) -> None:
        import tempfile

        vertices = [(-2.0, -1.0, 900.0, 0.0, 0.0),
                    (2.0, -1.0, 901.0, 1.0, 0.0),
                    (0.0, 1.0, 902.0, 0.5, 1.0)]
        fitted = fl.fit(vertices, 128, 128, True, False)
        with tempfile.TemporaryDirectory() as directory:
            mesh = os.path.join(directory, "mesh.bin")
            fl.write_mesh(mesh, fitted, [0, 1, 2])
            with open(mesh, "rb") as handle:
                encoded = handle.read()

        records = encoded[fl.MESH_HEADER.size:fl.MESH_HEADER.size + 3 * 20]
        depth = b"".join(records[offset + 8:offset + 12] for offset in (0, 20, 40))
        self.assertEqual(depth, struct.pack("<3f", 0.05, 0.5, 0.95))
        self.assertEqual(encoded[-12:], struct.pack("<3I", 0, 1, 2))


class FirstLightTests(unittest.TestCase):
    def setUp(self):
        self.out = os.environ.get("LEONIDA_FIRST_LIGHT_DIR")
        if not self.out:
            self.skipTest(
                "LEONIDA_FIRST_LIGHT_DIR is not set (run tools/first_light.py first)"
            )
        self.out = os.path.abspath(os.path.expanduser(self.out))
        self.harness = harness()
        if not self.harness:
            self.skipTest(
                "vk_harness is not built (runtime/build/linux or runtime/build/windows, target "
                "vk_harness); set LEONIDA_VK_HARNESS"
            )
        with open(os.path.join(self.out, "manifest.json"), encoding="utf-8") as f:
            self.manifest = json.load(f)

    def test_draw_matches_the_manifest(self):
        raw = os.path.join(self.out, "image_first_light.raw")
        if os.path.exists(raw):
            os.unlink(
                raw
            )  # a fresh run: a harness that writes nothing cannot pass on a stale file
        env = fl.harness_env(self.manifest, self.out)
        subprocess.run(
            [
                self.harness,
                os.path.join(self.out, "vs.spv"),
                os.path.join(self.out, "ps.spv"),
                raw,
                str(self.manifest["width"]),
                str(self.manifest["height"]),
            ],
            check=True,
            env=env,
        )
        self.assertTrue(os.path.isfile(raw), "the harness did not write a fresh image")
        with open(raw, "rb") as f:
            data = f.read()
        stats = check_image(data, self.manifest)
        write_ppm(
            os.path.join(self.out, "image_first_light.ppm"),
            stats["pixels"],
            self.manifest["width"],
            self.manifest["height"],
        )
        self.assertGreater(stats["covered"], 0)

    def test_check_cli_matches_the_oracle(self):
        """The tool's --check must accept and reject the same images the test does."""
        subprocess.run(
            [
                sys.executable,
                "-I",
                os.path.join(ROOT, "tools", "first_light.py"),
                "--check",
                "--out",
                self.out,
                "--harness",
                self.harness,
            ],
            check=True,
        )

    def test_manifest_names_the_assets(self):
        self.assertGreater(self.manifest["vertex_count"], 0)
        self.assertGreater(self.manifest["triangle_count"], 0)
        for key in ("vs", "ps"):
            self.assertTrue(
                self.manifest[key].get("fssm_sha256"), f"{key} has no FSSM hash"
            )
            self.assertTrue(
                os.path.isfile(os.path.join(self.out, self.manifest["files"][key]))
            )


class PpmTests(unittest.TestCase):
    def test_nonfinite_readback_channels_write_complete_ppm(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory

        pixels = (float("nan"), float("inf"), float("-inf"), 1.0,
                  -0.25, 0.5, 1.25, 1.0)
        with TemporaryDirectory() as temp:
            path = Path(temp) / "preview.ppm"
            write_ppm(str(path), pixels, 2, 1)
            self.assertEqual(path.read_bytes(), b"P6\n2 1\n255\n" + bytes((0, 255, 0, 0, 128, 255)))


class ShaderPreparationTests(unittest.TestCase):
    def test_same_display_name_keeps_both_stage_files(self) -> None:
        modules = {"vs": build_vs_stub([0]), "ps": build_ps_stub([0])}
        entries = [
            {"name": "shared", "file": "shared.dxbc", "stage": "vs"},
            {"name": "shared", "file": "shared~1.dxbc", "stage": "ps"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            dxbc = work / "shaders" / "dxbc"
            dxbc.mkdir(parents=True)
            (dxbc / "index.json").write_text(json.dumps(entries), encoding="utf-8", newline="\n")
            for entry in entries:
                (dxbc / entry["file"]).write_bytes(entry["stage"].encode("ascii"))

            def write_translation(argv: list[str]) -> None:
                if Path(argv[0]).name != "rdna2spirv.py":
                    return
                staged = work / "first_light" / "dxbc"
                picked = json.loads((staged / "index.json").read_text(encoding="utf-8"))
                self.assertEqual(picked, entries)
                spirv = Path(argv[argv.index("--out") + 1])
                spirv.mkdir()
                index = {}
                for entry in picked:
                    stage = entry["stage"]
                    self.assertEqual((staged / entry["file"]).read_bytes(), stage.encode("ascii"))
                    stem = Path(entry["file"]).stem
                    (spirv / f"{stem}.spv").write_bytes(modules[stage])
                    index[stem] = {"stage": stage, "fssm_sha256": stage + "-fixture"}
                (spirv / "index.json").write_text(json.dumps(index), encoding="utf-8", newline="\n")

            with patch.object(fl, "run", side_effect=write_translation), redirect_stdout(io.StringIO()):
                self.assertEqual(fl.main(["--game", tmp, "--work", tmp, "--no-fit",
                                          "--vs", "shared", "--ps", "shared"]), 0)
            out = work / "first_light" / "out"
            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            runtime_index = json.loads((out / "index.json").read_text(encoding="utf-8"))
            for stage, module in modules.items():
                self.assertEqual((out / manifest["files"][stage]).read_bytes(), module)
                self.assertEqual((out / f"{stage}.dxbc").read_bytes(), stage.encode("ascii"))
                self.assertEqual(runtime_index[stage]["fssm_sha256"], stage + "-fixture")
                self.assertEqual(manifest[stage]["fssm_sha256"], stage + "-fixture")


LINK_KINDS = ("symlink", "junction") if os.name == "nt" else ("symlink",)


def link_folder(kind: str, link: Path, target: Path) -> bool:
    """Make a directory symlink or a Windows junction; False where this machine does not allow it."""
    if kind == "junction":
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        return made.returncode == 0
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    return True


class RunFolderTests(unittest.TestCase):
    """first_light/dxbc and first_light/spirv hold one run's pair, whatever an earlier run left there.

    Fully synthetic: the containers are a few hand-written bytes, and the unpacker, the disassembler and the
    translator are replaced by stand-ins that, like the real tools, take every file in the folder they are given.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="first-light-run-folders-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.work = self.tmp / "work"
        self.staged = self.work / "first_light" / "dxbc"
        self.spirv = self.work / "first_light" / "spirv"
        carved = self.work / "shaders" / "dxbc"
        carved.mkdir(parents=True)
        entries = [
            {"name": "vertex", "file": "vertex.dxbc", "stage": "vs"},
            {"name": "first", "file": "first.dxbc", "stage": "ps"},
            {"name": "second", "file": "second.dxbc", "stage": "ps"},
        ]
        (carved / "index.json").write_text(json.dumps(entries), encoding="utf-8", newline="\n")
        for entry in entries:
            (carved / entry["file"]).write_bytes(entry["name"].encode("ascii"))
        self.handed = []  # per translator run: the listings it was given

    def tools(self, argv: list[str]) -> None:
        tool = Path(argv[0]).name
        src = Path(argv[1])
        if tool == "fssm_unpack.py":
            (src / "fssm").mkdir(exist_ok=True)
            for container in sorted(src.glob("*.dxbc")):
                (src / "fssm" / f"{container.stem}.fssm").write_bytes(container.read_bytes())
            return
        out = Path(argv[argv.index("--out") + 1])
        out.mkdir(exist_ok=True)
        if tool == "shader_disasm.py":
            for blob in sorted(src.glob("*.fssm")):
                (out / f"{blob.stem}.s").write_bytes(blob.read_bytes())
            return
        self.assertEqual(tool, "rdna2spirv.py")
        stages = {Path(e["file"]).stem: e["stage"]
                  for e in json.loads((src.parent / "index.json").read_text(encoding="utf-8"))}
        index_path = out / "index.json"  # the translator merges a small run into an index it finds
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.is_file() else {}
        listings = sorted(src.glob("*.s"))
        for listing in listings:
            (out / f"{listing.stem}.spv").write_bytes(b"spv " + listing.read_bytes())
            index[listing.stem] = {"stage": stages.get(listing.stem, "?"), "fssm_sha256": listing.stem}
        index_path.write_text(json.dumps(index), encoding="utf-8", newline="\n")
        self.handed.append([listing.stem for listing in listings])

    def prepare(self, ps: str, *extra: str) -> None:
        with patch.object(fl, "run", side_effect=self.tools), redirect_stdout(io.StringIO()):
            self.assertEqual(fl.main(["--game", str(self.tmp), "--work", str(self.work), "--no-fit",
                                      "--vs", "vertex", "--ps", ps, *extra]), 0)

    def files(self, folder: Path) -> list[str]:
        return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())

    def test_a_second_run_holds_only_its_own_pair(self) -> None:
        other_out = self.work / "first_light" / "out_first"
        self.prepare("first", "--out", str(other_out))
        self.assertEqual(self.handed, [["first", "vertex"]])
        (self.staged / "leftover.dxbc").write_bytes(b"leftover")  # a container no run selected
        (self.spirv / "leftover.spvasm").write_bytes(b"leftover")
        carved_before = {name: (self.work / "shaders" / "dxbc" / name).read_bytes()
                         for name in self.files(self.work / "shaders" / "dxbc")}
        other_out_before = {name: (other_out / name).read_bytes() for name in self.files(other_out)}

        self.prepare("second")

        self.assertEqual(self.handed[1], ["second", "vertex"])
        self.assertEqual(self.files(self.staged),
                         ["disasm/second.s", "disasm/vertex.s", "fssm/second.fssm", "fssm/vertex.fssm",
                          "index.json", "second.dxbc", "vertex.dxbc"])
        self.assertEqual(self.files(self.spirv), ["index.json", "second.spv", "vertex.spv"])
        translated = json.loads((self.spirv / "index.json").read_text(encoding="utf-8"))
        self.assertEqual({stem: meta["stage"] for stem, meta in translated.items()},
                         {"second": "ps", "vertex": "vs"})
        out = self.work / "first_light" / "out"
        self.assertEqual((out / "ps.dxbc").read_bytes(), b"second")
        self.assertEqual((out / "ps.spv").read_bytes(), b"spv second")
        # Only the two run folders are emptied: the carve and the other run's --out are as they were.
        self.assertEqual({name: (self.work / "shaders" / "dxbc" / name).read_bytes()
                          for name in self.files(self.work / "shaders" / "dxbc")}, carved_before)
        self.assertEqual({name: (other_out / name).read_bytes() for name in self.files(other_out)},
                         other_out_before)
        self.assertEqual((other_out / "ps.dxbc").read_bytes(), b"first")

    def linked(self, kind: str, where: str) -> tuple[Path, Path]:
        """A work folder whose first_light/<where> is a link of this kind to a folder outside it."""
        work, outside = self.tmp / kind / "work", self.tmp / kind / "outside"
        (outside / "dxbc").mkdir(parents=True)
        for keep in (outside / "keep.dxbc", outside / "dxbc" / "keep.dxbc"):
            keep.write_bytes(b"keep")
        link = work / "first_light" / where
        link.parent.mkdir(parents=True)
        if not link_folder(kind, link, outside):
            self.skipTest(f"cannot create a {kind} here")
        return work, outside

    def test_a_link_inside_a_run_folder_is_removed_not_followed(self) -> None:
        for kind in LINK_KINDS:
            with self.subTest(kind=kind):
                work, outside = self.linked(kind, os.path.join("dxbc", "link"))
                staged = work / "first_light" / "dxbc"
                self.assertEqual(fl.reset_run_folders(str(work)), str(staged))
                self.assertEqual(os.listdir(staged), [])
                self.assertEqual(self.files(outside), ["dxbc/keep.dxbc", "keep.dxbc"])

    def test_a_run_folder_that_is_a_link_is_refused(self) -> None:
        for kind in LINK_KINDS:
            with self.subTest(kind=kind):
                work, outside = self.linked(kind, "spirv")
                staged = work / "first_light" / "dxbc"
                staged.mkdir()
                (staged / "earlier.dxbc").write_bytes(b"earlier")
                with self.assertRaises(SystemExit) as refused:
                    fl.reset_run_folders(str(work))
                self.assertIn(str(work / "first_light" / "spirv"), str(refused.exception))
                self.assertNotIn("\n", str(refused.exception))
                self.assertEqual(self.files(outside), ["dxbc/keep.dxbc", "keep.dxbc"])
                self.assertEqual(os.listdir(staged), ["earlier.dxbc"])  # refused before anything is removed

    def test_a_first_light_folder_that_is_a_link_is_refused(self) -> None:
        for kind in LINK_KINDS:
            with self.subTest(kind=kind):
                work, outside = self.linked(kind, "")
                with self.assertRaises(SystemExit) as refused:
                    fl.reset_run_folders(str(work))
                self.assertIn(str(work / "first_light"), str(refused.exception))
                self.assertNotIn("\n", str(refused.exception))
                self.assertEqual(self.files(outside), ["dxbc/keep.dxbc", "keep.dxbc"])

    def test_a_linked_first_light_with_no_run_folders_is_refused_too(self) -> None:
        # Nothing stale lies behind the link, so only the check of first_light itself can refuse it; without that
        # check the new dxbc was created in the folder the link points to, outside the work folder.
        for kind in LINK_KINDS:
            with self.subTest(kind=kind):
                work, outside = self.tmp / kind / "work", self.tmp / kind / "outside"
                outside.mkdir(parents=True)
                work.mkdir()
                if not link_folder(kind, work / "first_light", outside):
                    self.skipTest(f"cannot create a {kind} here")
                with self.assertRaises(SystemExit) as refused:
                    fl.reset_run_folders(str(work))
                self.assertIn(str(work / "first_light"), str(refused.exception))
                self.assertNotIn("\n", str(refused.exception))
                self.assertEqual(os.listdir(outside), [])

    def test_a_file_that_cannot_be_removed_stops_the_run(self) -> None:
        self.prepare("first")
        held = self.staged / "first.dxbc"
        denied = PermissionError(13, "Permission denied", str(held))
        with patch.object(fl.shutil, "rmtree", side_effect=denied), self.assertRaises(SystemExit) as stopped:
            self.prepare("second")
        self.assertIn(str(held), str(stopped.exception))
        self.assertIn("Permission denied", str(stopped.exception))
        self.assertNotIn("\n", str(stopped.exception))
        self.assertEqual(self.handed, [["first", "vertex"]])  # nothing was translated from the stale folder


class OracleTests(unittest.TestCase):
    """The readback oracle on synthetic images: the controls run without a GPU or a game package."""

    def setUp(self):
        self.manifest = {
            "width": 4,
            "height": 4,
            "min_coverage": 0.2,
            "max_coverage": 0.5,
            "centre_pixel": [2, 2],
            "centre_uv": [0.25, 0.75],
            "uv_probes": [{"pixel": [2, 2], "uv": [0.25, 0.75]}],
            "clear_pixel": [0, 0],
            "clear": CLEAR,
        }

    def image(self, centre=(0.25, 0.75), fill=None) -> bytes:
        pixels = [[CLEAR] * 4 for _ in range(16)]
        if fill is not None:
            pixels = [[fill, fill, fill, 1.0] for _ in range(16)]
        else:
            for x, y in ((1, 1), (2, 1), (1, 2), (2, 2)):
                pixels[y * 4 + x] = [centre[0], centre[1], 0.0, 1.0]
        return struct.pack("<64f", *(v for pixel in pixels for v in pixel))

    def test_accepts_the_expected_image(self):
        check_image(self.image(), self.manifest)

    def test_rejects_an_all_opaque_image(self):
        with self.assertRaises(AssertionError):
            check_image(self.image(fill=0.5), self.manifest)

    def test_rejects_a_swapped_uv(self):
        with self.assertRaises(AssertionError):
            check_image(self.image(centre=(0.75, 0.25)), self.manifest)

    def test_rejects_a_short_image(self):
        with self.assertRaises(AssertionError):
            check_image(b"", self.manifest)


class ReadObjTests(unittest.TestCase):
    """Wavefront OBJ parsing in tools/first_light.py without a GPU or game package."""

    def _write_obj(self, text: str) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "mesh.obj")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return path

    def test_negative_relative_indices_inline_comments_and_continuations(self):
        positive = self._write_obj(
            "v 0 0 0\nv 1 0 0\nv 0 1 0\n"
            "vt 0.1 0.2\nvt 0.3 0.4\nvt 0.5 0.6\n"
            "f 1/1 2/2 3/3\n"
        )
        relative = self._write_obj(
            "v 0 0 0 # origin\n"
            "v 1 0 0\n"
            "v 0 1 0\n"
            "vt 0.1 0.2 # uv0\n"
            "vt 0.3 0.4\n"
            "vt 0.5 0.6\n"
            "f -3/-3 \\\n"
            "  -2/-2 -1/-1 # trailing comment\n"
        )
        self.assertEqual(fl.read_obj(relative), fl.read_obj(positive))

    def test_rejects_zero_and_out_of_range_vertex_indices(self):
        for bad in (
            "v 0 0 0\nv 1 0 0\nv 0 1 0\nf 0 1 2\n",
            "v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 4\n",
            "v 0 0 0\nv 1 0 0\nv 0 1 0\nf -4 -2 -1\n",
            "v 0 0\nf 1 1 1\n",
        ):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                fl.read_obj(self._write_obj(bad))


class TexturedOracleTests(unittest.TestCase):
    """Synthetic tests for the textured first-light path (TEST-524): no GPU or game files."""

    def setUp(self):
        self.manifest = {
            "width": 4,
            "height": 4,
            "min_coverage": 0.2,
            "max_coverage": 0.5,
            "centre_pixel": [2, 2],
            "centre_rgba": [0.2, 0.4, 0.6, 1.0],
            "rgba_probes": [{"pixel": [1, 1], "uv": [0.25, 0.75], "rgba": [0.8, 0.2, 0.1, 1.0]}],
            "rgba_tolerance": 0.02,
            "clear_pixel": [0, 0],
            "clear": CLEAR,
            "files": {"mesh": "mesh.bin", "texture": "texture.rgba"},
            "ps": {"subgroup_size": 32},
            "texture": {
                "width": 2,
                "height": 2,
                "ptr_sgpr": 9,
                "tsharp_va": 0x100,
                "ssharp": [0x92, 0xC00000, 0x14500000, 0],
            },
        }

    def image(self, centre=(0.2, 0.4, 0.6, 1.0), probe=(0.8, 0.2, 0.1, 1.0)) -> bytes:
        pixels = [[CLEAR] * 4 for _ in range(16)]
        for x, y in ((1, 1), (2, 1), (1, 2), (2, 2)):
            pixels[y * 4 + x] = list(centre)
        pixels[1 * 4 + 1] = list(probe)
        return struct.pack("<64f", *(v for pixel in pixels for v in pixel))

    @staticmethod
    def _make_raw(width: int, height: int, overrides: dict[tuple[int, int], tuple[float, float, float, float]]) -> bytes:
        pixels = [[CLEAR] * 4 for _ in range(width * height)]
        for (x, y), rgba in overrides.items():
            pixels[y * width + x] = list(rgba)
        return struct.pack(f"<{width * height * 4}f", *(v for pixel in pixels for v in pixel))

    def test_accepts_expected_textured_image(self):
        check_image(self.image(), self.manifest)

    def test_rejects_magenta_fallback_texture(self):
        with self.assertRaises(AssertionError):
            check_image(self.image(centre=(1.0, 0.0, 1.0, 1.0)), self.manifest)

    def test_rejects_wrong_textured_probe(self):
        with self.assertRaises(AssertionError):
            check_image(self.image(probe=(0.1, 0.2, 0.8, 1.0)), self.manifest)

    def test_sample_rgba_bilinear(self):
        rgba = bytes(
            [
                255, 0, 0, 255,    0, 255, 0, 255,
                0, 0, 255, 255,    255, 255, 0, 255,
            ]
        )
        # Centre (0.5, 0.5) averages all 4 texels equally -> (0.5, 0.5, 0.25, 1.0).
        mid = fl.sample_rgba_bilinear(2, 2, rgba, 0.5, 0.5)
        for got, want in zip(mid, (0.5, 0.5, 0.25, 1.0)):
            self.assertAlmostEqual(got, want, places=4)
        # Clamped corner (0.0, 0.0) hits texel (0, 0) -> red.
        tl = fl.sample_rgba_bilinear(2, 2, rgba, -0.5, -0.5)
        for got, want in zip(tl, (1.0, 0.0, 0.0, 1.0)):
            self.assertAlmostEqual(got, want, places=4)

    def test_harness_env_sets_texture_vars(self):
        env = fl.harness_env(self.manifest, "/tmp/fl_out", base_env={})
        self.assertEqual(env["VKH_TSHARP_VA"], "0x100")
        self.assertEqual(env["VKH_SSHARP"], "0x92,0xc00000,0x14500000,0x0")
        sgprs = env["VKH_SGPRS"].split(",")
        self.assertEqual(len(sgprs), 10)
        self.assertEqual(sgprs[9], "0x100")
        self.assertEqual(env["VKH_SUBGROUP_SIZE"], "32")
        self.assertTrue(env["VKH_TEX_RGBA"].endswith("texture.rgba"))

    def test_harness_rejects_wrapped_tsharp_va_overflow(self):
        h = harness()
        if not h:
            self.skipTest("vk_harness is not built")
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            rgba_path = os.path.join(td, "tex.rgba")
            fl.write_texture_rgba(rgba_path, 1, 1, bytes([255, 0, 0, 255]))
            env = dict(
                os.environ,
                VKH_TEX_RGBA=rgba_path,
                VKH_TSHARP_VA="0xFFFFFFE0",
            )
            proc = subprocess.run(
                [h, "unused_vs.spv", "unused_ps.spv", os.path.join(td, "out.raw"), "4", "4"],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 2)
            self.assertIn("VKH_TSHARP_VA", proc.stderr)

    def test_decode_synthetic_dds_and_write_rgba(self):
        import tempfile

        # Minimal valid 2x2 uncompressed R8G8B8A8 DDS (128-byte header + 16 bytes pixels).
        dds = bytearray(128)
        dds[0:4] = b"DDS "
        struct.pack_into("<I", dds, 4, 124)       # dwSize
        struct.pack_into("<I", dds, 8, 0x1007)    # DDSD_CAPS|HEIGHT|WIDTH|PIXELFORMAT
        struct.pack_into("<I", dds, 12, 2)        # height
        struct.pack_into("<I", dds, 16, 2)        # width
        struct.pack_into("<I", dds, 76, 32)       # ddspf.dwSize
        struct.pack_into("<I", dds, 80, 0x41)     # DDPF_RGB | DDPF_ALPHAPIXELS
        struct.pack_into("<I", dds, 88, 32)       # RGBBitCount
        struct.pack_into("<IIII", dds, 92, 0x000000FF, 0x0000FF00, 0x00FF0000, 0xFF000000)
        struct.pack_into("<I", dds, 108, 0x1000)  # DDSCAPS_TEXTURE
        pixels = bytes([10, 20, 30, 255, 40, 50, 60, 255, 70, 80, 90, 255, 100, 110, 120, 255])
        dds.extend(pixels)
        with tempfile.TemporaryDirectory() as td:
            dds_path = os.path.join(td, "tex.dds")
            rgba_path = os.path.join(td, "tex.rgba")
            with open(dds_path, "wb") as f:
                f.write(dds)
            w, h, rgba, sha = fl.decode_dds_to_rgba(dds_path)
            self.assertEqual((w, h), (2, 2))
            self.assertEqual(rgba, pixels)
            self.assertEqual(len(sha), 64)
            fl.write_texture_rgba(rgba_path, w, h, rgba)
            with open(rgba_path, "rb") as f:
                blob = f.read()
            self.assertEqual(blob[:8], struct.pack("<II", 2, 2))
            self.assertEqual(blob[8:], pixels)

    def test_off_centre_default_uv_triangle_gets_probes_and_rejects_magenta(self):
        # Off-centre triangle in the top-left quadrant with default (0, 0) UVs: centre_uv is None
        # and uv_probes() is empty (|u - v| == 0). rgba_probes_for_mesh() must still pick covered
        # pixels so a magenta fallback draw is rejected when red is expected.
        verts = [
            (-0.9, -0.9, 0.5, 0.0, 0.0),
            (-0.2, -0.9, 0.5, 0.0, 0.0),
            (-0.9, -0.2, 0.5, 0.0, 0.0),
        ]
        indices = [0, 1, 2]
        self.assertIsNone(fl.uv_at(verts, indices, 16, 16, 32, 32))
        self.assertEqual(fl.uv_probes(verts, indices, 32, 32), [])
        red_tex = bytes([255, 0, 0, 255])
        probes = fl.rgba_probes_for_mesh(verts, indices, 32, 32, 1, 1, red_tex, uv_probe_list=[])
        self.assertTrue(len(probes) >= 1)
        manifest = {
            "width": 32,
            "height": 32,
            "min_coverage": 0.01,
            "max_coverage": 0.99,
            "clear_pixel": [31, 31],
            "clear": -1.0,
            "texture": {"width": 1, "height": 1},
            "rgba_tolerance": 0.02,
            "rgba_probes": probes,
        }
        # Build synthetic image: covered pixels in the triangle set to red vs magenta fallback.
        covered_pixels = {
            (px, py)
            for py in range(32)
            for px in range(32)
            if fl.uv_at(verts, indices, px, py, 32, 32) is not None
        }
        self.assertTrue(len(covered_pixels) > 0)
        red_img = self._make_raw(
            32, 32,
            {pos: (1.0, 0.0, 0.0, 1.0) for pos in covered_pixels},
        )
        res = fl.check_image(red_img, manifest)
        self.assertEqual(res["covered"], len(covered_pixels))
        magenta_img = self._make_raw(
            32, 32,
            {pos: (1.0, 0.0, 1.0, 1.0) for pos in covered_pixels},
        )
        with self.assertRaises(AssertionError):
            fl.check_image(magenta_img, manifest)
        # Empty-probe textured manifest and fallback-indistinguishable expected colour are both rejected.
        empty_manifest = dict(manifest, rgba_probes=[])
        with self.assertRaises(AssertionError):
            fl.check_image(magenta_img, empty_manifest)
        magenta_manifest = dict(
            manifest,
            rgba_probes=[{"pixel": probes[0]["pixel"], "uv": [0.0, 0.0], "rgba": [1.0, 0.0, 1.0, 1.0]}],
        )
        with self.assertRaises(AssertionError):
            fl.check_image(magenta_img, magenta_manifest)

    def test_nan_channels_rejected(self):
        nan = float("nan")
        # NaN in centre_rgba must fail (abs(NaN) > tol is False without an explicit finiteness check).
        with self.assertRaises(AssertionError):
            fl.check_image(self.image(centre=(nan, 0.4, 0.6, 1.0)), self.manifest)
        # NaN in rgba_probes must fail.
        with self.assertRaises(AssertionError):
            fl.check_image(self.image(probe=(0.8, nan, 0.1, 1.0)), self.manifest)
        # NaN in clear_pixel must fail.
        raw_clear_nan = self._make_raw(
            4, 4,
            {
                (0, 0): (nan, -1.0, -1.0, -1.0),
                (1, 1): (0.8, 0.2, 0.1, 1.0),
                (2, 1): (0.2, 0.4, 0.6, 1.0),
                (1, 2): (0.2, 0.4, 0.6, 1.0),
                (2, 2): (0.2, 0.4, 0.6, 1.0),
            },
        )
        with self.assertRaises(AssertionError):
            fl.check_image(raw_clear_nan, self.manifest)

    def test_transparent_unorm_fragments_counted_as_covered(self):
        # Valid transparent-red UNORM fragments (alpha == 0.0) must be counted as covered by
        # distinguishing them from the -1.0 clear sentinel rather than requiring alpha > 0.5.
        manifest = {
            "width": 4,
            "height": 4,
            "min_coverage": 0.2,
            "max_coverage": 0.9,
            "centre_pixel": [2, 2],
            "centre_rgba": [1.0, 0.0, 0.0, 0.0],
            "clear_pixel": [0, 0],
            "clear": -1.0,
            "texture": {"width": 1, "height": 1},
            "rgba_tolerance": 0.02,
            "rgba_probes": [{"pixel": [1, 1], "uv": [0.0, 0.0], "rgba": [1.0, 0.0, 0.0, 0.0]}],
        }
        overrides = {(x, y): (1.0, 0.0, 0.0, 0.0) for x in (1, 2) for y in (1, 2)}
        raw = self._make_raw(4, 4, overrides)
        res = fl.check_image(raw, manifest)
        self.assertEqual(res["covered"], 4)
        self.assertAlmostEqual(res["ratio"], 0.25)


if __name__ == "__main__":
    sys.exit(unittest.main())
