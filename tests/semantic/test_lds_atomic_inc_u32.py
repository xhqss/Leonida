"""Semantic interpreter tests for 32-bit LDS atomic increment with wrap (ds_inc_u32, ds_inc_rtn_u32).

Standard library only; no GPU, no LLVM, no external tools, no game files.

Run directly:
    python -I tests/semantic/test_lds_atomic_inc_u32.py
Or via unittest discovery:
    python -m unittest discover -s tests/semantic -p "test_lds_atomic_inc_u32.py" -v

Background & Specification Rules:
  * AMD RDNA2 / GFX10 Local Data Share (LDS) Architecture:
    - LDS is an on-chip software-managed scratchpad memory shared across all
      work-items (lanes/threads) in a workgroup.
    - `ds_inc_u32 vaddr, vdata [offset:N]`:
        Non-returning 32-bit unsigned wrapping atomic increment:
          addr = (vaddr + byte_offset) & 0xFFFFFFFF
          dword_idx = (addr >> 2) & (lds_dwords - 1)
          tmp = LDS[dword_idx]
          LDS[dword_idx] = 0 if tmp >= vdata else ((tmp + 1) & 0xFFFFFFFF)
        The prior value in LDS[dword_idx] is discarded; vector registers are untouched.
    - `ds_inc_rtn_u32 vdst, vaddr, vdata [offset:N]`:
        Returning 32-bit unsigned wrapping atomic increment:
          addr = (vaddr + byte_offset) & 0xFFFFFFFF
          dword_idx = (addr >> 2) & (lds_dwords - 1)
          tmp = LDS[dword_idx]
          LDS[dword_idx] = 0 if tmp >= vdata else ((tmp + 1) & 0xFFFFFFFF)
          vdst = tmp
        Returns the pre-operation memory value in destination VGPR vdst.
    - Modulo wrap condition:
        Both current memory value `tmp` and `vdata` (the wrap threshold) are
        treated as unsigned 32-bit integers.
        When `tmp >= vdata`, the value immediately resets/wraps to 0.
        When `tmp < vdata`, the value increments by 1.
    - Active lane masking:
        LDS operations execute per-lane under the wave execution mask (EXEC).
        Inactive lanes neither mutate LDS storage nor alter destination VGPRs.
    - Byte addressing & alignment:
        Byte addresses are converted to dwords via right shift by 2, dropping
        unaligned low bits [1:0]. Out-of-range addresses wrap within the
        allocated power-of-two LDS allocation via `& (lds_dwords - 1)`.
"""
from __future__ import annotations

import os
import random
import re
import sys
import unittest

MASK32 = 0xFFFFFFFF
DEFAULT_LDS_DWORDS = 16384  # 64 KiB default capacity in dwords (16384 * 4 bytes)
DEFAULT_NUM_LANES = 64     # Wave64 default width
DEFAULT_NUM_VGPRS = 256    # Standard GFX10 vector register file size


def reference_atomic_inc_u32(current_val: int, wrap_limit: int) -> tuple[int, int]:
    """Reference ISA calculation for 32-bit LDS atomic increment with wrap.

    Formula:
        tmp = current_val & 0xFFFFFFFF
        limit = wrap_limit & 0xFFFFFFFF
        new_val = 0 if tmp >= limit else (tmp + 1) & 0xFFFFFFFF
        return (tmp, new_val)

    Returns:
        tuple[int, int]: (old_val, updated_mem_val)
    """
    tmp = current_val & MASK32
    limit = wrap_limit & MASK32
    new_val = 0 if tmp >= limit else (tmp + 1) & MASK32
    return tmp, new_val


def calc_lds_byte_offset(
    vaddr: int,
    byte_offset: int = 0,
    lds_dwords: int = DEFAULT_LDS_DWORDS,
) -> tuple[int, int, int]:
    """Reference ISA calculation for single LDS address.

    Args:
        vaddr: Base byte address in VGPR (32-bit).
        byte_offset: Static unsigned immediate byte offset [0..65535].
        lds_dwords: Total number of 32-bit dwords in allocated LDS (power-of-two).

    Returns:
        tuple[int, int, int]: (effective_byte_address, dword_idx, byte_in_dword)
    """
    eff_byte = (vaddr + (byte_offset & 0xFFFF)) & MASK32
    dword_idx = (eff_byte >> 2) & (lds_dwords - 1)
    byte_in_dword = eff_byte & 3
    return eff_byte, dword_idx, byte_in_dword


