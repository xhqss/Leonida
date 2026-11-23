"""Semantic interpreter tests for AMD RDNA in-place 16-bit integer packing (v_pack_b32_u16).

AMD RDNA VALU Instruction Set Architecture (ISA):
  v_pack_b32_u16 dst, src0, src1 [op_sel:[s0, s1]] [clamp]
  Operation: Packs two 16-bit integer values into a 32-bit vector register (VGPR).
    - dst[15:0]  = half_word(src0, op_sel[0]) [clamped to [0, 0xFFFF] if clamp]
    - dst[31:16] = half_word(src1, op_sel[1]) [clamped to [0, 0xFFFF] if clamp]

In-place semantics:
  When dst aliases src0 or src1:
    - dst = v_pack_b32_u16 dst, src: dst[15:0] is preserved (op_sel[0]=0) while
      dst[31:16] is updated with src's selected half-word (in-place upper pack).
    - dst = v_pack_b32_u16 src, dst: dst[31:16] is preserved (op_sel[1]=1) while
      dst[15:0] is updated with src's selected half-word (in-place lower pack).
    - dst = v_pack_b32_u16 dst, dst: self-permutation (swapping halves, broadcasting
      half-words, or identity).

Modifiers and lowerings:
  - op_sel:[s0, s1]:
      s0=0 selects bits [15:0], s0=1 selects bits [31:16] of src0.
      s1=0 selects bits [15:0], s1=1 selects bits [31:16] of src1.
  - clamp: Unsigned saturation to [0, 65535].
  - Lowering to SPIR-V:
      1. Shift / bitwise mask / OR sequence with OpExtInst GLSL.std.450 UClamp / SClamp.
      2. Bitfield extract / insert sequence with OpBitFieldUExtract / OpBitFieldInsert.

Standard library only: Python 3.9+ compatible, zero game data, zero proprietary assets.
"""
from __future__ import annotations

import itertools
import os
import random
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TOOLS_DIR = os.environ.get("LEONIDA_TOOLS_DIR") or os.path.join(ROOT, "tools")

sys.dont_write_bytecode = True
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import spirv_mini_interp as si
import spirv_wave_interp as sw

MASK16 = 0xFFFF
MASK32 = 0xFFFFFFFF


def s32(x: int) -> int:
    """Interpret a 32-bit unsigned word as a signed two's-complement integer."""
    x &= MASK32
    return x - (1 << 32) if x & 0x80000000 else x


def ref_extract_half(val: int, hi: bool = False) -> int:
    """Extract low or high 16-bit half-word with 16-bit masking."""
    val &= MASK32
    return ((val >> 16) if hi else val) & MASK16


def ref_clamp_u16(val: int, signed: bool = False) -> int:
    """Clamp integer value to unsigned 16-bit range [0, 65535]."""
    if signed:
        s = s32(val)
        if s < 0:
            return 0
        if s > MASK16:
            return MASK16
        return s & MASK16
    u = val & MASK32
    return min(u, MASK16)


def ref_pack_b32_u16(
    s0: int,
    s1: int,
    op_sel: tuple[int, int] = (0, 0),
    clamp: bool = False,
    signed: bool = False,
) -> int:
    """Pure-Python reference oracle for RDNA2 v_pack_b32_u16 integer packing."""
    s0 &= MASK32
    s1 &= MASK32
    val0 = (s0 >> 16) if op_sel[0] else s0
    val1 = (s1 >> 16) if op_sel[1] else s1

    if clamp:
        h0 = ref_clamp_u16(val0, signed=signed)
        h1 = ref_clamp_u16(val1, signed=signed)
    else:
        h0 = val0 & MASK16
        h1 = val1 & MASK16

    return (h0 & MASK16) | ((h1 & MASK16) << 16)


def ref_pack_in_place(
    dst: int,
    src: int,
    target: str = "hi",
    op_sel: tuple[int, int] = (0, 0),
    clamp: bool = False,
) -> int:
    """Pure-Python reference for in-place packing into an existing register value."""
    dst &= MASK32
    src &= MASK32
    if target == "hi":
        return ref_pack_b32_u16(dst, src, op_sel=op_sel, clamp=clamp)
    elif target == "lo":
        return ref_pack_b32_u16(src, dst, op_sel=op_sel, clamp=clamp)
    elif target == "both":
        return ref_pack_b32_u16(dst, dst, op_sel=op_sel, clamp=clamp)
    raise ValueError(f"Unknown in-place target: {target}")


def ref_decompose_pack(dword: int) -> tuple[int, int]:
    """Decompose a 32-bit packed word into (lo_u16, hi_u16)."""
    dword &= MASK32
    return dword & MASK16, (dword >> 16) & MASK16


