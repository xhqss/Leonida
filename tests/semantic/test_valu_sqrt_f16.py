from __future__ import annotations

import math
import struct
import unittest
from dataclasses import dataclass, field
from enum import Enum, auto
from fractions import Fraction
from typing import List, Optional, Tuple, Union

# IEEE 754 binary16 (half precision) architectural constants
SIGN_MASK: int = 0x8000
EXP_MASK: int = 0x7C00
FRAC_MASK: int = 0x03FF
QNAN_BIT: int = 0x0200
CANONICAL_QNAN: int = 0x7E00

POS_ZERO: int = 0x0000
NEG_ZERO: int = 0x8000
POS_INF: int = 0x7C00
NEG_INF: int = 0xFC00

ONE_F16: int = 0x3C00
FOUR_F16: int = 0x4400
NINE_F16: int = 0x4880
TWO_F16: int = 0x4000
THREE_F16: int = 0x4200
HALF_F16: int = 0x3800
QUARTER_F16: int = 0x3400


def f16_to_bits(val: float) -> int:
    """Convert a Python float to 16-bit binary16 integer bits."""
    return struct.unpack("<H", struct.pack("<e", val))[0]


def bits_to_f16(bits: int) -> float:
    """Convert 16-bit binary16 integer bits to a Python float."""
    return struct.unpack("<e", struct.pack("<H", bits & 0xFFFF))[0]


def split_f16_bits(bits: int) -> Tuple[int, int, int]:
    """Unpack bits into (sign, exponent, fraction) components."""
    sign = (bits & SIGN_MASK) >> 15
    exponent = (bits & EXP_MASK) >> 10
    fraction = bits & FRAC_MASK
    return sign, exponent, fraction


def dword_to_f16_halves(dword: int) -> Tuple[int, int]:
    """Split a 32-bit VGPR dword into (lo, hi) 16-bit halfwords."""
    return dword & 0xFFFF, (dword >> 16) & 0xFFFF


def f16_halves_to_dword(lo: int, hi: int) -> int:
    """Combine two 16-bit halfwords (lo, hi) into a 32-bit VGPR dword."""
    return ((hi & 0xFFFF) << 16) | (lo & 0xFFFF)


def is_nan(bits: int) -> bool:
    """Return True if bits represent an IEEE 754 half-precision NaN."""
    return ((bits & EXP_MASK) == EXP_MASK) and ((bits & FRAC_MASK) != 0)


def is_qnan(bits: int) -> bool:
    """Return True if bits represent an IEEE 754 half-precision Quiet NaN."""
    return is_nan(bits) and bool(bits & QNAN_BIT)


def is_snan(bits: int) -> bool:
    """Return True if bits represent an IEEE 754 half-precision Signaling NaN."""
    return is_nan(bits) and not bool(bits & QNAN_BIT)


def is_inf(bits: int) -> bool:
    """Return True if bits represent positive or negative infinity."""
    return ((bits & EXP_MASK) == EXP_MASK) and ((bits & FRAC_MASK) == 0)


def is_subnormal(bits: int) -> bool:
    """Return True if bits represent a subnormal (denormal) half-precision float."""
    return ((bits & EXP_MASK) == 0) and ((bits & FRAC_MASK) != 0)


def is_zero(bits: int) -> bool:
    """Return True if bits represent positive or negative zero."""
    return (bits & ~SIGN_MASK) == 0


def apply_f16_modifiers(bits: int, abs_mod: bool, neg_mod: bool) -> int:
    """Apply VOP3 input modifiers: abs clears sign bit; neg toggles sign bit."""
    res = bits & 0xFFFF
    if abs_mod:
        res &= ~SIGN_MASK
    if neg_mod:
        res ^= SIGN_MASK
    return res


def oracle_v_sqrt_f16(
    src0_bits: int,
    abs0: bool = False,
    neg0: bool = False,
    clamp: bool = False,
) -> int:
    """Reference mathematical oracle for RDNA2 v_sqrt_f16 half-precision square root.

    Computes D = sqrt(src0) conforming to IEEE 754-2008 binary16
    with Round-to-Nearest-Even (RNE), applying input modifiers and clamp.
    """
    s0 = apply_f16_modifiers(src0_bits, abs0, neg0)

    # 1. NaN handling
    if is_nan(s0):
        if clamp:
            return POS_ZERO
        return s0 | QNAN_BIT

    # 2. Negative inputs (excluding negative zero)
    # In IEEE 754 and RDNA2, sqrt of negative non-zero produces Canonical Quiet NaN
    if (s0 & SIGN_MASK) != 0 and s0 != NEG_ZERO:
        return POS_ZERO if clamp else CANONICAL_QNAN

    # 3. Infinity handling (+inf -> +inf, -inf was caught above as < 0)
    if is_inf(s0):
        if clamp:
            return ONE_F16
        return POS_INF

    # 4. Zero handling (+0 -> +0, -0 -> -0)
    if is_zero(s0):
        if clamp:
            return POS_ZERO
        return s0

    # 5. Finite positive square root (normals and subnormals)
    f0 = bits_to_f16(s0)
    res_f = math.sqrt(f0)
    res = f16_to_bits(res_f)

    # 6. Clamp modifier ([0.0, 1.0])
    if clamp:
        if res_f < 0.0 or res == NEG_ZERO:
            res = POS_ZERO
        elif res_f > 1.0 or res == POS_INF:
            res = ONE_F16

    return res


# ==============================================================================
# Symbolic AST Opcode Definitions and Typed Expressions
# ==============================================================================

class RegType(Enum):
    VGPR = auto()
    SGPR = auto()


class HalfSelect(Enum):
    LO = 0  # bits [15:0]
    HI = 1  # bits [31:16]


@dataclass(frozen=True)
class RegHalf:
    """Represents an architectural 16-bit half-register (e.g., v0.l, v0.h, s4.l)."""
    index: int
    reg_type: RegType
    half: HalfSelect = HalfSelect.LO

    def __post_init__(self) -> None:
        if not (0 <= self.index <= 255):
            raise ValueError(f"Register index {self.index} out of range [0, 255]")

    @property
    def name(self) -> str:
        prefix = "v" if self.reg_type == RegType.VGPR else "s"
        suffix = ".l" if self.half == HalfSelect.LO else ".h"
        return f"{prefix}{self.index}{suffix}"