class LdsSemanticInterpreter:
    """Deterministic multi-lane SIMD semantic interpreter for RDNA2 LDS atomic instructions.

    Models LDS shared memory, per-lane VGPR register files, and wave execution masks.
    """

    def __init__(
        self,
        lds_dwords: int = DEFAULT_LDS_DWORDS,
        num_vgprs: int = DEFAULT_NUM_VGPRS,
        num_lanes: int = DEFAULT_NUM_LANES,
    ):
        if lds_dwords <= 0 or (lds_dwords & (lds_dwords - 1)) != 0:
            raise ValueError(f"lds_dwords must be a positive power-of-two, got {lds_dwords}")
        self.lds_dwords = lds_dwords
        self.lds = [0] * lds_dwords
        self.num_vgprs = num_vgprs
        self.num_lanes = num_lanes
        self.vgprs = [[0] * num_vgprs for _ in range(num_lanes)]
        self.active_lanes = [True] * num_lanes

    def set_vgpr(self, reg_idx: int, value: int, lane: int = 0) -> None:
        if not (0 <= reg_idx < self.num_vgprs):
            raise IndexError(f"VGPR index {reg_idx} out of range [0..{self.num_vgprs - 1}]")
        if not (0 <= lane < self.num_lanes):
            raise IndexError(f"Lane index {lane} out of range [0..{self.num_lanes - 1}]")
        self.vgprs[lane][reg_idx] = value & MASK32

    def get_vgpr(self, reg_idx: int, lane: int = 0) -> int:
        if not (0 <= reg_idx < self.num_vgprs):
            raise IndexError(f"VGPR index {reg_idx} out of range [0..{self.num_vgprs - 1}]")
        if not (0 <= lane < self.num_lanes):
            raise IndexError(f"Lane index {lane} out of range [0..{self.num_lanes - 1}]")
        return self.vgprs[lane][reg_idx]

    def set_lds_dword(self, dword_idx: int, value: int) -> None:
        idx = dword_idx & (self.lds_dwords - 1)
        self.lds[idx] = value & MASK32

    def get_lds_dword(self, dword_idx: int) -> int:
        idx = dword_idx & (self.lds_dwords - 1)
        return self.lds[idx]

    def set_lane_active(self, lane: int, active: bool) -> None:
        if not (0 <= lane < self.num_lanes):
            raise IndexError(f"Lane index {lane} out of range [0..{self.num_lanes - 1}]")
        self.active_lanes[lane] = bool(active)

    def set_exec_mask(self, mask: int) -> None:
        """Sets active lanes based on a 32-bit or 64-bit integer execution mask."""
        for lane in range(self.num_lanes):
            self.active_lanes[lane] = bool((mask >> lane) & 1)

    def ds_write_b32(self, vaddr_reg: int, vdata_reg: int, offset: int = 0, lane: int = 0) -> int | None:
        """Writes 32-bit value into LDS if lane is active."""
        if not self.active_lanes[lane]:
            return None
        vaddr = self.vgprs[lane][vaddr_reg]
        vdata = self.vgprs[lane][vdata_reg]
        _, dword_idx, _ = calc_lds_byte_offset(vaddr, offset, self.lds_dwords)
        self.lds[dword_idx] = vdata & MASK32
        return dword_idx

    def ds_read_b32(self, vdst_reg: int, vaddr_reg: int, offset: int = 0, lane: int = 0) -> int | None:
        """Reads 32-bit value from LDS into vdst if lane is active."""
        if not self.active_lanes[lane]:
            return None
        vaddr = self.vgprs[lane][vaddr_reg]
        _, dword_idx, _ = calc_lds_byte_offset(vaddr, offset, self.lds_dwords)
        self.vgprs[lane][vdst_reg] = self.lds[dword_idx]
        return dword_idx

    def ds_inc_u32(self, vaddr_reg: int, vdata_reg: int, offset: int = 0, lane: int = 0) -> int | None:
        """Non-returning 32-bit atomic increment with wrap.

        Updates LDS[target] = 0 if LDS[target] >= limit else LDS[target] + 1.
        Returns target dword_idx if lane active, or None if inactive.
        """
        if not self.active_lanes[lane]:
            return None
        vaddr = self.vgprs[lane][vaddr_reg]
        wrap_limit = self.vgprs[lane][vdata_reg]
        _, dword_idx, _ = calc_lds_byte_offset(vaddr, offset, self.lds_dwords)
        _, new_val = reference_atomic_inc_u32(self.lds[dword_idx], wrap_limit)
        self.lds[dword_idx] = new_val
        return dword_idx

    def ds_inc_rtn_u32(
        self,
        vdst_reg: int,
        vaddr_reg: int,
        vdata_reg: int,
        offset: int = 0,
        lane: int = 0,
    ) -> tuple[int, int] | None:
        """Returning 32-bit atomic increment with wrap.

        Stores prior memory value into vdst and updates LDS[target].
        Returns (dword_idx, old_val) if lane active, or None if inactive.
        Safely reads sources before writing destination (handles vdst aliasing vaddr/vdata).
        """
        if not self.active_lanes[lane]:
            return None
        vaddr = self.vgprs[lane][vaddr_reg]
        wrap_limit = self.vgprs[lane][vdata_reg]
        _, dword_idx, _ = calc_lds_byte_offset(vaddr, offset, self.lds_dwords)
        old_val, new_val = reference_atomic_inc_u32(self.lds[dword_idx], wrap_limit)
        self.lds[dword_idx] = new_val
        self.vgprs[lane][vdst_reg] = old_val
        return dword_idx, old_val

    def parse_and_execute(self, instruction_text: str, lane: int = 0) -> int | tuple[int, int] | None:
        """Parses a single assembly line and executes it on the specified lane."""
        line = instruction_text.strip()
        if not line or line.startswith(";"):
            return None

        # Parse offset modifier if present (e.g., "offset:16")
        offset = 0
        tokens = line.split()
        filtered_tokens = []
        for tok in tokens:
            if tok.startswith("offset:"):
                offset = int(tok.split(":", 1)[1], 0)
            else:
                filtered_tokens.append(tok)

        text = " ".join(filtered_tokens)
        m = re.match(r"^(\w+)\s+(.+)$", text)
        if not m:
            raise ValueError(f"Malformed instruction line: {instruction_text!r}")
        op, operands_str = m.groups()
        args = [arg.strip().rstrip(",") for arg in operands_str.split(",") if arg.strip()]

        def reg_num(r_str: str) -> int:
            if not r_str.startswith("v") or not r_str[1:].isdigit():
                raise ValueError(f"Expected VGPR like 'v0', got {r_str!r}")
            return int(r_str[1:])

        if op == "ds_inc_u32":
            if len(args) != 2:
                raise ValueError(f"ds_inc_u32 expects 2 arguments (vaddr, vdata), got {len(args)}")
            return self.ds_inc_u32(reg_num(args[0]), reg_num(args[1]), offset=offset, lane=lane)

        if op == "ds_inc_rtn_u32":
            if len(args) != 3:
                raise ValueError(f"ds_inc_rtn_u32 expects 3 arguments (vdst, vaddr, vdata), got {len(args)}")
            return self.ds_inc_rtn_u32(reg_num(args[0]), reg_num(args[1]), reg_num(args[2]), offset=offset, lane=lane)

        if op == "ds_write_b32":
            if len(args) != 2:
                raise ValueError(f"ds_write_b32 expects 2 arguments (vaddr, vdata), got {len(args)}")
            return self.ds_write_b32(reg_num(args[0]), reg_num(args[1]), offset=offset, lane=lane)

        if op == "ds_read_b32":
            if len(args) != 2:
                raise ValueError(f"ds_read_b32 expects 2 arguments (vdst, vaddr), got {len(args)}")
            return self.ds_read_b32(reg_num(args[0]), reg_num(args[1]), offset=offset, lane=lane)

        raise ValueError(f"Unrecognized opcode: {op}")