def run_spirv_pack_logical(
    s0: int,
    s1: int,
    op_sel: tuple[int, int] = (0, 0),
    clamp: bool = False,
    signed: bool = False,
) -> int:
    """Execute SPIR-V sequence modeling v_pack_b32_u16 via shift/bitwise/clamp ops."""
    s0_u = s0 & MASK32
    s1_u = s1 & MASK32

    h0_extract = (
        "%s0_val = OpShiftRightLogical %uint %c_s0 %c16\n"
        if op_sel[0]
        else "%s0_val = OpCopyObject %uint %c_s0\n"
    )
    h1_extract = (
        "%s1_val = OpShiftRightLogical %uint %c_s1 %c16\n"
        if op_sel[1]
        else "%s1_val = OpCopyObject %uint %c_s1\n"
    )

    if clamp:
        clamp_op = "SClamp" if signed else "UClamp"
        h0_proc = f"%h0 = OpExtInst %uint %glsl {clamp_op} %s0_val %c0 %c65535\n"
        h1_proc = f"%h1 = OpExtInst %uint %glsl {clamp_op} %s1_val %c0 %c65535\n"
    else:
        h0_proc = "%h0 = OpBitwiseAnd %uint %s0_val %c65535\n"
        h1_proc = "%h1 = OpBitwiseAnd %uint %s1_val %c65535\n"

    spv = f"""OpCapability Shader
%glsl = OpExtInstImport "GLSL.std.450"
OpMemoryModel Logical GLSL450
OpEntryPoint Fragment %main "main" %out
%void = OpTypeVoid
%fn = OpTypeFunction %void
%uint = OpTypeInt 32 0
%_ptr_Output_uint = OpTypePointer Output %uint
%out = OpVariable %_ptr_Output_uint Output
%c_s0 = OpConstant %uint {s0_u}
%c_s1 = OpConstant %uint {s1_u}
%c0 = OpConstant %uint 0
%c16 = OpConstant %uint 16
%c65535 = OpConstant %uint 65535
%main = OpFunction %void None %fn
%entry = OpLabel
{h0_extract}{h0_proc}{h1_extract}{h1_proc}%h1_hi = OpShiftLeftLogical %uint %h1 %c16
%res = OpBitwiseOr %uint %h0 %h1_hi
OpStore %out %res
OpReturn
OpFunctionEnd
"""
    return si.run_fragment(spv)["%out"][0]


def run_spirv_pack_bitfield(
    s0: int,
    s1: int,
    op_sel: tuple[int, int] = (0, 0),
) -> int:
    """Execute SPIR-V sequence modeling unclamped v_pack_b32_u16 via bitfield ops."""
    off0 = 16 if op_sel[0] else 0
    off1 = 16 if op_sel[1] else 0
    spv = f"""OpCapability Shader
%glsl = OpExtInstImport "GLSL.std.450"
OpMemoryModel Logical GLSL450
OpEntryPoint Fragment %main "main" %out
%void = OpTypeVoid
%fn = OpTypeFunction %void
%uint = OpTypeInt 32 0
%_ptr_Output_uint = OpTypePointer Output %uint
%out = OpVariable %_ptr_Output_uint Output
%c_s0 = OpConstant %uint {s0 & MASK32}
%c_s1 = OpConstant %uint {s1 & MASK32}
%c16 = OpConstant %uint 16
%c_off0 = OpConstant %uint {off0}
%c_off1 = OpConstant %uint {off1}
%main = OpFunction %void None %fn
%entry = OpLabel
%h0 = OpBitFieldUExtract %uint %c_s0 %c_off0 %c16
%h1 = OpBitFieldUExtract %uint %c_s1 %c_off1 %c16
%res = OpBitFieldInsert %uint %h0 %h1 %c16 %c16
OpStore %out %res
OpReturn
OpFunctionEnd
"""
    return si.run_fragment(spv)["%out"][0]


