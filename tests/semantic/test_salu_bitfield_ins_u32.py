"""Semantic interpreter tests for RDNA2 scalar 32-bit bitfield insert (s_bfi_b32).

Covers:
  - RDNA2 Scalar ALU Bitfield Architecture:
      * s_bfi_b32 dst, mask, insert, base
        Scalar 32-bit bitfield insert using bitmask selection:
        dst = (mask & insert) | (~mask & base)
        SCC condition code: SCC = 1 if dst != 0 else 0.
      * s_bfm_b32 dst, width, offset
        Scalar bitfield mask generation:
        dst = ((1 << (width & 31)) - 1) << (offset & 31)
        If (width & 31) == 0, dst = 0.
        Note: s_bfm_b32 does NOT alter the SCC flag.
      * Canonical SALU bitfield insert composition:
        mask = s_bfm_b32(width, offset)
        field = s_lshl_b32(insert, offset)
        masked_field = s_and_b32(field, mask)
        cleared_base = s_andn2_b32(base, mask)  (or s_not_b32 + s_and_b32)
        dst = s_or_b32(cleared_base, masked_field)
  - Verification layers:
      1. Pure Python reference mathematical oracles.
      2. Simulated SALU register file interpreter (ScalarAluInterpreter).
      3. Register routing, SCC updates, and mask composition pipelines.
      4. Clamp boundary testing, SGPR aliasing, and negative controls.

Zero game data, standard library Python only, Python 3.9+ compatible.
"""
from __future__ import annotations

import os
import random
import struct
import sys
import unittest
import warnings

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TOOLS_DIR = os.environ.get("LEONIDA_TOOLS_DIR") or os.path.join(ROOT, "tools")

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import rdna2spirv  # noqa: E402
import spirv_mini_interp as si  # noqa: E402
from support import STUB_DXBC, mrt0_words, translate_lines  # noqa: E402

MASK32 = 0xFFFFFFFF


def u32(val: int) -> int:
    """Normalize integer to 32-bit unsigned dword."""
    return val & MASK32


def pack_bfe_ctl(offset: int, width: int) -> int:
    """Pack bitfield offset [4:0] and width [22:16] into an RDNA scalar control word."""
    return ((width & 0x7F) << 16) | (offset & 0x1F)


def unpack_bfe_ctl(ctl: int) -> tuple[int, int]:
    """Unpack offset [4:0] and width [22:16] from an RDNA scalar control word."""
    offset = ctl & 0x1F
    width = (ctl >> 16) & 0x7F
    return offset, width


def ref_s_bfi_b32(mask: int, insert: int, base: int) -> tuple[int, int]:
    """Pure-Python reference oracle for RDNA2 scalar 32-bit bitfield insert (s_bfi_b32).

    dst = (mask & insert) | (~mask & base)
    SCC = 1 if dst != 0 else 0

    Returns:
        (result, scc): 32-bit unsigned result and 1-bit SCC flag.
    """
    m = mask & MASK32
    ins = insert & MASK32
    b = base & MASK32
    res = ((m & ins) | ((~m & MASK32) & b)) & MASK32
    scc = 1 if res != 0 else 0
    return res, scc


def ref_s_bfm_b32(width: int, offset: int) -> int:
    """Pure-Python reference oracle for RDNA2 s_bfm_b32 mask generation.

    dst = ((1 << (width & 31)) - 1) << (offset & 31)
    If (width & 31) == 0, dst = 0.
    Does not modify SCC.
    """
    w = width & 31
    o = offset & 31
    if w == 0:
        return 0
    mask = ((1 << w) - 1) & MASK32
    return (mask << o) & MASK32


def ref_salu_bitfield_insert(base: int, insert: int, offset: int, width: int) -> tuple[int, int]:
    """Canonical scalar bitfield insert of a value at [offset +: width].

    Generates mask using s_bfm_b32, shifts insert field into position, and
    performs bitwise insertion into base.
    Returns:
        (result, scc)
    """
    mask = ref_s_bfm_b32(width, offset)
    field = (insert << (offset & 31)) & mask
    return ref_s_bfi_b32(mask, field, base)


