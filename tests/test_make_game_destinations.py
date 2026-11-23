"""make_game.py rejects a --out/--work that is a file (or sits under one) before doing any work, instead of a
traceback from os.makedirs or a run that reports success without creating the destination.

    python -I tests/test_make_game_destinations.py
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import make_game


class DestinationTests(unittest.TestCase):
    def run_main(self, *argv):
        """main() over the given arguments, with the game check stubbed out. Returns (exit code, stderr)."""
        err = io.StringIO()
        with patch.object(sys, "argv", ["make_game.py", *argv]), \
             patch.object(make_game, "below_normal"), \
             patch.object(make_game, "log"), \
             patch.object(make_game, "check", return_value={"title": "test", "version": "1.0"}), \
             patch.object(make_game, "preflight", side_effect=AssertionError("preflight was reached")), \
             contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as raised:
                make_game.main()
        return raised.exception.code, err.getvalue()

    def test_out_is_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = os.path.join(tmp, "game")
            os.makedirs(game)
            out = os.path.join(tmp, "out.txt")
            open(out, "w").close()
            code, err = self.run_main("--game", game, "--out", out, "--work", os.path.join(tmp, "work"))
            self.assertEqual(code, 1, err)
            self.assertIn("--out is not a folder", err)

    def test_work_is_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = os.path.join(tmp, "game")
            os.makedirs(game)
            work = os.path.join(tmp, "work.txt")
            open(work, "w").close()
            code, err = self.run_main("--game", game, "--out", os.path.join(tmp, "out"), "--work", work)
            self.assertEqual(code, 1, err)
            self.assertIn("--work is not a folder", err)
            self.assertFalse(os.path.exists(os.path.join(tmp, "out")), "the build created --out before rejecting --work")

    def test_out_under_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = os.path.join(tmp, "game")
            os.makedirs(game)
            blocker = os.path.join(tmp, "blocker.txt")
            open(blocker, "w").close()
            code, err = self.run_main("--game", game, "--out", os.path.join(blocker, "out"),
                                      "--work", os.path.join(tmp, "work"))
            self.assertEqual(code, 1, err)
            self.assertIn("--out is not a folder", err)

    def test_missing_destination_is_accepted(self):
        # A destination that does not exist yet is fine as long as its nearest existing parent is a folder.
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(sys, "argv", ["make_game.py", "--game", os.path.join(tmp, "game"), "--check",
                                        "--out", os.path.join(tmp, "new", "out"),
                                        "--work", os.path.join(tmp, "new", "work")]), \
             patch.object(make_game, "below_normal"), patch.object(make_game, "log"), \
             patch.object(make_game, "check", return_value={"title": "test", "version": "1.0"}), \
             contextlib.redirect_stderr(io.StringIO()):
            os.makedirs(os.path.join(tmp, "game"))
            self.assertEqual(make_game.main(), 0)


if __name__ == "__main__":
    unittest.main()