class TestReferenceAtomicIncOracle(unittest.TestCase):
    """Verifies reference mathematical properties and edge cases of 32-bit ds_inc_u32."""

    def test_basic_increment_below_limit(self):
        # When current < limit, increments by 1
        limit = 10
        for val in range(limit):
            with self.subTest(val=val, limit=limit):
                old_val, new_val = reference_atomic_inc_u32(val, limit)
                self.assertEqual(old_val, val)
                self.assertEqual(new_val, val + 1)

    def test_exact_wrap_at_limit(self):
        # When current == limit, wraps immediately to 0
        limits = [1, 2, 5, 10, 64, 255, 1000, 0x7FFFFFFF, 0xFFFFFFFF]
        for lim in limits:
            with self.subTest(limit=lim):
                old_val, new_val = reference_atomic_inc_u32(lim, lim)
                self.assertEqual(old_val, lim)
                self.assertEqual(new_val, 0)

    def test_above_limit_immediate_wrap(self):
        # When current > limit, wraps immediately to 0 (per AMD ISA (tmp >= DATA) ? 0 : (tmp + 1))
        limit = 5
        for val in [6, 7, 10, 100, 0x12345678, 0xFFFFFFFF]:
            with self.subTest(val=val, limit=limit):
                old_val, new_val = reference_atomic_inc_u32(val, limit)
                self.assertEqual(old_val, val)
                self.assertEqual(new_val, 0)

    def test_wrap_limit_zero(self):
        # When limit is 0: any non-negative unsigned value >= 0 wraps to 0
        for val in [0, 1, 42, 0xFFFFFFFF]:
            with self.subTest(val=val):
                old_val, new_val = reference_atomic_inc_u32(val, 0)
                self.assertEqual(old_val, val)
                self.assertEqual(new_val, 0)

    def test_wrap_limit_one(self):
        # Limit 1: toggles between 0 and 1
        val = 0
        sequence = []
        for _ in range(6):
            old_val, val = reference_atomic_inc_u32(val, 1)
            sequence.append((old_val, val))
        expected = [(0, 1), (1, 0), (0, 1), (1, 0), (0, 1), (1, 0)]
        self.assertEqual(sequence, expected)

    def test_wrap_limit_max_u32(self):
        # Limit 0xFFFFFFFF: counts from 0 up to 0xFFFFFFFF and then wraps to 0
        old_val, new_val = reference_atomic_inc_u32(0xFFFFFFFE, 0xFFFFFFFF)
        self.assertEqual(old_val, 0xFFFFFFFE)
        self.assertEqual(new_val, 0xFFFFFFFF)

        old_val, new_val = reference_atomic_inc_u32(0xFFFFFFFF, 0xFFFFFFFF)
        self.assertEqual(old_val, 0xFFFFFFFF)
        self.assertEqual(new_val, 0)

    def test_counter_sequence_cycle(self):
        # Verifies that repeated increments starting from 0 cycle through 0..L and wrap to 0
        for limit in [2, 3, 5, 16]:
            with self.subTest(limit=limit):
                curr = 0
                history = [curr]
                for _ in range(limit + 1):
                    _, curr = reference_atomic_inc_u32(curr, limit)
                    history.append(curr)
                expected = list(range(limit + 1)) + [0]
                self.assertEqual(history, expected)

    def test_randomized_oracle_invariants(self):
        rng = random.Random(0x1337C0DE)
        for _ in range(200):
            curr = rng.getrandbits(32)
            lim = rng.getrandbits(32)
            old_val, new_val = reference_atomic_inc_u32(curr, lim)
            self.assertEqual(old_val, curr & MASK32)
            if curr >= lim:
                self.assertEqual(new_val, 0)
            else:
                self.assertEqual(new_val, (curr + 1) & MASK32)

    def test_interpreter_ds_inc_u32_basic(self):
        # Non-returning ds_inc_u32 execution in interpreter
        interp = LdsSemanticInterpreter(lds_dwords=1024)
        interp.set_lds_dword(10, 3)
        interp.set_vgpr(1, 10 * 4)  # v1 = byte address (dword 10)
        interp.set_vgpr(2, 5)       # v2 = limit 5

        # Execute ds_inc_u32 v1, v2
        res = interp.parse_and_execute("ds_inc_u32 v1, v2")
        self.assertEqual(res, 10)
        self.assertEqual(interp.get_lds_dword(10), 4)

    def test_interpreter_ds_inc_rtn_u32_basic(self):
        # Returning ds_inc_rtn_u32 execution in interpreter
        interp = LdsSemanticInterpreter(lds_dwords=1024)
        interp.set_lds_dword(20, 5)
        interp.set_vgpr(0, 0xDEADBEEF)  # v0 = initial trash
        interp.set_vgpr(1, 20 * 4)      # v1 = byte address (dword 20)
        interp.set_vgpr(2, 5)           # v2 = limit 5

        # Execute ds_inc_rtn_u32 v0, v1, v2 (at limit, so memory becomes 0, returns old 5)
        res = interp.parse_and_execute("ds_inc_rtn_u32 v0, v1, v2")
        self.assertEqual(res, (20, 5))
        self.assertEqual(interp.get_vgpr(0), 5)
        self.assertEqual(interp.get_lds_dword(20), 0)

    def test_inactive_lanes_no_op(self):
        interp = LdsSemanticInterpreter(lds_dwords=1024)
        interp.set_lds_dword(5, 10)
        interp.set_vgpr(0, 0xCAFEBABE, lane=3)
        interp.set_vgpr(1, 5 * 4, lane=3)
        interp.set_vgpr(2, 20, lane=3)
        interp.set_lane_active(3, False)

        res = interp.parse_and_execute("ds_inc_rtn_u32 v0, v1, v2", lane=3)
        self.assertIsNone(res)
        # LDS must be unchanged
        self.assertEqual(interp.get_lds_dword(5), 10)
        # Destination register must be untouched
        self.assertEqual(interp.get_vgpr(0, lane=3), 0xCAFEBABE)