@dataclass(frozen=True)
class Literal16:
    """Represents a 16-bit literal or inline float constant."""
    bits: int

    @classmethod
    def from_float(cls, val: float) -> Literal16:
        return cls(f16_to_bits(val))

    @property
    def float_value(self) -> float:
        return bits_to_f16(self.bits)

    @property
    def name(self) -> str:
        if self.bits == POS_ZERO:
            return "0.0"
        if self.bits == NEG_ZERO:
            return "-0.0"
        f = self.float_value
        if math.isnan(f):
            return "nan"
        if math.isinf(f):
            return "inf" if f > 0 else "-inf"
        return f"{f:g}"


@dataclass(frozen=True)
class VOP3Operand:
    """Typed expression operand with optional input modifiers (abs, neg)."""
    source: Union[RegHalf, Literal16]
    abs_mod: bool = False
    neg_mod: bool = False

    def disassemble(self) -> str:
        s = self.source.name
        if self.abs_mod:
            s = f"|{s}|"
        if self.neg_mod:
            s = f"-{s}"
        return s


@dataclass(frozen=True)
class VSqrtF16Instruction:
    """Symbolic AST node representing a RDNA2 v_sqrt_f16 instruction."""
    dst: RegHalf
    src0: VOP3Operand
    clamp: bool = False

    def __post_init__(self) -> None:
        if self.dst.reg_type != RegType.VGPR:
            raise TypeError(f"Destination of VALU instruction must be VGPR, got {self.dst.reg_type}")

    def disassemble(self) -> str:
        s = f"v_sqrt_f16 {self.dst.name}, {self.src0.disassemble()}"
        if self.clamp:
            s += " clamp"
        return s


# ==============================================================================
# Deterministic Scalar Instruction Interpreter
# ==============================================================================

@dataclass
class ScalarMachineState:
    """Architectural register file for a single wavefront lane."""
    vgprs: List[int] = field(default_factory=lambda: [0] * 256)
    sgprs: List[int] = field(default_factory=lambda: [0] * 256)

    def get_vgpr16(self, index: int, half: HalfSelect) -> int:
        dword = self.vgprs[index]
        lo, hi = dword_to_f16_halves(dword)
        return hi if half == HalfSelect.HI else lo

    def set_vgpr16(self, index: int, half: HalfSelect, bits: int) -> None:
        dword = self.vgprs[index]
        lo, hi = dword_to_f16_halves(dword)
        if half == HalfSelect.HI:
            self.vgprs[index] = f16_halves_to_dword(lo, bits & 0xFFFF)
        else:
            self.vgprs[index] = f16_halves_to_dword(bits & 0xFFFF, hi)

    def get_sgpr16(self, index: int, half: HalfSelect) -> int:
        dword = self.sgprs[index]
        lo, hi = dword_to_f16_halves(dword)
        return hi if half == HalfSelect.HI else lo

    def set_sgpr16(self, index: int, half: HalfSelect, bits: int) -> None:
        dword = self.sgprs[index]
        lo, hi = dword_to_f16_halves(dword)
        if half == HalfSelect.HI:
            self.sgprs[index] = f16_halves_to_dword(lo, bits & 0xFFFF)
        else:
            self.sgprs[index] = f16_halves_to_dword(bits & 0xFFFF, hi)


class ScalarInterpreter:
    """Deterministic scalar instruction interpreter for v_sqrt_f16."""

    def resolve_operand(self, op: VOP3Operand, state: ScalarMachineState) -> int:
        if isinstance(op.source, RegHalf):
            if op.source.reg_type == RegType.VGPR:
                return state.get_vgpr16(op.source.index, op.source.half)
            elif op.source.reg_type == RegType.SGPR:
                return state.get_sgpr16(op.source.index, op.source.half)
            else:
                raise ValueError(f"Unknown register type: {op.source.reg_type}")
        elif isinstance(op.source, Literal16):
            return op.source.bits
        else:
            raise TypeError(f"Unsupported operand source type: {type(op.source)}")

    def step(self, instr: VSqrtF16Instruction, state: ScalarMachineState) -> int:
        # Atomic read phase
        s0_raw = self.resolve_operand(instr.src0, state)

        # Computation phase
        result_bits = oracle_v_sqrt_f16(
            src0_bits=s0_raw,
            abs0=instr.src0.abs_mod,
            neg0=instr.src0.neg_mod,
            clamp=instr.clamp,
        )

        # Write-back phase
        state.set_vgpr16(instr.dst.index, instr.dst.half, result_bits)
        return result_bits


# ==============================================================================
# SIMD Multi-Lane Execution Engine and EXEC Masking
# ==============================================================================

@dataclass
class SIMDState:
    """Multi-lane SIMD machine state representing Wave32 or Wave64."""
    num_lanes: int
    exec_mask: Optional[int] = None
    vgprs: List[List[int]] = field(default_factory=list)
    sgprs: List[int] = field(default_factory=lambda: [0] * 256)

    def __post_init__(self) -> None:
        if self.num_lanes not in (32, 64):
            raise ValueError(f"num_lanes must be 32 or 64, got {self.num_lanes}")
        if not self.vgprs:
            self.vgprs = [[0] * 256 for _ in range(self.num_lanes)]
        if self.exec_mask is None:
            self.exec_mask = (1 << self.num_lanes) - 1

    def get_vgpr16(self, lane: int, index: int, half: HalfSelect) -> int:
        dword = self.vgprs[lane][index]
        lo, hi = dword_to_f16_halves(dword)
        return hi if half == HalfSelect.HI else lo

    def set_vgpr16(self, lane: int, index: int, half: HalfSelect, bits: int) -> None:
        dword = self.vgprs[lane][index]
        lo, hi = dword_to_f16_halves(dword)
        if half == HalfSelect.HI:
            self.vgprs[lane][index] = f16_halves_to_dword(lo, bits & 0xFFFF)
        else:
            self.vgprs[lane][index] = f16_halves_to_dword(bits & 0xFFFF, hi)

    def get_sgpr16(self, index: int, half: HalfSelect) -> int:
        dword = self.sgprs[index]
        lo, hi = dword_to_f16_halves(dword)
        return hi if half == HalfSelect.HI else lo

    def set_sgpr16(self, index: int, half: HalfSelect, bits: int) -> None:
        dword = self.sgprs[index]
        lo, hi = dword_to_f16_halves(dword)
        if half == HalfSelect.HI:
            self.sgprs[index] = f16_halves_to_dword(lo, bits & 0xFFFF)
        else:
            self.sgprs[index] = f16_halves_to_dword(bits & 0xFFFF, hi)