class ScalarAluInterpreter:
    """Deterministic simulated RDNA2 SALU machine with SGPR file and SCC state.

    Models:
      - SGPR file: s0 .. s105 (106 registers).
      - SCC: 1-bit Scalar Condition Code.
    """

    def __init__(self, num_sgprs: int = 106) -> None:
        self.num_sgprs = num_sgprs
        self.sgpr = [0] * num_sgprs
        self.scc = 0

    def reset(self) -> None:
        self.sgpr = [0] * self.num_sgprs
        self.scc = 0

    def get_sgpr(self, r: int) -> int:
        return self.sgpr[r] & MASK32

    def set_sgpr(self, r: int, val: int) -> None:
        self.sgpr[r] = val & MASK32

    def resolve_src(self, opnd: int | dict | str) -> int:
        """Resolve scalar operand to 32-bit unsigned value."""
        if isinstance(opnd, int):
            return opnd & MASK32
        if isinstance(opnd, dict):
            k = opnd.get("k")
            if k == "imm":
                return opnd["v"] & MASK32
            if k == "sgpr":
                return self.get_sgpr(opnd["r"])
            raise ValueError(f"Unknown operand dict: {opnd}")
        if isinstance(opnd, str):
            s = opnd.strip()
            if s.startswith("s"):
                return self.get_sgpr(int(s[1:]))
            return int(s, 0) & MASK32
        raise TypeError(f"Unsupported operand type: {type(opnd)}")

    def resolve_dst(self, dst: int | dict | str) -> int:
        """Resolve destination operand to SGPR index."""
        if isinstance(dst, int):
            return dst
        if isinstance(dst, dict) and dst.get("k") == "sgpr":
            return dst["r"]
        if isinstance(dst, str):
            s = dst.strip()
            if s.startswith("s"):
                return int(s[1:])
        raise ValueError(f"Unsupported destination operand: {dst}")

    def step(self, op: str, dst: int | dict | str, *args: int | dict | str) -> int:
        """Execute a single SALU instruction, updating state and SCC where defined."""
        d_idx = self.resolve_dst(dst)
        srcs = [self.resolve_src(a) for a in args]

        if op == "s_bfi_b32":
            # args: mask, insert, base
            if len(srcs) != 3:
                raise ValueError(f"s_bfi_b32 requires 3 sources, got {len(srcs)}")
            res, scc = ref_s_bfi_b32(srcs[0], srcs[1], srcs[2])
            self.set_sgpr(d_idx, res)
            self.scc = scc
            return res

        if op == "s_bfm_b32":
            # args: width, offset
            if len(srcs) != 2:
                raise ValueError(f"s_bfm_b32 requires 2 sources, got {len(srcs)}")
            res = ref_s_bfm_b32(srcs[0], srcs[1])
            self.set_sgpr(d_idx, res)
            # s_bfm_b32 does NOT alter SCC
            return res

        if op == "s_mov_b32":
            res = srcs[0]
            self.set_sgpr(d_idx, res)
            # s_mov_b32 does NOT alter SCC
            return res

        if op == "s_and_b32":
            res = (srcs[0] & srcs[1]) & MASK32
            self.set_sgpr(d_idx, res)
            self.scc = 1 if res != 0 else 0
            return res

        if op == "s_andn2_b32":
            res = (srcs[0] & (~srcs[1] & MASK32)) & MASK32
            self.set_sgpr(d_idx, res)
            self.scc = 1 if res != 0 else 0
            return res

        if op == "s_or_b32":
            res = (srcs[0] | srcs[1]) & MASK32
            self.set_sgpr(d_idx, res)
            self.scc = 1 if res != 0 else 0
            return res

        if op == "s_xor_b32":
            res = (srcs[0] ^ srcs[1]) & MASK32
            self.set_sgpr(d_idx, res)
            self.scc = 1 if res != 0 else 0
            return res

        if op == "s_not_b32":
            res = (~srcs[0]) & MASK32
            self.set_sgpr(d_idx, res)
            self.scc = 1 if res != 0 else 0
            return res

        if op == "s_lshl_b32":
            sh = srcs[1] & 31
            res = (srcs[0] << sh) & MASK32
            self.set_sgpr(d_idx, res)
            self.scc = 1 if res != 0 else 0
            return res

        if op == "s_lshr_b32":
            sh = srcs[1] & 31
            res = (srcs[0] >> sh) & MASK32
            self.set_sgpr(d_idx, res)
            self.scc = 1 if res != 0 else 0
            return res

        if op == "s_cmp_eq_u32":
            self.scc = 1 if srcs[0] == srcs[1] else 0
            return self.scc

        if op == "s_cselect_b32":
            res = srcs[0] if self.scc != 0 else srcs[1]
            self.set_sgpr(d_idx, res)
            return res

        raise NotImplementedError(f"Instruction {op} not implemented in ScalarAluInterpreter")