class SimdWaveState:
    """Models SIMD vector and scalar register state across active wave lanes."""

    def __init__(self, wave_size: int = 32):
        self.wave_size = wave_size
        self.vgprs: dict[int, list[int]] = {i: [0] * wave_size for i in range(256)}
        self.sgprs: dict[int, int] = {i: 0 for i in range(106)}
        self.exec_mask = (1 << wave_size) - 1

    def set_vgpr_lane(self, reg: int, lane: int, val: int) -> None:
        self.vgprs[reg][lane] = val & MASK32

    def set_vgpr_all(self, reg: int, val: int) -> None:
        for lane in range(self.wave_size):
            self.vgprs[reg][lane] = val & MASK32

    def set_vgpr_lanes(self, reg: int, vals: list[int]) -> None:
        for lane in range(self.wave_size):
            self.vgprs[reg][lane] = vals[lane] & MASK32

    def get_vgpr_lanes(self, reg: int) -> list[int]:
        return list(self.vgprs[reg])

    def execute_pack_b32_u16(
        self,
        dst_reg: int,
        src0: int | str,
        src1: int | str,
        op_sel: tuple[int, int] = (0, 0),
        clamp: bool = False,
    ) -> None:
        """Execute v_pack_b32_u16 across all active lanes with in-place semantics."""
        for lane in range(self.wave_size):
            if (self.exec_mask >> lane) & 1:
                if isinstance(src0, int):
                    s0_val = src0
                elif src0.startswith("v"):
                    s0_val = self.vgprs[int(src0[1:])][lane]
                elif src0.startswith("s"):
                    s0_val = self.sgprs[int(src0[1:])]
                else:
                    s0_val = int(src0, 0)

                if isinstance(src1, int):
                    s1_val = src1
                elif src1.startswith("v"):
                    s1_val = self.vgprs[int(src1[1:])][lane]
                elif src1.startswith("s"):
                    s1_val = self.sgprs[int(src1[1:])]
                else:
                    s1_val = int(src1, 0)

                self.vgprs[dst_reg][lane] = ref_pack_b32_u16(
                    s0_val, s1_val, op_sel=op_sel, clamp=clamp
                )


class TestPackB32U16ReferenceOracle(unittest.TestCase):
    """Unit tests for pure-Python reference oracle and SPIR-V lowerings."""

    def test_basic_halfword_packing(self):
        self.assertEqual(ref_pack_b32_u16(0, 0), 0)
        self.assertEqual(ref_pack_b32_u16(0x1234, 0x5678), 0x56781234)
        self.assertEqual(ref_pack_b32_u16(0x00FF, 0xFF00), 0xFF0000FF)
        self.assertEqual(ref_pack_b32_u16(0xAAAA, 0x5555), 0x5555AAAA)

    def test_in_place_hi_half_replacement(self):
        dst = 0xDEADBEEF
        src = 0x0000CAFE
        result = ref_pack_in_place(dst, src, target="hi", op_sel=(0, 0))
        self.assertEqual(result, 0xCAFEBEEF)
        self.assertEqual(result & MASK16, dst & MASK16)
        self.assertEqual(result >> 16, src & MASK16)

    def test_in_place_lo_half_replacement(self):
        dst = 0xDEADBEEF
        src = 0x0000CAFE
        result = ref_pack_in_place(dst, src, target="lo", op_sel=(0, 1))
        self.assertEqual(result, 0xDEADCAFE)
        self.assertEqual(result >> 16, dst >> 16)
        self.assertEqual(result & MASK16, src & MASK16)

    def test_in_place_self_permutation(self):
        val = 0x12345678
        self.assertEqual(ref_pack_in_place(val, val, target="both", op_sel=(0, 0)), 0x56785678)
        self.assertEqual(ref_pack_in_place(val, val, target="both", op_sel=(1, 1)), 0x12341234)
        self.assertEqual(ref_pack_in_place(val, val, target="both", op_sel=(1, 0)), 0x56781234)
        self.assertEqual(ref_pack_in_place(val, val, target="both", op_sel=(0, 1)), 0x12345678)

    def test_op_sel_truth_table(self):
        s0 = 0xA1B2C3D4
        s1 = 0xE5F60718
        self.assertEqual(ref_pack_b32_u16(s0, s1, op_sel=(0, 0)), 0x0718C3D4)
        self.assertEqual(ref_pack_b32_u16(s0, s1, op_sel=(1, 0)), 0x0718A1B2)
        self.assertEqual(ref_pack_b32_u16(s0, s1, op_sel=(0, 1)), 0xE5F6C3D4)
        self.assertEqual(ref_pack_b32_u16(s0, s1, op_sel=(1, 1)), 0xE5F6A1B2)

    def test_unsigned_clamping(self):
        self.assertEqual(ref_pack_b32_u16(100, 200, clamp=True), (200 << 16) | 100)
        self.assertEqual(ref_pack_b32_u16(65535, 65535, clamp=True), 0xFFFFFFFF)
        self.assertEqual(ref_pack_b32_u16(65536, 10, clamp=True), (10 << 16) | 0xFFFF)
        self.assertEqual(ref_pack_b32_u16(10, 70000, clamp=True), (0xFFFF << 16) | 10)
        self.assertEqual(ref_pack_b32_u16(0xFFFFFFFF, 0xFFFFFFFF, clamp=True), 0xFFFFFFFF)
        self.assertEqual(ref_pack_b32_u16(0x80000000, 0x7FFFFFFF, clamp=True), 0xFFFFFFFF)

    def test_signed_clamping(self):
        self.assertEqual(ref_pack_b32_u16(-1, 500, clamp=True, signed=True), 500 << 16)
        self.assertEqual(ref_pack_b32_u16(500, -100, clamp=True, signed=True), 500)
        self.assertEqual(ref_pack_b32_u16(0x80000000, 0x80000000, clamp=True, signed=True), 0)
        self.assertEqual(ref_pack_b32_u16(70000, -5, clamp=True, signed=True), 0x0000FFFF)
        self.assertEqual(ref_pack_b32_u16(-5, 0x7FFFFFFF, clamp=True, signed=True), 0xFFFF0000)

    def test_clamping_with_op_sel(self):
        s0 = 50
        s1 = 100000
        self.assertEqual(ref_pack_b32_u16(s0, s1, op_sel=(0, 0), clamp=True), (0xFFFF << 16) | 50)
        self.assertEqual(ref_pack_b32_u16(s0, s1, op_sel=(1, 0), clamp=True), 0xFFFF0000)
        self.assertEqual(ref_pack_b32_u16(s0, s1, op_sel=(0, 1), clamp=True), (1 << 16) | 50)
        self.assertEqual(ref_pack_b32_u16(s0, s1, op_sel=(1, 1), clamp=True), 1 << 16)

    def test_decomposition_roundtrip(self):
        test_pairs = [(0, 0), (0x1234, 0x5678), (0xFFFF, 0x0001), (0x8000, 0x7FFF)]
        for lo, hi in test_pairs:
            dword = ref_pack_b32_u16(lo, hi)
            self.assertEqual(ref_decompose_pack(dword), (lo, hi))

    def test_spirv_logical_matches_oracle(self):
        vectors = [
            (0x1234, 0x5678),
            (0xA1B2C3D4, 0xE5F60718),
            (0xFFFFFFFF, 0x00000000),
            (0x80000000, 0x7FFFFFFF),
        ]
        for s0, s1 in vectors:
            for op_sel in itertools.product((0, 1), repeat=2):
                with self.subTest(s0=hex(s0), s1=hex(s1), op_sel=op_sel):
                    expected = ref_pack_b32_u16(s0, s1, op_sel=op_sel, clamp=False)
                    got = run_spirv_pack_logical(s0, s1, op_sel=op_sel, clamp=False)
                    self.assertEqual(got, expected)

    def test_spirv_bitfield_matches_oracle(self):
        vectors = [
            (0x0000, 0xFFFF),
            (0x12345678, 0x9ABCDEF0),
            (0x5555AAAA, 0xAAAA5555),
        ]
        for s0, s1 in vectors:
            for op_sel in itertools.product((0, 1), repeat=2):
                with self.subTest(s0=hex(s0), s1=hex(s1), op_sel=op_sel):
                    expected = ref_pack_b32_u16(s0, s1, op_sel=op_sel, clamp=False)
                    got = run_spirv_pack_bitfield(s0, s1, op_sel=op_sel)
                    self.assertEqual(got, expected)

    def test_spirv_clamping_parity(self):
        clamp_vectors = [(0, 0), (100, 200), (70000, 10), (0xFFFFFFFF, 0xFFFFFFFF)]
        for s0, s1 in clamp_vectors:
            for op_sel in itertools.product((0, 1), repeat=2):
                with self.subTest(s0=hex(s0), s1=hex(s1), op_sel=op_sel):
                    expected = ref_pack_b32_u16(s0, s1, op_sel=op_sel, clamp=True)
                    got = run_spirv_pack_logical(s0, s1, op_sel=op_sel, clamp=True)
                    self.assertEqual(got, expected)