class SIMDInterpreter:
    """SIMD instruction interpreter handling multi-lane divergence and EXEC masking."""

    def resolve_lane_operand(self, op: VOP3Operand, state: SIMDState, lane: int) -> int:
        if isinstance(op.source, RegHalf):
            if op.source.reg_type == RegType.VGPR:
                return state.get_vgpr16(lane, op.source.index, op.source.half)
            elif op.source.reg_type == RegType.SGPR:
                return state.get_sgpr16(op.source.index, op.source.half)
            else:
                raise ValueError(f"Unknown register type: {op.source.reg_type}")
        elif isinstance(op.source, Literal16):
            return op.source.bits
        else:
            raise TypeError(f"Unsupported operand source type: {type(op.source)}")

    def step(self, instr: VSqrtF16Instruction, state: SIMDState) -> List[Optional[int]]:
        results: List[Optional[int]] = [None] * state.num_lanes
        # Atomic read phase across all lanes
        s0_vals = [
            self.resolve_lane_operand(instr.src0, state, lane)
            for lane in range(state.num_lanes)
        ]

        # Execution phase
        for lane in range(state.num_lanes):
            if (state.exec_mask & (1 << lane)) != 0:
                res = oracle_v_sqrt_f16(
                    src0_bits=s0_vals[lane],
                    abs0=instr.src0.abs_mod,
                    neg0=instr.src0.neg_mod,
                    clamp=instr.clamp,
                )
                state.set_vgpr16(lane, instr.dst.index, instr.dst.half, res)
                results[lane] = res

        return results


# ==============================================================================
# Unit Tests
# ==============================================================================

class TestF16ModelAndOracle(unittest.TestCase):
    """Unit tests for binary16 bit model, predicates, and reference oracle."""

    def test_bit_representation_roundtrip(self) -> None:
        values = [0.0, -0.0, 1.0, -1.0, 0.5, -2.5, 4.0, 9.0, 16.0]
        for val in values:
            with self.subTest(val=val):
                bits = f16_to_bits(val)
                recovered = bits_to_f16(bits)
                self.assertEqual(val, recovered)
                self.assertEqual(math.copysign(1.0, val), math.copysign(1.0, recovered))

    def test_halfword_packing_vgpr(self) -> None:
        lo = 0x3C00  # 1.0
        hi = 0x4000  # 2.0
        dword = f16_halves_to_dword(lo, hi)
        self.assertEqual(dword, 0x40003C00)
        unpacked_lo, unpacked_hi = dword_to_f16_halves(dword)
        self.assertEqual(unpacked_lo, lo)
        self.assertEqual(unpacked_hi, hi)
        self.assertEqual(bits_to_f16(unpacked_lo), 1.0)
        self.assertEqual(bits_to_f16(unpacked_hi), 2.0)

    def test_classification_predicates(self) -> None:
        self.assertTrue(f16_to_bits(0.0) == POS_ZERO)
        self.assertTrue(f16_to_bits(-0.0) == NEG_ZERO)
        self.assertTrue(is_zero(POS_ZERO))
        self.assertTrue(is_zero(NEG_ZERO))
        self.assertFalse(is_zero(f16_to_bits(1.0)))

        self.assertTrue(is_inf(POS_INF))
        self.assertTrue(is_inf(NEG_INF))
        self.assertFalse(is_inf(POS_ZERO))

        snan_bits = 0x7C01  # exponent 0x1F, fraction != 0, quiet bit 9 is 0
        qnan_bits = CANONICAL_QNAN
        self.assertTrue(is_nan(snan_bits))
        self.assertTrue(is_snan(snan_bits))
        self.assertFalse(is_qnan(snan_bits))

        self.assertTrue(is_nan(qnan_bits))
        self.assertTrue(is_qnan(qnan_bits))
        self.assertFalse(is_snan(qnan_bits))

        subnorm_bits = 0x0001
        self.assertTrue(is_subnormal(subnorm_bits))
        self.assertFalse(is_subnormal(POS_ZERO))
        self.assertFalse(is_subnormal(f16_to_bits(1.0)))

    def test_exact_finite_sqrt(self) -> None:
        self.assertEqual(oracle_v_sqrt_f16(FOUR_F16), TWO_F16)
        self.assertEqual(oracle_v_sqrt_f16(NINE_F16), THREE_F16)
        self.assertEqual(oracle_v_sqrt_f16(ONE_F16), ONE_F16)
        self.assertEqual(oracle_v_sqrt_f16(QUARTER_F16), HALF_F16)

    def test_signed_zero_rules(self) -> None:
        # IEEE 754: sqrt(+0.0) = +0.0, sqrt(-0.0) = -0.0
        self.assertEqual(oracle_v_sqrt_f16(POS_ZERO), POS_ZERO)
        self.assertEqual(oracle_v_sqrt_f16(NEG_ZERO), NEG_ZERO)

    def test_input_modifiers_abs_neg(self) -> None:
        val_neg_four = FOUR_F16 | SIGN_MASK  # -4.0
        # abs on -4.0: sqrt(|-4.0|) = sqrt(4.0) = 2.0
        res = oracle_v_sqrt_f16(val_neg_four, abs0=True)
        self.assertEqual(res, TWO_F16)

        # neg on 4.0: sqrt(-(4.0)) = sqrt(-4.0) = NaN
        res_neg = oracle_v_sqrt_f16(FOUR_F16, neg0=True)
        self.assertTrue(is_qnan(res_neg))
        self.assertEqual(res_neg, CANONICAL_QNAN)

        # abs and neg together: -|4.0| = -4.0 -> NaN
        res_neg_abs = oracle_v_sqrt_f16(FOUR_F16, abs0=True, neg0=True)
        self.assertEqual(res_neg_abs, CANONICAL_QNAN)

    def test_clamp_modifier(self) -> None:
        # sqrt(4.0) = 2.0 -> clamped to 1.0
        res_clamped_hi = oracle_v_sqrt_f16(FOUR_F16, clamp=True)
        self.assertEqual(res_clamped_hi, ONE_F16)

        # sqrt(0.25) = 0.5 -> within [0, 1], remains 0.5
        res_mid = oracle_v_sqrt_f16(QUARTER_F16, clamp=True)
        self.assertEqual(res_mid, HALF_F16)

        # sqrt(-4.0) with clamp produces +0.0
        res_nan_clamp = oracle_v_sqrt_f16(FOUR_F16 | SIGN_MASK, clamp=True)
        self.assertEqual(res_nan_clamp, POS_ZERO)

        # sqrt(-0.0) with clamp produces +0.0
        res_zero_clamp = oracle_v_sqrt_f16(NEG_ZERO, clamp=True)
        self.assertEqual(res_zero_clamp, POS_ZERO)

    def test_infinity_sqrt(self) -> None:
        self.assertEqual(oracle_v_sqrt_f16(POS_INF), POS_INF)
        self.assertEqual(oracle_v_sqrt_f16(NEG_INF), CANONICAL_QNAN)
        # Clamped +inf yields 1.0
        self.assertEqual(oracle_v_sqrt_f16(POS_INF, clamp=True), ONE_F16)

    def test_negative_operand_produces_qnan(self) -> None:
        self.assertEqual(oracle_v_sqrt_f16(0xBC00), CANONICAL_QNAN)  # -1.0
        self.assertEqual(oracle_v_sqrt_f16(0x8001), CANONICAL_QNAN)  # negative subnormal

    def test_nan_quietening_and_propagation(self) -> None:
        snan = 0x7C05  # Signaling NaN
        res = oracle_v_sqrt_f16(snan)
        self.assertTrue(is_qnan(res))
        self.assertEqual(res, snan | QNAN_BIT)