class ReferenceOracleTests(unittest.TestCase):
    """Validation of pure-Python reference oracles for s_bfi_b32 and s_bfm_b32."""

    def test_ref_bfi_all_zero_mask_returns_base(self) -> None:
        bases = [0x00000000, 0xFFFFFFFF, 0x12345678, 0xCAFEBABE, 0x80000001]
        for b in bases:
            res, scc = ref_s_bfi_b32(0, 0xDEADBEEF, b)
            self.assertEqual(res, b)
            self.assertEqual(scc, 1 if b != 0 else 0)

    def test_ref_bfi_all_ones_mask_returns_insert(self) -> None:
        inserts = [0x00000000, 0xFFFFFFFF, 0x55555555, 0xAAAA0000, 0x00000001]
        for ins in inserts:
            res, scc = ref_s_bfi_b32(MASK32, ins, 0x12345678)
            self.assertEqual(res, ins)
            self.assertEqual(scc, 1 if ins != 0 else 0)

    def test_ref_bfi_identity_when_insert_equals_base(self) -> None:
        values = [0x00000000, 0xFFFFFFFF, 0x13579BDF, 0x80000000, 0x000000FF]
        masks = [0x00000000, 0xFFFFFFFF, 0x00FF00FF, 0xF0F0F0F0, 0x12345678]
        for val in values:
            for m in masks:
                res, scc = ref_s_bfi_b32(m, val, val)
                self.assertEqual(res, val)
                self.assertEqual(scc, 1 if val != 0 else 0)

    def test_ref_bfi_mask_duality(self) -> None:
        mask = 0xAA55AA55
        inv_mask = (~mask) & MASK32
        s1 = 0x12345678
        s2 = 0x87654321
        res1, scc1 = ref_s_bfi_b32(mask, s1, s2)
        res2, scc2 = ref_s_bfi_b32(inv_mask, s2, s1)
        self.assertEqual(res1, res2)
        self.assertEqual(scc1, scc2)

    def test_ref_bfi_single_bit_walking(self) -> None:
        for bit in range(32):
            mask = 1 << bit
            # With mask selecting this bit from insert=1 and base=0
            res, scc = ref_s_bfi_b32(mask, MASK32, 0)
            self.assertEqual(res, 1 << bit)
            self.assertEqual(scc, 1)

            # With insert=0 and base=all 1s
            res_cleared, scc_cleared = ref_s_bfi_b32(mask, 0, MASK32)
            self.assertEqual(res_cleared, (~mask) & MASK32)
            self.assertEqual(scc_cleared, 1)

    def test_ref_bfi_nibble_and_byte_lanes(self) -> None:
        # Swap low and high bytes using alternating mask
        mask = 0x00FF00FF
        ins = 0x12345678
        base = 0x87654321
        res, scc = ref_s_bfi_b32(mask, ins, base)
        # expected: byte0 and byte2 from ins (78 and 34), byte1 and byte3 from base (43 and 87)
        self.assertEqual(res, 0x87344378)
        self.assertEqual(scc, 1)

    def test_ref_bfm_b32_mask_generation(self) -> None:
        test_cases = [
            (0, 0, 0x00000000),
            (1, 0, 0x00000001),
            (1, 31, 0x80000000),
            (8, 0, 0x000000FF),
            (8, 8, 0x0000FF00),
            (8, 16, 0x00FF0000),
            (8, 24, 0xFF000000),
            (16, 0, 0x0000FFFF),
            (16, 16, 0xFFFF0000),
            (31, 0, 0x7FFFFFFF),
            (32, 0, 0x00000000),  # width & 31 == 0
            (8 + 32, 8 + 64, 0x0000FF00),  # modulo 32
        ]
        for w, o, expected in test_cases:
            with self.subTest(w=w, o=o):
                self.assertEqual(ref_s_bfm_b32(w, o), expected)

    def test_ref_salu_bitfield_insert_fields(self) -> None:
        base = 0x12345678
        # Insert 8 bits at offset 8
        res, scc = ref_salu_bitfield_insert(base, 0xAB, 8, 8)
        self.assertEqual(res, 0x1234AB78)
        self.assertEqual(scc, 1)

        # Insert 0 bits at offset 8 (width=0) -> base unchanged
        res0, scc0 = ref_salu_bitfield_insert(base, 0xFF, 8, 0)
        self.assertEqual(res0, base)
        self.assertEqual(scc0, 1)

        # Insert into zero base
        res_z, scc_z = ref_salu_bitfield_insert(0, 0x5, 4, 4)
        self.assertEqual(res_z, 0x00000050)
        self.assertEqual(scc_z, 1)

        # Clear field by inserting 0 into 0
        res_zero, scc_zero = ref_salu_bitfield_insert(0, 0, 0, 8)
        self.assertEqual(res_zero, 0)
        self.assertEqual(scc_zero, 0)