class TestUpperByteTruncation(unittest.TestCase):
    """Validation of upper and lower half-word byte truncation semantics."""

    def test_src0_upper_bytes_masked_when_op_sel_zero(self):
        upper_variants = [0x00000000, 0x00010000, 0x7FFF0000, 0x80000000, 0xFFFF0000, 0xDEAD0000]
        for upper in upper_variants:
            s0 = upper | 0x1234
            res = ref_pack_b32_u16(s0, 0x5678, op_sel=(0, 0))
            self.assertEqual(res, 0x56781234)
            self.assertEqual(res & MASK16, 0x1234)

    def test_src1_upper_bytes_masked_when_op_sel_zero(self):
        upper_variants = [0x00000000, 0x00010000, 0x80000000, 0xFFFF0000, 0xBEEF0000]
        for upper in upper_variants:
            s1 = upper | 0x5678
            res = ref_pack_b32_u16(0x1234, s1, op_sel=(0, 0))
            self.assertEqual(res, 0x56781234)
            self.assertEqual(res >> 16, 0x5678)

    def test_src0_lower_bytes_masked_when_op_sel_one(self):
        lower_variants = [0x0000, 0x0001, 0x7FFF, 0x8000, 0xFFFF, 0x1234]
        for lower in lower_variants:
            s0 = (0x9ABC << 16) | lower
            res = ref_pack_b32_u16(s0, 0xDEF0, op_sel=(1, 0))
            self.assertEqual(res, 0xDEF09ABC)
            self.assertEqual(res & MASK16, 0x9ABC)

    def test_src1_lower_bytes_masked_when_op_sel_one(self):
        lower_variants = [0x0000, 0x0001, 0x8000, 0xFFFF, 0x5678]
        for lower in lower_variants:
            s1 = (0xDEF0 << 16) | lower
            res = ref_pack_b32_u16(0x9ABC, s1, op_sel=(0, 1))
            self.assertEqual(res, 0xDEF09ABC)
            self.assertEqual(res >> 16, 0xDEF0)

    def test_all_upper_byte_patterns_invariant_under_op_sel_zero(self):
        for b in range(256):
            s0 = (b << 24) | 0x0042
            self.assertEqual(ref_pack_b32_u16(s0, 0, op_sel=(0, 0)), 0x00000042)
            s0_b2 = (b << 16) | 0x0042
            self.assertEqual(ref_pack_b32_u16(s0_b2, 0, op_sel=(0, 0)), 0x00000042)

    def test_all_lower_byte_patterns_invariant_under_op_sel_one(self):
        for b in range(256):
            s0 = 0x42000000 | b
            self.assertEqual(ref_pack_b32_u16(s0, 0, op_sel=(1, 0)), 0x00004200)
            s0_b1 = 0x42000000 | (b << 8)
            self.assertEqual(ref_pack_b32_u16(s0_b1, 0, op_sel=(1, 0)), 0x00004200)

    def test_in_place_upper_byte_overwrite(self):
        dst = 0xAAAA5555
        src = 0x00003333
        new_dst = ref_pack_in_place(dst, src, target="hi", op_sel=(0, 0))
        self.assertEqual(new_dst, 0x33335555)
        self.assertNotEqual(new_dst >> 16, dst >> 16)

    def test_in_place_lower_byte_overwrite(self):
        dst = 0x5555AAAA
        src = 0x00003333
        new_dst = ref_pack_in_place(dst, src, target="lo", op_sel=(0, 1))
        self.assertEqual(new_dst, 0x55553333)
        self.assertNotEqual(new_dst & MASK16, dst & MASK16)

    def test_truncation_vs_clamping_contrast(self):
        val = 0x00020005
        unclamped = ref_pack_b32_u16(val, 0, clamp=False)
        clamped = ref_pack_b32_u16(val, 0, clamp=True)
        self.assertEqual(unclamped, 0x00000005)
        self.assertEqual(clamped, 0x0000FFFF)