class TestSymbolicAST(unittest.TestCase):
    """Unit tests for symbolic AST opcode definitions and typed expressions."""

    def test_reghalf_validation_and_naming(self) -> None:
        v0_l = RegHalf(0, RegType.VGPR, HalfSelect.LO)
        self.assertEqual(v0_l.name, "v0.l")
        self.assertEqual(v0_l.index, 0)
        self.assertEqual(v0_l.half, HalfSelect.LO)

        v0_h = RegHalf(0, RegType.VGPR, HalfSelect.HI)
        self.assertEqual(v0_h.name, "v0.h")

        s8_l = RegHalf(8, RegType.SGPR, HalfSelect.LO)
        self.assertEqual(s8_l.name, "s8.l")
        s8_h = RegHalf(8, RegType.SGPR, HalfSelect.HI)
        self.assertEqual(s8_h.name, "s8.h")

        with self.assertRaises(ValueError):
            RegHalf(256, RegType.VGPR)
        with self.assertRaises(ValueError):
            RegHalf(-1, RegType.VGPR)

    def test_operand_disassembly(self) -> None:
        v = RegHalf(2, RegType.VGPR, HalfSelect.LO)
        op_plain = VOP3Operand(v)
        self.assertEqual(op_plain.disassemble(), "v2.l")

        op_abs = VOP3Operand(v, abs_mod=True)
        self.assertEqual(op_abs.disassemble(), "|v2.l|")

        op_neg = VOP3Operand(v, neg_mod=True)
        self.assertEqual(op_neg.disassemble(), "-v2.l")

        op_neg_abs = VOP3Operand(v, abs_mod=True, neg_mod=True)
        self.assertEqual(op_neg_abs.disassemble(), "-|v2.l|")

        lit = Literal16.from_float(4.0)
        op_lit = VOP3Operand(lit)
        self.assertEqual(op_lit.disassemble(), "4")

    def test_v_sqrt_f16_ast_construction_and_validation(self) -> None:
        dst = RegHalf(0, RegType.VGPR, HalfSelect.LO)
        src0 = VOP3Operand(RegHalf(2, RegType.VGPR, HalfSelect.HI), abs_mod=True)
        instr = VSqrtF16Instruction(dst=dst, src0=src0, clamp=True)

        self.assertEqual(instr.disassemble(), "v_sqrt_f16 v0.l, |v2.h| clamp")

        # Invalid destination type (SGPR)
        with self.assertRaises(TypeError):
            VSqrtF16Instruction(dst=RegHalf(0, RegType.SGPR), src0=src0)

    def test_structural_equality(self) -> None:
        i1 = VSqrtF16Instruction(
            RegHalf(0, RegType.VGPR, HalfSelect.LO),
            VOP3Operand(RegHalf(2, RegType.VGPR, HalfSelect.HI)),
        )
        i2 = VSqrtF16Instruction(
            RegHalf(0, RegType.VGPR, HalfSelect.LO),
            VOP3Operand(RegHalf(2, RegType.VGPR, HalfSelect.HI)),
        )
        self.assertEqual(i1, i2)


class TestScalarInterpreter(unittest.TestCase):
    """Unit tests for the deterministic scalar interpreter."""

    def setUp(self) -> None:
        self.interp = ScalarInterpreter()
        self.state = ScalarMachineState()

    def test_scalar_vgpr_to_vgpr_lo(self) -> None:
        self.state.set_vgpr16(2, HalfSelect.LO, FOUR_F16)
        instr = VSqrtF16Instruction(
            dst=RegHalf(0, RegType.VGPR, HalfSelect.LO),
            src0=VOP3Operand(RegHalf(2, RegType.VGPR, HalfSelect.LO)),
        )
        res_bits = self.interp.step(instr, self.state)
        self.assertEqual(res_bits, TWO_F16)
        self.assertEqual(self.state.get_vgpr16(0, HalfSelect.LO), TWO_F16)

    def test_scalar_vgpr_hi_preserves_lo(self) -> None:
        self.state.vgprs[0] = 0x5555AAAA
        self.state.set_vgpr16(4, HalfSelect.LO, NINE_F16)
        instr = VSqrtF16Instruction(
            dst=RegHalf(0, RegType.VGPR, HalfSelect.HI),
            src0=VOP3Operand(RegHalf(4, RegType.VGPR, HalfSelect.LO)),
        )
        res_bits = self.interp.step(instr, self.state)
        self.assertEqual(res_bits, THREE_F16)
        self.assertEqual(self.state.get_vgpr16(0, HalfSelect.HI), THREE_F16)
        self.assertEqual(self.state.get_vgpr16(0, HalfSelect.LO), 0xAAAA)

    def test_scalar_sgpr_source(self) -> None:
        self.state.set_sgpr16(8, HalfSelect.LO, ONE_F16)
        instr = VSqrtF16Instruction(
            dst=RegHalf(1, RegType.VGPR, HalfSelect.LO),
            src0=VOP3Operand(RegHalf(8, RegType.SGPR, HalfSelect.LO)),
        )
        res_bits = self.interp.step(instr, self.state)
        self.assertEqual(res_bits, ONE_F16)
        self.assertEqual(self.state.get_vgpr16(1, HalfSelect.LO), ONE_F16)

    def test_scalar_literal_source(self) -> None:
        instr = VSqrtF16Instruction(
            dst=RegHalf(3, RegType.VGPR, HalfSelect.LO),
            src0=VOP3Operand(Literal16.from_float(0.25)),
        )
        res_bits = self.interp.step(instr, self.state)
        self.assertEqual(res_bits, HALF_F16)
        self.assertEqual(self.state.get_vgpr16(3, HalfSelect.LO), HALF_F16)

    def test_scalar_with_modifiers_and_clamp(self) -> None:
        self.state.set_vgpr16(5, HalfSelect.LO, FOUR_F16 | SIGN_MASK)  # -4.0
        instr = VSqrtF16Instruction(
            dst=RegHalf(6, RegType.VGPR, HalfSelect.LO),
            src0=VOP3Operand(RegHalf(5, RegType.VGPR, HalfSelect.LO), abs_mod=True),
            clamp=True,
        )
        res_bits = self.interp.step(instr, self.state)
        self.assertEqual(res_bits, ONE_F16)
        self.assertEqual(self.state.get_vgpr16(6, HalfSelect.LO), ONE_F16)

    def test_scalar_determinism(self) -> None:
        runs = []
        for _ in range(50):
            st = ScalarMachineState()
            st.set_vgpr16(0, HalfSelect.LO, 0x4880)  # 9.0
            instr = VSqrtF16Instruction(
                dst=RegHalf(1, RegType.VGPR, HalfSelect.LO),
                src0=VOP3Operand(RegHalf(0, RegType.VGPR, HalfSelect.LO)),
            )
            runs.append(self.interp.step(instr, st))
        self.assertTrue(all(r == runs[0] for r in runs))


