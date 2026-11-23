"""Concurrent-output regression tests for tools/world_build.py (no game data).

A world build stages chunks in the output directory, publishes them one at a time,
then prunes stale chunks.  Two builds on one output must not interleave: the
second one must fail while the first holds the lock, and the next build must
work again after the lock is released.
"""
from contextlib import contextmanager
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOLS = os.path.join(ROOT, "tools")
for path in (TOOLS, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

import test_world_build as twb  # noqa: E402
import world_build as wb  # noqa: E402

CHILD_HOLD_LOCK = """
import sys, time
sys.path.insert(0, sys.argv[1])
import world_build as wb
with wb._exclusive_output(sys.argv[2]):
    with open(sys.argv[3], 'w', encoding='ascii') as fh:
        fh.write('ready')
    time.sleep(30)
"""


class WorldBuildOutputLockTests(unittest.TestCase):
    def test_second_process_refuses_while_first_holds_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets = os.path.join(tmp, "assets")
            out = os.path.join(tmp, "world")
            ready = os.path.join(tmp, "ready")
            twb._write_synthetic_assets(assets, origin_overlap=False)
            proc = subprocess.Popen(
                [sys.executable, "-I", "-c", CHILD_HOLD_LOCK, TOOLS, out, ready],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.time() + 10
                while not os.path.isfile(ready):
                    self.assertIsNone(proc.poll(), "lock holder exited before becoming ready")
                    self.assertLess(time.time(), deadline, "lock holder never became ready")
                    time.sleep(0.05)
                with self.assertRaisesRegex(RuntimeError, "another world_build is using"):
                    wb.build_world(assets, out, textures=False)
                self.assertFalse(os.path.exists(os.path.join(out, "chunks")))
            finally:
                proc.terminate()
                try:
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate(timeout=5)

    def test_build_world_holds_the_output_lock_and_releases_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets = os.path.join(tmp, "assets")
            out = os.path.join(tmp, "world")
            twb._write_synthetic_assets(assets, origin_overlap=False)
            calls = []

            @contextmanager
            def fake_lock(path):
                calls.append(path)
                yield

            with mock.patch.object(wb, "_exclusive_output", fake_lock):
                wb.build_world(assets, out, textures=False)
                wb.build_world(assets, out, textures=False)
            self.assertEqual(calls, [os.path.abspath(out)] * 2)


if __name__ == "__main__":
    unittest.main()