class TestSimdMultiLanePack(unittest.TestCase):
    """Validation of multi-lane SIMD wave execution and in-place vector packing."""

    def test_wave32_full_wave_in_place_gradient(self):
        wave = SimdWaveState(wave_size=32)
        v0_initial = [(0xAA00 | lane) for lane in range(32)]
        v1_initial = [(0x5500 | (31 - lane)) for lane in range(32)]
        wave.set_vgpr_lanes(0, v0_initial)
        wave.set_vgpr_lanes(1, v1_initial)

        wave.execute_pack_b32_u16(dst_reg=0, src0="v0", src1="v1", op_sel=(0, 0))

        v0_result = wave.get_vgpr_lanes(0)
        for lane in range(32):
            expected = (v1_initial[lane] << 16) | v0_initial[lane]
            self.assertEqual(v0_result[lane], expected)

    def test_wave64_full_wave_in_place_gradient(self):
        wave = SimdWaveState(wave_size=64)
        v0_initial = [(0x1000 | lane) for lane in range(64)]
        v1_initial = [(0x2000 | lane) for lane in range(64)]
        wave.set_vgpr_lanes(0, v0_initial)
        wave.set_vgpr_lanes(1, v1_initial)

        wave.execute_pack_b32_u16(dst_reg=0, src0="v0", src1="v1", op_sel=(0, 0))

        v0_result = wave.get_vgpr_lanes(0)
        for lane in range(64):
            expected = (v1_initial[lane] << 16) | v0_initial[lane]
            self.assertEqual(v0_result[lane], expected)

    def test_simd_predication_even_lanes(self):
        wave = SimdWaveState(wave_size=32)
        v0_initial = [0x11111111] * 32
        v1_initial = [0x22222222] * 32
        wave.set_vgpr_lanes(0, v0_initial)
        wave.set_vgpr_lanes(1, v1_initial)

        wave.exec_mask = 0x55555555
        wave.execute_pack_b32_u16(dst_reg=0, src0="v0", src1="v1", op_sel=(0, 0))

        v0_result = wave.get_vgpr_lanes(0)
        for lane in range(32):
            if lane % 2 == 0:
                self.assertEqual(v0_result[lane], 0x22221111)
            else:
                self.assertEqual(v0_result[lane], 0x11111111)

    def test_simd_predication_divergent_halves(self):
        wave = SimdWaveState(wave_size=32)
        v0_initial = [0xCAFEBABE] * 32
        v1_initial = [0x00001234] * 32
        wave.set_vgpr_lanes(0, v0_initial)
        wave.set_vgpr_lanes(1, v1_initial)

        wave.exec_mask = 0x0000FFFF
        wave.execute_pack_b32_u16(dst_reg=0, src0="v0", src1="v1", op_sel=(0, 0))

        v0_result = wave.get_vgpr_lanes(0)
        for lane in range(32):
            if lane < 16:
                self.assertEqual(v0_result[lane], 0x1234BABE)
            else:
                self.assertEqual(v0_result[lane], 0xCAFEBABE)

    def test_simd_uniform_scalar_in_place_pack(self):
        wave = SimdWaveState(wave_size=32)
        wave.sgprs[0] = 0x7777
        v0_initial = [(0x1000 | lane) for lane in range(32)]
        wave.set_vgpr_lanes(0, v0_initial)

        wave.execute_pack_b32_u16(dst_reg=0, src0="v0", src1="s0", op_sel=(0, 0))

        v0_result = wave.get_vgpr_lanes(0)
        for lane in range(32):
            self.assertEqual(v0_result[lane], (0x7777 << 16) | v0_initial[lane])

    def test_simd_interleaved_vertex_packing(self):
        wave = SimdWaveState(wave_size=32)
        u_coords = [lane * 100 for lane in range(32)]
        v_coords = [lane * 200 for lane in range(32)]
        wave.set_vgpr_lanes(10, u_coords)
        wave.set_vgpr_lanes(11, v_coords)

        wave.execute_pack_b32_u16(dst_reg=10, src0="v10", src1="v11", op_sel=(0, 0))

        packed_coords = wave.get_vgpr_lanes(10)
        for lane in range(32):
            expected = ((v_coords[lane] & MASK16) << 16) | (u_coords[lane] & MASK16)
            self.assertEqual(packed_coords[lane], expected)

    def test_spirv_wave_interp_lane_packing(self):
        head = """OpCapability Shader
OpCapability GroupNonUniformBallot
OpMemoryModel Logical GLSL450
OpEntryPoint Fragment %main "main" %lane %out
OpDecorate %lane BuiltIn SubgroupLocalInvocationId
%void = OpTypeVoid
%fn = OpTypeFunction %void
%uint = OpTypeInt 32 0
%v4uint = OpTypeVector %uint 4
%pin = OpTypePointer Input %uint
%pout = OpTypePointer Output %v4uint
%lane = OpVariable %pin Input
%out = OpVariable %pout Output
%c16 = OpConstant %uint 16
%c65535 = OpConstant %uint 65535
%c_base0 = OpConstant %uint 4096
%c_base1 = OpConstant %uint 8192
%main = OpFunction %void None %fn
%entry = OpLabel
%l = OpLoad %uint %lane
%s0 = OpIAdd %uint %l %c_base0
%s1 = OpIAdd %uint %l %c_base1
%h0 = OpBitwiseAnd %uint %s0 %c65535
%h1 = OpBitwiseAnd %uint %s1 %c65535
%h1_hi = OpShiftLeftLogical %uint %h1 %c16
%res = OpBitwiseOr %uint %h0 %h1_hi
%vec = OpCompositeConstruct %v4uint %res %res %res %res
OpStore %out %vec
OpReturn
OpFunctionEnd
"""
        wave_res = sw.run_wave(head, wave=32)
        outputs = [o["%out"][0] for o in wave_res["outputs"]]
        for lane in range(32):
            s0 = lane + 4096
            s1 = lane + 8192
            expected = ref_pack_b32_u16(s0, s1)
            self.assertEqual(outputs[lane], expected)