class TestSIMDMultiLaneDivergence(unittest.TestCase):
    """Unit tests for SIMD execution, lane divergence, and 32/64-bit EXEC masking."""

    def setUp(self) -> None:
        self.interp = SIMDInterpreter()

    def test_wave32_full_exec(self) -> None:
        state = SIMDState(num_lanes=32, exec_mask=0xFFFF_FFFF)
        # Provide positive squares 1.0, 4.0, 9.0, 16.0 ...
        for lane in range(32):
            val = float((lane % 6 + 1) ** 2)
            state.set_vgpr16(lane, 0, HalfSelect.LO, f16_to_bits(val))

        instr = VSqrtF16Instruction(
            dst=RegHalf(2, RegType.VGPR, HalfSelect.LO),
            src0=VOP3Operand(RegHalf(0, RegType.VGPR, HalfSelect.LO)),
        )
        results = self.interp.step(instr, state)
        for lane in range(32):
            self.assertIsNotNone(results[lane])
            expected = float(lane % 6 + 1)
            self.assertEqual(bits_to_f16(state.get_vgpr16(lane, 2, HalfSelect.LO)), expected)

    def test_wave32_divergent_masking(self) -> None:
        sentinel = 0xDEAD
        state = SIMDState(num_lanes=32, exec_mask=0x5555_5555)
        for lane in range(32):
            state.set_vgpr16(lane, 0, HalfSelect.LO, FOUR_F16)
            state.set_vgpr16(lane, 2, HalfSelect.LO, sentinel)

        instr = VSqrtF16Instruction(
            dst=RegHalf(2, RegType.VGPR, HalfSelect.LO),
            src0=VOP3Operand(RegHalf(0, RegType.VGPR, HalfSelect.LO)),
        )
        results = self.interp.step(instr, state)
        for lane in range(32):
            if lane % 2 == 0:
                self.assertIsNotNone(results[lane])
                self.assertEqual(state.get_vgpr16(lane, 2, HalfSelect.LO), TWO_F16)
            else:
                self.assertIsNone(results[lane])
                self.assertEqual(state.get_vgpr16(lane, 2, HalfSelect.LO), sentinel)

    def test_wave64_full_exec(self) -> None:
        state = SIMDState(num_lanes=64, exec_mask=0xFFFF_FFFF_FFFF_FFFF)
        for lane in range(64):
            state.set_vgpr16(lane, 0, HalfSelect.LO, NINE_F16)

        instr = VSqrtF16Instruction(
            dst=RegHalf(1, RegType.VGPR, HalfSelect.LO),
            src0=VOP3Operand(RegHalf(0, RegType.VGPR, HalfSelect.LO)),
        )
        self.interp.step(instr, state)
        for lane in range(64):
            self.assertEqual(state.get_vgpr16(lane, 1, HalfSelect.LO), THREE_F16)

    def test_wave64_half_disabled(self) -> None:
        sentinel = 0xBEEF
        state = SIMDState(num_lanes=64, exec_mask=0x0000_0000_FFFF_FFFF)
        for lane in range(64):
            state.set_vgpr16(lane, 0, HalfSelect.LO, FOUR_F16)
            state.set_vgpr16(lane, 2, HalfSelect.LO, sentinel)

        instr = VSqrtF16Instruction(
            dst=RegHalf(2, RegType.VGPR, HalfSelect.LO),
            src0=VOP3Operand(RegHalf(0, RegType.VGPR, HalfSelect.LO)),
        )
        results = self.interp.step(instr, state)
        for lane in range(64):
            if lane < 32:
                self.assertIsNotNone(results[lane])
                self.assertEqual(state.get_vgpr16(lane, 2, HalfSelect.LO), TWO_F16)
            else:
                self.assertIsNone(results[lane])
                self.assertEqual(state.get_vgpr16(lane, 2, HalfSelect.LO), sentinel)

    def test_wave_exec_all_zero(self) -> None:
        sentinel = 0xAA55
        state = SIMDState(num_lanes=32, exec_mask=0x0000_0000)
        for lane in range(32):
            state.set_vgpr16(lane, 0, HalfSelect.LO, sentinel)

        instr = VSqrtF16Instruction(
            dst=RegHalf(0, RegType.VGPR, HalfSelect.LO),
            src0=VOP3Operand(Literal16.from_float(4.0)),
        )
        results = self.interp.step(instr, state)
        self.assertTrue(all(r is None for r in results))
        for lane in range(32):
            self.assertEqual(state.get_vgpr16(lane, 0, HalfSelect.LO), sentinel)

    def test_simd_sgpr_uniform_broadcast(self) -> None:
        state = SIMDState(num_lanes=32, exec_mask=0xFFFF_FFFF)
        state.set_sgpr16(10, HalfSelect.LO, FOUR_F16)

        instr = VSqrtF16Instruction(
            dst=RegHalf(2, RegType.VGPR, HalfSelect.LO),
            src0=VOP3Operand(RegHalf(10, RegType.SGPR, HalfSelect.LO)),
        )
        self.interp.step(instr, state)
        for lane in range(32):
            self.assertEqual(state.get_vgpr16(lane, 2, HalfSelect.LO), TWO_F16)


