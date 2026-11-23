"""CPU tests for tools/mad_denorm_probe.py (SHADER-340): RDNA1 mad/mac pre- and post-omod denormal flush.
Standard library only; no GPU, no LLVM, no game files.
"""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(ROOT, "tools"))

import mad_denorm_probe as probe  # noqa: E402
from hw_oracle import runner as hw_runner  # noqa: E402


class TranslatorTests(unittest.TestCase):
    def test_translator_matches_rdna1_expected_words(self):
        got = probe.run_translator()
        self.assertEqual(len(got), len(probe.CASES))
        by_what = {c.what: w for c, w in zip(probe.CASES, got)}

        # Pre-omod sum flush: subnormal sum 2^-127 is flushed to +0 before mul:2 / mul:4.
        self.assertEqual(by_what["(1.5*2^-126 - 2^-126) mul:2"], probe.POS_ZERO)
        self.assertEqual(by_what["(1.5*2^-126 - 2^-126) mul:4"], probe.POS_ZERO)
        self.assertEqual(by_what["legacy (1.5*2^-126 - 2^-126) mul:2"], probe.POS_ZERO)

        # Post-omod flush: FLT_MIN div:2 and -FLT_MIN div:2 flush with sign kept.
        self.assertEqual(by_what["FLT_MIN div:2 -> +0"], probe.POS_ZERO)
        self.assertEqual(by_what["-FLT_MIN div:2 -> -0"], probe.NEG_ZERO)
        self.assertEqual(by_what["-FLT_MIN clamp div:2 -> +0"], probe.POS_ZERO)
        self.assertEqual(by_what["legacy FLT_MIN div:2 -> +0"], probe.POS_ZERO)

        # Subnormal product flush and legacy 0 * anything -> +0 after input flush.
        self.assertEqual(by_what["2^-126 * 0.5 + 2^-126 (prod flushed)"], probe.FLT_MIN)
        self.assertEqual(by_what["mac 2^-126 * 0.5 + 2^-126"], probe.FLT_MIN)
        self.assertEqual(by_what["subnorm * +inf + -1 -> -1"], probe.NEG_ONE)
        self.assertEqual(by_what["subnorm * qNaN + 1 -> 1"], probe.ONE)

    def test_mode_sanity_rejects_broken_denorm_control(self):
        class BrokenModeOracle:
            device = "fake"
            arch = "gfx1010"

            def run(self, kernels, words=1):
                return [[probe.POS_ZERO] for _ in kernels]

            def close(self):
                pass

        with mock.patch.object(hw_runner, "open_oracle", return_value=BrokenModeOracle()):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = probe.main(["--oracle", "hw"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