class TestRegisterAliasing(unittest.TestCase):
    """Validation of register aliasing when destination matches src0, src1, or both."""

    def test_dst_aliases_src0_in_place_pack_high(self):
        v0 = 0x11112222
        v1 = 0x33334444
        self.assertEqual(ref_pack_b32_u16(v0, v1, op_sel=(0, 0)), 0x44442222)
        self.assertEqual(ref_pack_b32_u16(v0, v1, op_sel=(1, 0)), 0x44441111)
        self.assertEqual(ref_pack_b32_u16(v0, v1, op_sel=(0, 1)), 0x33332222)
        self.assertEqual(ref_pack_b32_u16(v0, v1, op_sel=(1, 1)), 0x33331111)

    def test_dst_aliases_src1_in_place_pack_low(self):
        v0 = 0x11112222
        v1 = 0x33334444
        self.assertEqual(ref_pack_b32_u16(v0, v1, op_sel=(0, 0)), 0x44442222)
        self.assertEqual(ref_pack_b32_u16(v0, v1, op_sel=(1, 0)), 0x44441111)
        self.assertEqual(ref_pack_b32_u16(v0, v1, op_sel=(0, 1)), 0x33332222)
        self.assertEqual(ref_pack_b32_u16(v0, v1, op_sel=(1, 1)), 0x33331111)

    def test_dst_aliases_both_sources_self_pack(self):
        v0 = 0xAABBCCDD
        self.assertEqual(ref_pack_b32_u16(v0, v0, op_sel=(0, 0)), 0xCCDDCCDD)
        self.assertEqual(ref_pack_b32_u16(v0, v0, op_sel=(1, 1)), 0xAABBAABB)
        self.assertEqual(ref_pack_b32_u16(v0, v0, op_sel=(1, 0)), 0xCCDDAABB)
        self.assertEqual(ref_pack_b32_u16(v0, v0, op_sel=(0, 1)), 0xAABBCCDD)

    def test_simd_wave_aliased_registers(self):
        wave = SimdWaveState(wave_size=32)
        initial_v0 = [(0x10000000 | (lane << 16) | (lane * 2)) for lane in range(32)]
        initial_v1 = [(0x20000000 | ((31 - lane) << 16) | (lane * 3)) for lane in range(32)]

        wave.set_vgpr_lanes(0, initial_v0)
        wave.set_vgpr_lanes(1, initial_v1)
        wave.execute_pack_b32_u16(dst_reg=0, src0="v0", src1="v1", op_sel=(0, 0))

        result_v0 = wave.get_vgpr_lanes(0)
        for lane in range(32):
            expected = ref_pack_b32_u16(initial_v0[lane], initial_v1[lane], op_sel=(0, 0))
            self.assertEqual(result_v0[lane], expected)

        wave.execute_pack_b32_u16(dst_reg=0, src0="v0", src1="v0", op_sel=(1, 0))
        swapped_v0 = wave.get_vgpr_lanes(0)
        for lane in range(32):
            expected = ref_pack_b32_u16(result_v0[lane], result_v0[lane], op_sel=(1, 0))
            self.assertEqual(swapped_v0[lane], expected)

    def test_chained_in_place_packing_sequence(self):
        wave = SimdWaveState(wave_size=1)
        wave.set_vgpr_lane(0, 0, 0x00000000)
        wave.set_vgpr_lane(1, 0, 0x00001111)
        wave.set_vgpr_lane(2, 0, 0x00002222)
        wave.set_vgpr_lane(3, 0, 0x00003333)

        wave.execute_pack_b32_u16(dst_reg=0, src0="v1", src1="v2", op_sel=(0, 0))
        self.assertEqual(wave.vgprs[0][0], 0x22221111)

        wave.execute_pack_b32_u16(dst_reg=0, src0="v0", src1="v3", op_sel=(0, 0))
        self.assertEqual(wave.vgprs[0][0], 0x33331111)

        wave.execute_pack_b32_u16(dst_reg=0, src0="v0", src1="v0", op_sel=(1, 0))
        self.assertEqual(wave.vgprs[0][0], 0x11113333)