class ScalarAluInterpreterBasicTests(unittest.TestCase):
    """Execution tests for the ScalarAluInterpreter."""

    def setUp(self) -> None:
        self.interp = ScalarAluInterpreter()

    def test_interpreter_step_s_bfi_b32(self) -> None:
        self.interp.set_sgpr(1, 0x00FF0000)
        self.interp.set_sgpr(2, 0x12345678)
        self.interp.set_sgpr(3, 0xA0B0C0D0)
        res = self.interp.step("s_bfi_b32", "s0", "s1", "s2", "s3")
        self.assertEqual(res, 0xA034C0D0)
        self.assertEqual(self.interp.get_sgpr(0), 0xA034C0D0)
        self.assertEqual(self.interp.scc, 1)

    def test_interpreter_step_s_bfm_b32_preserves_scc(self) -> None:
        for init_scc in (0, 1):
            self.interp.scc = init_scc
            self.interp.set_sgpr(1, 8)
            self.interp.set_sgpr(2, 16)
            res = self.interp.step("s_bfm_b32", "s0", "s1", "s2")
            self.assertEqual(res, 0x00FF0000)
            self.assertEqual(self.interp.get_sgpr(0), 0x00FF0000)
            self.assertEqual(self.interp.scc, init_scc)

    def test_interpreter_reset(self) -> None:
        self.interp.set_sgpr(0, 0x12345678)
        self.interp.scc = 1
        self.interp.reset()
        self.assertEqual(self.interp.get_sgpr(0), 0)
        self.assertEqual(self.interp.scc, 0)