class TestRegisterAliasing(unittest.TestCase):
    """Unit tests for register aliasing and in-place operand preservation."""

    def setUp(self) -> None:
        self.scalar_interp = ScalarInterpreter()
        self.simd_interp = SIMDInterpreter()

    def test_dst_alias_src0_lo_to_lo(self) -> None:
        state = ScalarMachineState()
        state.set_vgpr16(0, HalfSelect.LO, FOUR_F16)
        instr = VSqrtF16Instruction(
            dst=RegHalf(0, RegType.VGPR, HalfSelect.LO),
            src0=VOP3Operand(RegHalf(0, RegType.VGPR, HalfSelect.LO)),
        )
        self.scalar_interp.step(instr, state)
        self.assertEqual(state.get_vgpr16(0, HalfSelect.LO), TWO_F16)

    def test_dst_alias_src0_hi_to_hi(self) -> None:
        state = ScalarMachineState()
        state.set_vgpr16(0, HalfSelect.HI, NINE_F16)
        instr = VSqrtF16Instruction(
            dst=RegHalf(0, RegType.VGPR, HalfSelect.HI),
            src0=VOP3Operand(RegHalf(0, RegType.VGPR, HalfSelect.HI)),
        )
        self.scalar_interp.step(instr, state)
        self.assertEqual(state.get_vgpr16(0, HalfSelect.HI), THREE_F16)

    def test_dst_hi_src_lo_cross_half_alias(self) -> None:
        state = ScalarMachineState()
        state.set_vgpr16(0, HalfSelect.LO, FOUR_F16)
        state.set_vgpr16(0, HalfSelect.HI, 0x1234)
        instr = VSqrtF16Instruction(
            dst=RegHalf(0, RegType.VGPR, HalfSelect.HI),
            src0=VOP3Operand(RegHalf(0, RegType.VGPR, HalfSelect.LO)),
        )
        self.scalar_interp.step(instr, state)
        self.assertEqual(state.get_vgpr16(0, HalfSelect.HI), TWO_F16)
        self.assertEqual(state.get_vgpr16(0, HalfSelect.LO), FOUR_F16)

    def test_dst_lo_src_hi_cross_half_alias(self) -> None:
        state = ScalarMachineState()
        state.set_vgpr16(0, HalfSelect.HI, NINE_F16)
        state.set_vgpr16(0, HalfSelect.LO, 0x5678)
        instr = VSqrtF16Instruction(
            dst=RegHalf(0, RegType.VGPR, HalfSelect.LO),
            src0=VOP3Operand(RegHalf(0, RegType.VGPR, HalfSelect.HI)),
        )
        self.scalar_interp.step(instr, state)
        self.assertEqual(state.get_vgpr16(0, HalfSelect.LO), THREE_F16)
        self.assertEqual(state.get_vgpr16(0, HalfSelect.HI), NINE_F16)

    def test_inplace_iterative_evolution(self) -> None:
        state = ScalarMachineState()
        state.set_vgpr16(0, HalfSelect.LO, f16_to_bits(16.0))
        instr = VSqrtF16Instruction(
            dst=RegHalf(0, RegType.VGPR, HalfSelect.LO),
            src0=VOP3Operand(RegHalf(0, RegType.VGPR, HalfSelect.LO)),
        )
        self.scalar_interp.step(instr, state)
        self.assertEqual(state.get_vgpr16(0, HalfSelect.LO), FOUR_F16)
        self.scalar_interp.step(instr, state)
        self.assertEqual(state.get_vgpr16(0, HalfSelect.LO), TWO_F16)

    def test_simd_multi_lane_aliasing(self) -> None:
        state = SIMDState(num_lanes=32, exec_mask=0xFFFF_FFFF)
        for lane in range(32):
            state.set_vgpr16(lane, 0, HalfSelect.LO, FOUR_F16)
        instr = VSqrtF16Instruction(
            dst=RegHalf(0, RegType.VGPR, HalfSelect.LO),
            src0=VOP3Operand(RegHalf(0, RegType.VGPR, HalfSelect.LO)),
        )
        self.simd_interp.step(instr, state)
        for lane in range(32):
            self.assertEqual(state.get_vgpr16(lane, 0, HalfSelect.LO), TWO_F16)


# ==============================================================================
# Condition Codes, Status Flags, and Carry/Borrow Generation
# ==============================================================================

@dataclass(frozen=True)
class FPStatusFlags:
    """Architectural floating-point condition codes and IEEE 754 status flags."""
    invalid: bool = False
    overflow: bool = False
    underflow: bool = False
    inexact: bool = False
    zero: bool = False
    negative: bool = False
    denormal: bool = False
    infinite: bool = False
    nan: bool = False


def evaluate_v_sqrt_f16_flags(
    src0_bits: int,
    res_bits: int,
    abs0: bool = False,
    neg0: bool = False,
    clamp: bool = False,
) -> FPStatusFlags:
    """Evaluate IEEE 754 architectural flags and condition codes for v_sqrt_f16."""
    s0 = apply_f16_modifiers(src0_bits, abs0, neg0)
    s0_nan = is_nan(s0)
    s0_snan = is_snan(s0)
    s0_inf = is_inf(s0)
    s0_neg = bool(s0 & SIGN_MASK) and (s0 != NEG_ZERO)

    invalid = False
    if s0_snan or (s0_neg and not s0_nan):
        invalid = True

    nan_flag = is_nan(res_bits)
    inf_flag = is_inf(res_bits)
    zero_flag = is_zero(res_bits)
    neg_flag = bool(res_bits & SIGN_MASK)
    denorm_flag = is_subnormal(res_bits)

    overflow = False
    underflow = False

    inexact = False
    if not (s0_nan or s0_inf or nan_flag or inf_flag or zero_flag):
        f0 = bits_to_f16(s0)
        res_f = bits_to_f16(res_bits)
        if Fraction(res_f) * Fraction(res_f) != Fraction(f0):
            inexact = True

    return FPStatusFlags(
        invalid=invalid,
        overflow=overflow,
        underflow=underflow,
        inexact=inexact,
        zero=zero_flag,
        negative=neg_flag,
        denormal=denorm_flag,
        infinite=inf_flag,
        nan=nan_flag,
    )


def compute_f16_root_borrow_carry(s0_bits: int) -> Tuple[int, int]:
    """Demonstrate binary radix-2 digit-recurrence square root trial subtraction."""
    _, exp0, frac0 = split_f16_bits(s0_bits)
    m = (1 << 10) | frac0 if exp0 != 0 else frac0
    rem = m
    trial_sub = (1 << 10)
    borrow = 1 if rem < trial_sub else 0
    root = 1 if borrow == 0 else 0
    return root, borrow