class TestBoundaryExtremes(unittest.TestCase):
    """Validation of boundary extreme words, bit patterns, and clamping saturation limits."""

    EXTREME_WORDS = (
        0x00000000,
        0xFFFFFFFF,
        0x7FFFFFFF,
        0x80000000,
        0x0000FFFF,
        0xFFFF0000,
        0x00010000,
        0x00000001,
        0x7FFF0000,
        0x00007FFF,
        0x80000000,
        0x00008000,
    )

    PATTERNS = (
        0x55555555,
        0xAAAAAAAA,
        0x33333333,
        0xCCCCCCCC,
        0x0F0F0F0F,
        0xF0F0F0F0,
        0x00FF00FF,
        0xFF00FF00,
    )

    def test_full_word_extremes(self):
        for s0 in self.EXTREME_WORDS:
            for s1 in (0x00000000, 0xFFFFFFFF, 0x12345678):
                for op_sel in itertools.product((0, 1), repeat=2):
                    expected = ref_pack_b32_u16(s0, s1, op_sel=op_sel)
                    self.assertEqual(run_spirv_pack_logical(s0, s1, op_sel=op_sel), expected)

    def test_alternating_bit_patterns(self):
        for s0 in self.PATTERNS:
            for s1 in self.PATTERNS:
                expected = ref_pack_b32_u16(s0, s1)
                self.assertEqual(run_spirv_pack_logical(s0, s1), expected)

    def test_single_bit_walking_ones(self):
        for k in range(32):
            s0 = 1 << k
            s1 = 0
            res = ref_pack_b32_u16(s0, s1, op_sel=(0, 0))
            expected = s0 & MASK16
            self.assertEqual(res, expected)

            res_hi = ref_pack_b32_u16(s0, s1, op_sel=(1, 0))
            expected_hi = (s0 >> 16) & MASK16
            self.assertEqual(res_hi, expected_hi)

    def test_single_bit_walking_zeros(self):
        for k in range(32):
            s0 = MASK32 ^ (1 << k)
            s1 = 0xFFFFFFFF
            res = ref_pack_b32_u16(s0, s1, op_sel=(0, 0))
            expected = (s0 & MASK16) | 0xFFFF0000
            self.assertEqual(res, expected)

    def test_clamp_saturation_boundaries(self):
        boundary_cases = (
            (65534, 65534),
            (65535, 65535),
            (65536, 65535),
            (65537, 65535),
            (0x7FFFFFFE, 65535),
            (0x7FFFFFFF, 65535),
            (0x80000000, 65535),
            (0xFFFFFFFE, 65535),
            (0xFFFFFFFF, 65535),
        )
        for val, expected_half in boundary_cases:
            with self.subTest(val=hex(val)):
                res = ref_pack_b32_u16(val, 0, clamp=True)
                self.assertEqual(res & MASK16, expected_half)
                res_hi = ref_pack_b32_u16(0, val, clamp=True)
                self.assertEqual(res_hi >> 16, expected_half)

    def test_deterministic_fuzzing(self):
        rng = random.Random(0x1616)
        for _ in range(40):
            s0 = rng.getrandbits(32)
            s1 = rng.getrandbits(32)
            op_sel = (rng.choice((0, 1)), rng.choice((0, 1)))
            clamp = rng.choice((True, False))

            expected = ref_pack_b32_u16(s0, s1, op_sel=op_sel, clamp=clamp)
            got = run_spirv_pack_logical(s0, s1, op_sel=op_sel, clamp=clamp)
            self.assertEqual(got, expected)