class MaskCompositionAndRoutingTests(unittest.TestCase):
    """Tests for mask composition, SCC updates, and register routing."""

    def setUp(self) -> None:
        self.interp = ScalarAluInterpreter()

    def test_mask_composition_pipeline(self) -> None:
        # s_bfm_b32 width=12, offset=4 -> mask = 0x0000FFF0
        self.interp.set_sgpr(10, 12)
        self.interp.set_sgpr(11, 4)
        self.interp.step("s_bfm_b32", "s2", "s10", "s11")
        self.assertEqual(self.interp.get_sgpr(2), 0x0000FFF0)

        # insert=0xABC shifted by 4 -> 0x0000ABC0, masked -> 0x0000ABC0
        self.interp.set_sgpr(12, 0xABC)
        self.interp.step("s_lshl_b32", "s3", "s12", "s11")
        self.assertEqual(self.interp.get_sgpr(3), 0x0000ABC0)

        # base=0x12345678
        self.interp.set_sgpr(4, 0x12345678)

        # s_bfi_b32 s0, s2 (mask), s3 (insert), s4 (base)
        res = self.interp.step("s_bfi_b32", "s0", "s2", "s3", "s4")
        self.assertEqual(res, 0x1234ABC8)
        self.assertEqual(self.interp.scc, 1)

    def test_scc_updates_across_operations(self) -> None:
        # s_bfm_b32 does NOT alter SCC
        self.interp.scc = 1
        self.interp.set_sgpr(1, 0)
        self.interp.set_sgpr(2, 0)
        self.interp.step("s_bfm_b32", "s0", "s1", "s2")
        self.assertEqual(self.interp.scc, 1)

        # s_bfi_b32 sets SCC = 1 when result != 0
        self.interp.step("s_bfi_b32", "s0", 0xFFFFFFFF, 0xFFFFFFFF, 0)
        self.assertEqual(self.interp.scc, 1)

        # s_bfi_b32 sets SCC = 0 when result == 0
        self.interp.step("s_bfi_b32", "s0", 0, 0, 0)
        self.assertEqual(self.interp.scc, 0)

    def test_register_routing_and_sgpr_independence(self) -> None:
        # Populate multiple SGPRs and ensure operations route correctly between specified registers
        for r in range(10, 16):
            self.interp.set_sgpr(r, (r * 0x11111111) & MASK32)

        # s_bfi_b32 s5, s12, s13, s14
        expected_res, expected_scc = ref_s_bfi_b32(
            self.interp.get_sgpr(12),
            self.interp.get_sgpr(13),
            self.interp.get_sgpr(14)
        )
        self.interp.step("s_bfi_b32", "s5", "s12", "s13", "s14")
        self.assertEqual(self.interp.get_sgpr(5), expected_res)
        self.assertEqual(self.interp.scc, expected_scc)

        # Verify other SGPRs remain untouched
        for r in range(10, 16):
            if r == 5:
                continue
            self.assertEqual(self.interp.get_sgpr(r), (r * 0x11111111) & MASK32)


class ClampBoundaryAndNegativeTests(unittest.TestCase):
    """Tests for width/offset clamp boundaries, register aliasing, and negative controls."""

    def setUp(self) -> None:
        self.interp = ScalarAluInterpreter()

    def test_width_offset_modulo_32_clamping(self) -> None:
        # Width and offset are masked to 5 bits ([4:0]) in s_bfm_b32 / shifts
        # width = 32 (32 & 31 = 0) -> mask = 0
        self.assertEqual(ref_s_bfm_b32(32, 0), 0x00000000)
        # width = 64 (64 & 31 = 0) -> mask = 0
        self.assertEqual(ref_s_bfm_b32(64, 4), 0x00000000)
        # offset = 32 (32 & 31 = 0)
        self.assertEqual(ref_s_bfm_b32(8, 32), 0x000000FF)
        # offset = 33 (33 & 31 = 1)
        self.assertEqual(ref_s_bfm_b32(8, 33), 0x000001FE)

    def test_in_place_register_aliasing(self) -> None:
        # Destination same as base (s1 = s_bfi_b32(mask, insert, s1))
        self.interp.set_sgpr(1, 0x12345678)
        self.interp.set_sgpr(2, 0x00FF0000)
        self.interp.set_sgpr(3, 0xA0B0C0D0)
        self.interp.step("s_bfi_b32", "s1", "s2", "s3", "s1")
        self.assertEqual(self.interp.get_sgpr(1), 0x12b05678)

    def test_negative_controls_invalid_operand_counts(self) -> None:
        with self.assertRaises(ValueError):
            self.interp.step("s_bfi_b32", "s0", "s1", "s2")  # Missing 3rd source
        with self.assertRaises(ValueError):
            self.interp.step("s_bfm_b32", "s0", "s1")  # Needs 2 sources, test passing 1

    def test_negative_controls_unsupported_instruction(self) -> None:
        with self.assertRaises(NotImplementedError):
            self.interp.step("s_bogus_op_b32", "s0", "s1")


if __name__ == "__main__":
    unittest.main()
