"""Exercise the assets stage, existing C# catalogs and downstream texture lookup."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tools"), str(ROOT / "tests")]
import make_game
import ttpf_assets
import world_build
from test_calibrated_texture import synthetic_profile
from test_ttpf_mips import build_bc1_bcpack_pool


class TextureAssets(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.game = self.root / "game"
        self.work = self.root / "work"
        audio = self.work / "assets/audio"
        audio.mkdir(parents=True)
        for marker in ("audio_manifest.json", "ogg.done", "xma.done"):
            (audio / marker).touch()
        self.pool = self.game / "data/pool.ttpf"
        self.pool.parent.mkdir(parents=True)
        self.folder = self.work / "assets/ttpf/pool"
        self.folder.mkdir(parents=True)
        self.catalog = self.folder / "manifest.json"
        self.row = dict(Index=0, AssetKey="CAFEBABE12345678", SourceMips=5, BaseMipLevel=0,
                        Mips=0, File=None, DxgiFormat=71, Limitation="BCPack")
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(make_game, "MODEL_ARCHIVES", ()))
        self.stack.enter_context(mock.patch.object(make_game, "log"))

    def build(self):
        make_game.assets(str(self.game), str(self.work), False, 1)

    def legacy(self, **changes):
        self.catalog.write_text(json.dumps({"Count": 1, "Textures": [self.row | changes]}), encoding="utf-8")

    def row_after_export(self):
        return json.loads(self.catalog.read_bytes())["Textures"][0]

    def test_partial_upgrade_failure_then_resume(self):
        _, linear, _ = build_bc1_bcpack_pool(self.pool)
        self.legacy(File="partial.dds", BaseMipLevel=2, Mips=3, Limitation=None)
        partial = self.folder / "partial.dds"
        partial.write_bytes(b"previous mip chain")
        prior = self.catalog.read_bytes()
        replace = os.replace

        def fail_catalog(src, dest):
            if dest == self.catalog:
                raise OSError("simulated write failure")
            replace(src, dest)

        with mock.patch.object(os, "replace", side_effect=fail_catalog), self.assertRaises(OSError):
            self.build()
        self.assertEqual(prior, self.catalog.read_bytes())
        self.assertEqual(partial.read_bytes(), b"previous mip chain")
        self.build()
        row = self.row_after_export()
        path = self.folder / row["File"]
        self.assertEqual((row["Mips"], row["BaseMipLevel"]), (5, 0))
        self.assertEqual(path.read_bytes()[148:], b"".join(linear.values()))
        self.assertEqual(world_build.index_ttpf_textures(self.work / "assets")[row["AssetKey"]], str(path))
        with mock.patch.object(ttpf_assets.ttpf, "Pool", side_effect=AssertionError("unexpected reopen")):
            self.build()

    def test_profile_added_after_strict_rejection(self):
        build_bc1_bcpack_pool(self.pool, ref_dists=[250] + [0] * 1023)
        self.legacy()
        self.build()
        row = self.row_after_export()
        self.assertIsNone(row["File"])
        self.assertIn("BCPackLimitation", row)
        try:
            from calibrated_texture import native
            native()
        except (ImportError, OSError) as exc:
            self.skipTest(str(exc))
        profile = self.work / "bcpack-calibration.json"
        config = synthetic_profile()
        profile.write_text(json.dumps(config), encoding="utf-8")
        self.build()
        row = self.row_after_export()
        original = self.folder / row["File"]
        self.assertEqual(original.read_bytes()[:4], b"DDS ")
        self.assertNotIn("BCPackLimitation", row)
        config["bc1"]["selectors"][0] = [mask ^ 1 for mask in config["bc1"]["selectors"][0]]
        profile.write_text(json.dumps(config), encoding="utf-8")
        self.build()
        updated = self.folder / self.row_after_export()["File"]
        self.assertNotEqual(updated, original)
        self.assertNotEqual(updated.read_bytes(), original.read_bytes())

    @unittest.skipUnless(os.environ.get("RPF9DUMP_DLL"), "requires the built rpf9dump")
    def test_new_base_and_pack_pools(self):
        dll = Path(os.environ["RPF9DUMP_DLL"]).resolve()
        exe = dll.with_suffix(".exe" if os.name == "nt" else "")
        pack = self.game / "pack/story/data/pool.ttpf"
        pack.parent.mkdir(parents=True)
        _, linear, _ = build_bc1_bcpack_pool(self.pool)
        pack.write_bytes(self.pool.read_bytes())
        with mock.patch.object(make_game, "rpf9dump", return_value=str(exe)):
            self.build()
        for folder in (self.folder, self.work / "assets/ttpf_pack/story/pool"):
            row = json.loads((folder / "manifest.json").read_bytes())["Textures"][0]
            self.assertEqual(row["Mips"], 5)
            self.assertEqual((folder / row["File"]).read_bytes()[148:], b"".join(linear.values()))

    def test_complete_pool_handles_unmapped_range_without_crashing(self):
        # If pool.codecs() raises ValueError (e.g., an unmapped hole inside a texture's span),
        # complete_pool must record BCPackLimitation on that row instead of aborting the pool.
        build_bc1_bcpack_pool(self.pool)
        self.legacy()
        with mock.patch.object(ttpf_assets.ttpf.Pool, "codecs",
                               side_effect=ValueError("texture range is not completely mapped")):
            self.build()
        row = self.row_after_export()
        self.assertIsNone(row["File"])
        self.assertEqual(row["BCPackLimitation"], "texture range is not completely mapped")


if __name__ == "__main__":
    unittest.main()