class TestConditionCodesAndStatusFlags(unittest.TestCase):
    """Unit tests for status flags, condition codes, and carry/borrow generation."""

    def test_exact_finite_flags(self) -> None:
        s0 = FOUR_F16
        res = oracle_v_sqrt_f16(s0)
        flags = evaluate_v_sqrt_f16_flags(s0, res)
        self.assertFalse(flags.invalid)
        self.assertFalse(flags.overflow)
        self.assertFalse(flags.underflow)
        self.assertFalse(flags.inexact)
        self.assertFalse(flags.zero)
        self.assertFalse(flags.negative)
        self.assertFalse(flags.denormal)
        self.assertFalse(flags.infinite)
        self.assertFalse(flags.nan)

    def test_inexact_flag(self) -> None:
        s0 = TWO_F16  # sqrt(2.0) is irrational, inexact
        res = oracle_v_sqrt_f16(s0)
        flags = evaluate_v_sqrt_f16_flags(s0, res)
        self.assertTrue(flags.inexact)
        self.assertFalse(flags.invalid)

    def test_invalid_operation_flag(self) -> None:
        # Negative operand (-4.0) triggers invalid operation
        s0_neg = FOUR_F16 | SIGN_MASK
        res_neg = oracle_v_sqrt_f16(s0_neg)
        flags_neg = evaluate_v_sqrt_f16_flags(s0_neg, res_neg)
        self.assertTrue(flags_neg.invalid)
        self.assertTrue(flags_neg.nan)

        # Signaling NaN triggers invalid operation
        snan = 0x7C01
        res_snan = oracle_v_sqrt_f16(snan)
        flags_snan = evaluate_v_sqrt_f16_flags(snan, res_snan)
        self.assertTrue(flags_snan.invalid)
        self.assertTrue(flags_snan.nan)

    def test_zero_and_negative_flags(self) -> None:
        # +0.0
        res_pz = oracle_v_sqrt_f16(POS_ZERO)
        flags_pz = evaluate_v_sqrt_f16_flags(POS_ZERO, res_pz)
        self.assertTrue(flags_pz.zero)
        self.assertFalse(flags_pz.negative)

        # -0.0
        res_nz = oracle_v_sqrt_f16(NEG_ZERO)
        flags_nz = evaluate_v_sqrt_f16_flags(NEG_ZERO, res_nz)
        self.assertTrue(flags_nz.zero)
        self.assertTrue(flags_nz.negative)

    def test_infinite_flag(self) -> None:
        res_inf = oracle_v_sqrt_f16(POS_INF)
        flags_inf = evaluate_v_sqrt_f16_flags(POS_INF, res_inf)
        self.assertTrue(flags_inf.infinite)
        self.assertFalse(flags_inf.invalid)

    def test_root_borrow_carry_generation(self) -> None:
        # For normal number, mantissa >= 1024, borrow = 0, root bit = 1
        root1, borrow1 = compute_f16_root_borrow_carry(ONE_F16)
        self.assertEqual(borrow1, 0)
        self.assertEqual(root1, 1)

        # For subnormal number with frac < 1024, borrow = 1, root bit = 0
        root0, borrow0 = compute_f16_root_borrow_carry(0x0001)
        self.assertEqual(borrow0, 1)
        self.assertEqual(root0, 0)


class TestIEEE754BoundaryExtremes(unittest.TestCase):
    """Unit tests for IEEE 754 boundary extremes, subnormals, and NaN handling."""

    def test_subnormal_square_root_precision(self) -> None:
        # Smallest subnormal: 2^-24 -> sqrt(2^-24) = 2^-12 = 0x0C00
        min_sub = 0x0001
        res = oracle_v_sqrt_f16(min_sub)
        self.assertEqual(res, 0x0C00)
        self.assertFalse(is_subnormal(res))
        self.assertEqual(bits_to_f16(res), 2.0 ** -12)

        # 4 * 2^-24 -> sqrt is 2 * 2^-12 = 2^-11 = 0x1000
        sub_four = 0x0004
        res_four = oracle_v_sqrt_f16(sub_four)
        self.assertEqual(res_four, 0x1000)
        self.assertEqual(bits_to_f16(res_four), 2.0 ** -11)

    def test_subnormal_produces_normal(self) -> None:
        # Square root of any positive non-zero subnormal in binary16 produces a normal number
        for sub in [0x0001, 0x0002, 0x0010, 0x0100, 0x03FF]:
            with self.subTest(sub=hex(sub)):
                res = oracle_v_sqrt_f16(sub)
                self.assertFalse(is_subnormal(res))
                _, exp, _ = split_f16_bits(res)
                self.assertGreater(exp, 0)

    def test_largest_subnormal_sqrt(self) -> None:
        max_sub = 0x03FF
        res = oracle_v_sqrt_f16(max_sub)
        expected = f16_to_bits(math.sqrt(bits_to_f16(max_sub)))
        self.assertEqual(res, expected)

    def test_nan_signaling_to_quiet_conversion(self) -> None:
        snan = 0x7C05
        self.assertTrue(is_snan(snan))
        res = oracle_v_sqrt_f16(snan)
        self.assertTrue(is_qnan(res))
        self.assertEqual(res, snan | QNAN_BIT)

    def test_nan_payload_preservation(self) -> None:
        payload_qnan = 0x7E33
        res = oracle_v_sqrt_f16(payload_qnan)
        self.assertEqual(res, payload_qnan)

    def test_nan_with_modifiers(self) -> None:
        qnan = 0x7E12
        # abs clears sign bit on negative NaN
        res_abs = oracle_v_sqrt_f16(qnan | SIGN_MASK, abs0=True)
        self.assertEqual(res_abs & SIGN_MASK, 0)
        self.assertTrue(is_qnan(res_abs))

        # neg sets sign bit on positive NaN
        res_neg = oracle_v_sqrt_f16(qnan, neg0=True)
        self.assertEqual(res_neg & SIGN_MASK, SIGN_MASK)

    def test_nan_with_clamp(self) -> None:
        qnan = CANONICAL_QNAN
        res = oracle_v_sqrt_f16(qnan, clamp=True)
        self.assertEqual(res, POS_ZERO)

    def test_infinity_arithmetic_boundaries(self) -> None:
        self.assertEqual(oracle_v_sqrt_f16(POS_INF), POS_INF)
        self.assertEqual(oracle_v_sqrt_f16(NEG_INF), CANONICAL_QNAN)
        self.assertEqual(oracle_v_sqrt_f16(POS_INF, clamp=True), ONE_F16)
        self.assertEqual(oracle_v_sqrt_f16(NEG_INF, clamp=True), POS_ZERO)

    def test_round_to_nearest_even_ties(self) -> None:
        # In binary16, verify RNE rounding on values around 1.0 + epsilon
        for val in [1.0, 1.5, 2.0, 3.0, 5.0, 7.0]:
            bits = f16_to_bits(val)
            res = oracle_v_sqrt_f16(bits)
            expected = f16_to_bits(math.sqrt(val))
            self.assertEqual(res, expected)