class TestModuloWrapBoundaryAndBankConflicts(unittest.TestCase):
    """Tests modulo wrap boundaries and SIMD multi-lane bank conflict scenarios."""

    def test_modulo_wrap_boundaries(self):
        interp = LdsSemanticInterpreter(lds_dwords=256)
        limits = [0, 1, 2, 3, 15, 16, 255, 65535]
        for idx, lim in enumerate(limits):
            interp.set_lds_dword(idx, lim)
            interp.set_vgpr(1, idx * 4)
            interp.set_vgpr(2, lim)
            res = interp.parse_and_execute("ds_inc_rtn_u32 v0, v1, v2", lane=0)
            self.assertEqual(res, (idx, lim))
            self.assertEqual(interp.get_lds_dword(idx), 0)

            if lim > 0:
                interp.set_lds_dword(idx, 0)
                interp.set_vgpr(2, lim)
                res2 = interp.parse_and_execute("ds_inc_rtn_u32 v0, v1, v2", lane=0)
                self.assertEqual(res2, (idx, 0))
                self.assertEqual(interp.get_lds_dword(idx), 1)

    def test_simd_multilane_bank_conflicts(self):
        interp = LdsSemanticInterpreter(lds_dwords=1024, num_lanes=64)
        target_dword = 42
        interp.set_lds_dword(target_dword, 0)
        for lane in range(64):
            interp.set_vgpr(1, target_dword * 4, lane=lane)
            interp.set_vgpr(2, 100, lane=lane)

        old_vals = []
        for lane in range(64):
            _, old_v = interp.parse_and_execute("ds_inc_rtn_u32 v0, v1, v2", lane=lane)
            old_vals.append(old_v)

        self.assertEqual(old_vals, list(range(64)))
        self.assertEqual(interp.get_lds_dword(target_dword), 64)

    def test_simd_multilane_staggered_addresses(self):
        interp = LdsSemanticInterpreter(lds_dwords=1024, num_lanes=16)
        for lane in range(16):
            dword = lane * 3
            interp.set_lds_dword(dword, lane * 2)
            interp.set_vgpr(1, dword * 4, lane=lane)
            interp.set_vgpr(2, 50, lane=lane)

        for lane in range(16):
            res = interp.parse_and_execute("ds_inc_rtn_u32 v0, v1, v2", lane=lane)
            self.assertEqual(res[1], lane * 2)
            dword = lane * 3
            self.assertEqual(interp.get_lds_dword(dword), lane * 2 + 1)