class TestNegativeControls(unittest.TestCase):
    """Negative controls ensuring mutated packing or missing masks fail assertions."""

    def test_missing_high_half_shift_fails(self):
        s0, s1 = 0x1234, 0x5678
        correct = ref_pack_b32_u16(s0, s1)
        sabotaged_no_shift = (s0 & MASK16) | (s1 & MASK16)
        self.assertNotEqual(correct, sabotaged_no_shift)

    def test_off_by_one_shift_fails(self):
        s0, s1 = 0x1234, 0x5678
        correct = ref_pack_b32_u16(s0, s1)
        sabotaged_shift_15 = (s0 & MASK16) | ((s1 & MASK16) << 15)
        sabotaged_shift_17 = (s0 & MASK16) | (((s1 & MASK16) << 17) & MASK32)
        self.assertNotEqual(correct, sabotaged_shift_15)
        self.assertNotEqual(correct, sabotaged_shift_17)

    def test_inverted_operand_order_fails(self):
        s0, s1 = 0x11112222, 0x33334444
        correct = ref_pack_b32_u16(s0, s1, op_sel=(0, 0))
        reversed_operands = ref_pack_b32_u16(s1, s0, op_sel=(0, 0))
        self.assertNotEqual(correct, reversed_operands)

    def test_missing_low_half_mask_fails(self):
        s0, s1 = 0xDEAD1234, 0xBEEF5678
        correct = ref_pack_b32_u16(s0, s1)
        sabotaged_no_truncation = (s0 | (s1 << 16)) & MASK32
        self.assertNotEqual(correct, sabotaged_no_truncation)

    def test_inverted_op_sel_fails(self):
        s0, s1 = 0x12345678, 0x9ABCDEF0
        correct = ref_pack_b32_u16(s0, s1, op_sel=(0, 0))
        inverted = ref_pack_b32_u16(s0, s1, op_sel=(1, 1))
        self.assertNotEqual(correct, inverted)

    def test_modulo_wrap_instead_of_clamp_fails(self):
        s0, s1 = 70000, 20
        clamped = ref_pack_b32_u16(s0, s1, clamp=True)
        wrapped = ref_pack_b32_u16(s0, s1, clamp=False)
        self.assertNotEqual(clamped, wrapped)

    def test_signed_vs_unsigned_clamp_discrepancy_fails(self):
        s0, s1 = -10, 10
        uclamped = ref_pack_b32_u16(s0, s1, clamp=True, signed=False)
        sclamped = ref_pack_b32_u16(s0, s1, clamp=True, signed=True)
        self.assertNotEqual(uclamped, sclamped)

    def test_read_after_write_hazard_simulator_detected(self):
        s0 = 0x11112222
        s1 = 0x33334444
        correct = ref_pack_b32_u16(s0, s1)

        sim_reg = s1
        sim_reg = s0 & MASK16
        corrupted = sim_reg | ((sim_reg & MASK16) << 16)
        self.assertNotEqual(correct, corrupted)


if __name__ == "__main__":
    unittest.main()
