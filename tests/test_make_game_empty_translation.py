"""make_game.py: an empty translated shader index is a failed stage, not a finished one.

Standard library only; no GPU, no Vulkan SDK, no game data: the shard processes and rdna2spirv.py are mocked.

    python -I tests/test_make_game_empty_translation.py
"""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import make_game  # noqa: E402


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class Done:
    def poll(self):
        return 0

    def wait(self):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


class TranslateEmptyIndexTest(unittest.TestCase):
    """translate(): a merge that published nothing is an error and must not leave a marker behind."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.disasm = os.path.join(tmp.name, "dxbc", "disasm")
        self.spirv = os.path.join(tmp.name, "spirv")
        os.makedirs(self.disasm)

    def translate_with_index(self, text):
        def fake_py(tool, *args):
            if "--merge" in args:
                write(os.path.join(self.spirv, "index.json"), text)

        with mock.patch.object(make_game, "log", lambda *a: None), \
                mock.patch.object(make_game, "py", side_effect=fake_py), \
                mock.patch.object(make_game.subprocess, "Popen", lambda *a, **k: Done()):
            make_game.translate(self.disasm, self.spirv, 1)

    def test_an_empty_index_is_removed_with_the_plan_and_the_retry_fails(self):
        for _ in range(2):
            with self.assertRaises(SystemExit) as cm:
                self.translate_with_index("{}")
            self.assertEqual(cm.exception.code, 1)
            self.assertFalse(os.path.exists(os.path.join(self.spirv, "index.json")))
            self.assertFalse(os.path.exists(os.path.join(self.spirv, make_game.SHARD_PLAN)))

    def test_an_unreadable_index_is_removed(self):
        with self.assertRaises(SystemExit) as cm:
            self.translate_with_index("not json")
        self.assertEqual(cm.exception.code, 1)
        self.assertFalse(os.path.exists(os.path.join(self.spirv, "index.json")))

    def test_a_non_empty_index_is_kept(self):
        self.translate_with_index(json.dumps({"shader_0": {"stage": "ps"}}))
        with open(os.path.join(self.spirv, "index.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"shader_0": {"stage": "ps"}})


class RequireTranslatedShadersTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.work = tmp.name
        self.spirv = os.path.join(self.work, "shaders", "spirv")
        os.makedirs(self.spirv)

    def call(self):
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            return make_game.require_translated_shaders(self.work), err.getvalue()

    def test_missing_index_names_the_stage(self):
        with self.assertRaises(SystemExit) as cm:
            self.call()
        self.assertEqual(cm.exception.code, 1)

    def test_an_empty_index_is_refused(self):
        write(os.path.join(self.spirv, "index.json"), "{}")
        with self.assertRaises(SystemExit) as cm:
            self.call()
        self.assertEqual(cm.exception.code, 1)

    def test_an_unreadable_index_is_refused(self):
        write(os.path.join(self.spirv, "index.json"), "not json")
        with self.assertRaises(SystemExit) as cm:
            self.call()
        self.assertEqual(cm.exception.code, 1)

    def test_a_non_empty_index_is_accepted(self):
        write(os.path.join(self.spirv, "index.json"), '{"shader_0": {}}')
        self.assertEqual(self.call()[0], self.spirv)


if __name__ == "__main__":
    unittest.main()