class TestReturnValuePreservationAlignmentAndNegativeControls(unittest.TestCase):
    """Tests return value preservation, 32-bit dword alignment, offset immediates, and negative controls."""

    def test_return_value_preservation_and_aliasing(self):
        # Verify returning atomic inc preserves pre-op value in vdst even when vdst aliases vaddr or vdata
        interp = LdsSemanticInterpreter(lds_dwords=1024)
        interp.set_lds_dword(10, 7)
        interp.set_vgpr(5, 40)  # v5 = byte address 40 (dword 10)
        interp.set_vgpr(6, 10)  # v6 = wrap limit 10

        # Execute ds_inc_rtn_u32 v5, v5, v6 (vdst aliases vaddr)
        # Should read old value 7 into v5 and update memory to 8
        res = interp.parse_and_execute("ds_inc_rtn_u32 v5, v5, v6")
        self.assertEqual(res, (10, 7))
        self.assertEqual(interp.get_vgpr(5), 7)
        self.assertEqual(interp.get_lds_dword(10), 8)

    def test_alignment_and_offset_immediates(self):
        # Verify byte addressing and offset addition
        interp = LdsSemanticInterpreter(lds_dwords=1024)
        interp.set_lds_dword(100, 2)
        interp.set_vgpr(1, 384)       # base byte address 384 (dword 96)
        interp.set_vgpr(2, 5)         # limit 5

        # offset:16 adds 16 bytes -> 384 + 16 = 400 bytes -> dword 100
        res = interp.parse_and_execute("ds_inc_rtn_u32 v0, v1, v2 offset:16")
        self.assertEqual(res, (100, 2))
        self.assertEqual(interp.get_lds_dword(100), 3)

    def test_unaligned_low_bits_ignored(self):
        # Unaligned low bits [1:0] of effective address are truncated (right shift by 2)
        interp = LdsSemanticInterpreter(lds_dwords=1024)
        interp.set_lds_dword(50, 4)
        interp.set_vgpr(1, 201)       # 201 bytes -> dword index 201 >> 2 = 50 (remainder 1 ignored)
        interp.set_vgpr(2, 10)

        res = interp.parse_and_execute("ds_inc_rtn_u32 v0, v1, v2")
        self.assertEqual(res, (50, 4))
        self.assertEqual(interp.get_lds_dword(50), 5)

    def test_negative_controls_malformed_instructions(self):
        interp = LdsSemanticInterpreter(lds_dwords=1024)
        with self.assertRaises(ValueError):
            interp.parse_and_execute("ds_inc_u32 v1")  # too few arguments
        with self.assertRaises(ValueError):
            interp.parse_and_execute("ds_inc_rtn_u32 v0, v1")  # too few arguments
        with self.assertRaises(ValueError):
            interp.parse_and_execute("ds_unknown_op v0, v1, v2")  # invalid opcode
        with self.assertRaises(ValueError):
            interp.parse_and_execute("ds_inc_u32 r1, v2")  # invalid register syntax (not vN)

    def test_negative_controls_index_out_of_bounds(self):
        interp = LdsSemanticInterpreter(lds_dwords=256)
        with self.assertRaises(IndexError):
            interp.set_vgpr(300, 10)  # VGPR index out of range [0..255]
        with self.assertRaises(IndexError):
            interp.get_vgpr(300, 0)
        with self.assertRaises(IndexError):
            interp.set_lane_active(100, True)  # lane index out of range [0..63]
        with self.assertRaises(ValueError):
            LdsSemanticInterpreter(lds_dwords=0)  # non-positive lds dwords
        with self.assertRaises(ValueError):
            LdsSemanticInterpreter(lds_dwords=100)  # non power-of-two lds dwords


if __name__ == "__main__":
    unittest.main()