class TestNegativeControlsAndMutations(unittest.TestCase):
    """Unit tests for negative controls, syntax rejection, and mutation detection."""

    def test_syntax_rejection_invalid_register_indices(self) -> None:
        with self.assertRaises(ValueError):
            RegHalf(-1, RegType.VGPR)
        with self.assertRaises(ValueError):
            RegHalf(256, RegType.VGPR)
        with self.assertRaises(ValueError):
            RegHalf(300, RegType.SGPR)

    def test_syntax_rejection_valu_destination_must_be_vgpr(self) -> None:
        dst_sgpr = RegHalf(0, RegType.SGPR)
        src = VOP3Operand(RegHalf(2, RegType.VGPR))
        with self.assertRaises(TypeError):
            VSqrtF16Instruction(dst=dst_sgpr, src0=src)

    def test_syntax_rejection_invalid_lane_count(self) -> None:
        with self.assertRaises(ValueError):
            SIMDState(num_lanes=16)
        with self.assertRaises(ValueError):
            SIMDState(num_lanes=128)

    def test_negative_control_bit_flip_detection(self) -> None:
        s0 = FOUR_F16
        correct = oracle_v_sqrt_f16(s0)
        self.assertEqual(correct, TWO_F16)

        # Perturb single mantissa bit
        corrupted_mantissa = correct ^ 0x1
        self.assertNotEqual(correct, corrupted_mantissa)

        # Perturb single exponent bit
        corrupted_exp = correct ^ (1 << 10)
        self.assertNotEqual(correct, corrupted_exp)

        # Perturb sign bit
        corrupted_sign = correct ^ SIGN_MASK
        self.assertNotEqual(correct, corrupted_sign)

    def test_mutation_sqrt_to_identity_or_rcp(self) -> None:
        """Mutant: Return input operand unchanged instead of square root."""
        def mutant_sqrt(s0_bits: int) -> int:
            return s0_bits

        s0 = FOUR_F16
        correct = oracle_v_sqrt_f16(s0)
        mutant = mutant_sqrt(s0)
        self.assertNotEqual(correct, mutant)

    def test_mutation_ignore_abs_modifier(self) -> None:
        """Mutant: Ignore abs modifier on negative operand."""
        def mutant_apply_mod(bits: int, abs_mod: bool, neg_mod: bool) -> int:
            res = bits & 0xFFFF
            if neg_mod:
                res ^= SIGN_MASK
            return res

        val = FOUR_F16 | SIGN_MASK  # -4.0
        correct = apply_f16_modifiers(val, abs_mod=True, neg_mod=False)
        mutant = mutant_apply_mod(val, abs_mod=True, neg_mod=False)
        self.assertEqual(correct, FOUR_F16)
        self.assertNotEqual(correct, mutant)

    def test_mutation_ignore_neg_modifier(self) -> None:
        """Mutant: Ignore neg modifier on positive operand."""
        def mutant_apply_mod(bits: int, abs_mod: bool, neg_mod: bool) -> int:
            res = bits & 0xFFFF
            if abs_mod:
                res &= ~SIGN_MASK
            return res

        val = FOUR_F16
        correct = apply_f16_modifiers(val, abs_mod=False, neg_mod=True)
        mutant = mutant_apply_mod(val, abs_mod=False, neg_mod=True)
        self.assertEqual(correct, FOUR_F16 | SIGN_MASK)
        self.assertNotEqual(correct, mutant)

    def test_mutation_ignore_clamp_upper_bound(self) -> None:
        """Mutant: Clamp modifier fails to clamp values exceeding 1.0."""
        s0 = FOUR_F16
        expected = oracle_v_sqrt_f16(s0, clamp=True)
        self.assertEqual(expected, ONE_F16)

        # Mutant unclamped
        unclamped = oracle_v_sqrt_f16(s0, clamp=False)
        self.assertNotEqual(expected, unclamped)

    def test_mutation_ignore_exec_mask(self) -> None:
        """Mutant: Inactive SIMD lanes are overwritten despite EXEC mask."""
        sentinel = 0xCAFE
        state = SIMDState(num_lanes=32, exec_mask=0x0000_0000)
        for lane in range(32):
            state.set_vgpr16(lane, 4, HalfSelect.LO, sentinel)

        class MutantSIMDInterpreter(SIMDInterpreter):
            def step(self, instr: VSqrtF16Instruction, state: SIMDState) -> List[Optional[int]]:
                results = []
                for lane in range(state.num_lanes):
                    res = oracle_v_sqrt_f16(FOUR_F16)
                    state.set_vgpr16(lane, instr.dst.index, instr.dst.half, res)
                    results.append(res)
                return results

        mutant_interp = MutantSIMDInterpreter()
        instr = VSqrtF16Instruction(
            dst=RegHalf(4, RegType.VGPR, HalfSelect.LO),
            src0=VOP3Operand(Literal16.from_float(4.0)),
        )
        mutant_interp.step(instr, state)
        corrupted_lanes = [
            lane for lane in range(32)
            if state.get_vgpr16(lane, 4, HalfSelect.LO) != sentinel
        ]
        self.assertEqual(len(corrupted_lanes), 32)

    def test_mutation_subnormal_flush_to_zero(self) -> None:
        """Mutant: Subnormal input is flushed to zero instead of producing normalized root."""
        sub = 0x0001
        correct = oracle_v_sqrt_f16(sub)
        self.assertEqual(correct, 0x0C00)

        # FTZ mutant
        ftz_mutant = POS_ZERO
        self.assertNotEqual(correct, ftz_mutant)

    def test_mutation_skip_snan_quietening(self) -> None:
        """Mutant: Signaling NaN is not quietened."""
        snan = 0x7C05
        res = oracle_v_sqrt_f16(snan)
        self.assertTrue(is_qnan(res))
        self.assertNotEqual(res, snan)

    def test_mutation_negative_operand_not_producing_nan(self) -> None:
        """Mutant: Negative operand returns zero instead of Canonical Quiet NaN."""
        s0 = FOUR_F16 | SIGN_MASK
        expected = oracle_v_sqrt_f16(s0)
        self.assertEqual(expected, CANONICAL_QNAN)

        buggy_result = POS_ZERO
        self.assertNotEqual(expected, buggy_result)


if __name__ == "__main__":
    unittest.main()
