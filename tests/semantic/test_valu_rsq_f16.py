"""Semantic tests and reference mathematical oracle for RDNA2 v_rsq_f16.

AMD RDNA2 VALU Instruction Set Architecture (GFX10 / GFX1030 / GFX1035):
  The v_rsq_f16 instruction computes the IEEE-754 binary16 (half-precision) reciprocal
  square root of a 16-bit floating-point operand:
      D.f16 = 1.0 / sqrt(S0.f16)

Mathematical & Architectural Semantics:
  - Exact powers of two and four:
      1.0     -> 1.0     (0x3C00 -> 0x3C00)
      4.0     -> 0.5     (0x4400 -> 0x3800)
      16.0    -> 0.25    (0x4C00 -> 0x3400)
      64.0    -> 0.125   (0x5400 -> 0x3000)
      0.25    -> 2.0     (0x3400 -> 0x4000)
      0.0625  -> 4.0     (0x2C00 -> 0x4400)
      0.015625-> 8.0     (0x2400 -> 0x4800)
  - Special floating-point values:
      +inf    -> +0.0    (IEEE-754 / GLSL 1/sqrt(+inf) = +0.0)
      +0.0    -> +inf    (Division by zero)
      -0.0    -> +inf    (RDNA2 VALU hardware maps -0.0 to +inf)
      qNaN    -> quiet NaN preserved
      sNaN    -> quieted NaN (quiet bit 9 set)
      negative finite (-x) -> Canonical Quiet NaN (square root of negative value)
      -inf    -> Canonical Quiet NaN
  - Subnormal numbers:
      Gradual underflow (FTZ=0): evaluated with full mathematical precision.
          1.0 / sqrt(2^-24) = 1.0 / 2^-12 = 2^12 = 4096.0 (0x6C00)
      Flush-to-zero (FTZ=1): subnormal input flushes to +0.0, yielding +inf.
  - VOP3 / VOP3P modifiers:
      abs: Clears sign bit of S0 prior to evaluation.
      neg: Toggles sign bit of S0 prior to evaluation.
      clamp: Clamps output value to [0.0, 1.0]; clamped NaN produces +0.0.
      omod: Output scaling (mul:2, mul:4, div:2) applied prior to clamp.

Standard library Python only (Python 3.9+ compatible, unittest, zero game data).
"""
from __future__ import annotations

import math
import struct
import unittest
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional, Tuple, Union

# IEEE-754 binary16 (half-precision) architectural constants
SIGN_MASK_16: int = 0x8000
EXP_MASK_16: int = 0x7C00
FRAC_MASK_16: int = 0x03FF
QNAN_BIT_16: int = 0x0200
CANONICAL_QNAN_16: int = 0x7E00

# Canonical binary16 encodings
H_POS_ZERO: int = 0x0000
H_NEG_ZERO: int = 0x8000
H_POS_INF: int = 0x7C00
H_NEG_INF: int = 0xFC00
H_QNAN: int = 0x7E00
H_SNAN: int = 0x7D00

H_ONE: int = 0x3C00          # 1.0
H_FOUR: int = 0x4400         # 4.0
H_SIXTEEN: int = 0x4C00      # 16.0
H_SIXTY_FOUR: int = 0x5400   # 64.0
H_QUARTER: int = 0x3400      # 0.25
H_HALF: int = 0x3800         # 0.5
H_TWO: int = 0x4000          # 2.0
H_EIGHT: int = 0x4800        # 8.0
H_0P0625: int = 0x2C00       # 0.0625 (1/16)
H_0P125: int = 0x3000        # 0.125 (1/8)
H_4096: int = 0x6C00         # 4096.0 (2^12)

H_MAX_NORMAL: int = 0x7BFF   # 65504.0
H_MIN_NORMAL: int = 0x0400   # 2^-14 (~6.10e-5)
H_MIN_SUBNORMAL: int = 0x0001  # 2^-24 (~5.96e-8)
H_MAX_SUBNORMAL: int = 0x03FF  # 1023 * 2^-24


def f16_to_bits(val: float) -> int:
    """Convert a Python float to 16-bit IEEE-754 half-precision integer bits."""
    if math.isnan(val):
        sign = 0x8000 if math.copysign(1.0, val) < 0.0 else 0x0000
        return sign | CANONICAL_QNAN_16
    if math.isinf(val):
        return H_POS_INF if val > 0.0 else H_NEG_INF
    if val >= 65520.0:
        return H_POS_INF
    if val <= -65520.0:
        return H_NEG_INF
    return struct.unpack("<H", struct.pack("<e", val))[0]


def bits_to_f16(bits: int) -> float:
    """Convert 16-bit integer bits to a Python float via binary16 format."""
    return struct.unpack("<e", struct.pack("<H", bits & 0xFFFF))[0]


def split_f16_bits(bits: int) -> Tuple[int, int, int]:
    """Split 16-bit halfword into (sign, exponent, fraction) components."""
    bits &= 0xFFFF
    sign = (bits & SIGN_MASK_16) >> 15
    exponent = (bits & EXP_MASK_16) >> 10
    fraction = bits & FRAC_MASK_16
    return sign, exponent, fraction


def is_nan_f16(bits: int) -> bool:
    """Return True if 16-bit pattern represents an IEEE-754 NaN."""
    return ((bits & EXP_MASK_16) == EXP_MASK_16) and ((bits & FRAC_MASK_16) != 0)


def is_qnan_f16(bits: int) -> bool:
    """Return True if 16-bit pattern represents a Quiet NaN."""
    return is_nan_f16(bits) and bool(bits & QNAN_BIT_16)


def is_snan_f16(bits: int) -> bool:
    """Return True if 16-bit pattern represents a Signaling NaN."""
    return is_nan_f16(bits) and not bool(bits & QNAN_BIT_16)


def is_inf_f16(bits: int) -> bool:
    """Return True if 16-bit pattern represents positive or negative infinity."""
    return ((bits & EXP_MASK_16) == EXP_MASK_16) and ((bits & FRAC_MASK_16) == 0)


def is_subnormal_f16(bits: int) -> bool:
    """Return True if 16-bit pattern represents a subnormal half-precision float."""
    return ((bits & EXP_MASK_16) == 0) and ((bits & FRAC_MASK_16) != 0)


def is_zero_f16(bits: int) -> bool:
    """Return True if 16-bit pattern represents positive or negative zero."""
    return (bits & ~SIGN_MASK_16) == 0


def apply_f16_input_modifiers(bits: int, abs_mod: bool = False, neg_mod: bool = False) -> int:
    """Apply VOP3 input modifiers to 16-bit halfword: abs clears sign bit; neg toggles."""
    res = bits & 0xFFFF
    if abs_mod:
        res &= ~SIGN_MASK_16
    if neg_mod:
        res ^= SIGN_MASK_16
    return res


def oracle_v_rsq_f16(
    src0_bits: int,
    abs_mod: bool = False,
    neg_mod: bool = False,
    clamp: bool = False,
    omod: str = "none",
    ftz: bool = False,
) -> int:
    """Reference mathematical oracle for RDNA2 v_rsq_f16 (reciprocal square root).

    Computes D = 1.0 / sqrt(src0) in IEEE-754 binary16 arithmetic conforming
    to RDNA2 hardware semantics, including modifiers, FTZ mode, and saturation clamp.
    """
    s0 = apply_f16_input_modifiers(src0_bits, abs_mod=abs_mod, neg_mod=neg_mod)

    # 1. NaN handling
    if is_nan_f16(s0):
        if clamp:
            return H_POS_ZERO
        return (s0 | QNAN_BIT_16) & 0xFFFF

    # 2. Flush-to-zero (FTZ) for subnormals
    if ftz and is_subnormal_f16(s0):
        s0 = (s0 & SIGN_MASK_16)  # Flush fraction to zero

    # 3. Signed zero handling: 1.0 / sqrt(+/-0.0) -> +Infinity in RDNA2
    if is_zero_f16(s0):
        if clamp:
            return H_ONE
        return H_POS_INF

    # 4. Negative values (including -inf) produce Quiet NaN
    if bool(s0 & SIGN_MASK_16):
        if clamp:
            return H_POS_ZERO
        return CANONICAL_QNAN_16

    # 5. Positive infinity: 1.0 / sqrt(+inf) = +0.0
    if s0 == H_POS_INF:
        return H_POS_ZERO

    # 6. Finite positive operands
    val = bits_to_f16(s0)
    if val <= 0.0:
        res_float = float("inf")
    else:
        res_float = 1.0 / math.sqrt(val)

    # Apply output scaling modifiers
    if omod == "mul:2":
        res_float *= 2.0
    elif omod == "mul:4":
        res_float *= 4.0
    elif omod == "div:2":
        res_float *= 0.5

    # Apply clamp [0.0, 1.0]
    if clamp:
        if math.isnan(res_float) or res_float < 0.0:
            res_float = 0.0
        elif res_float > 1.0:
            res_float = 1.0

    return f16_to_bits(res_float)


# ---------------------------------------------------------------------------
# Symbolic AST Opcode Definitions and Typed Expressions
# ---------------------------------------------------------------------------

class OperandType(Enum):
    VGPR = auto()
    SGPR = auto()
    INLINE_CONST = auto()
    LITERAL_CONST = auto()


class HalfwordSelect(Enum):
    LOW_16 = auto()
    HIGH_16 = auto()


@dataclass(frozen=True)
class ASTOperand:
    op_type: OperandType
    reg_index: int = 0
    half: HalfwordSelect = HalfwordSelect.LOW_16
    abs_mod: bool = False
    neg_mod: bool = False
    literal_val: Optional[int] = None

    def validate(self) -> None:
        if self.op_type == OperandType.VGPR:
            if not (0 <= self.reg_index <= 255):
                raise ValueError(f"VGPR index out of range: v{self.reg_index}")
        elif self.op_type == OperandType.SGPR:
            if not (0 <= self.reg_index <= 105):
                raise ValueError(f"SGPR index out of range: s{self.reg_index}")
        elif self.op_type == OperandType.LITERAL_CONST:
            if self.literal_val is None or not (0 <= self.literal_val <= 0xFFFF):
                raise ValueError(f"Invalid literal constant: {self.literal_val}")

    def to_assembly(self) -> str:
        half_suffix = ".l" if self.half == HalfwordSelect.LOW_16 else ".h"
        if self.op_type == OperandType.VGPR:
            base = f"v{self.reg_index}{half_suffix}"
        elif self.op_type == OperandType.SGPR:
            base = f"s{self.reg_index}{half_suffix}"
        elif self.op_type == OperandType.INLINE_CONST:
            base = f"{bits_to_f16(self.literal_val or 0):.2f}"
        else:
            base = f"{self.literal_val:#06x}"

        if self.abs_mod:
            base = f"|{base}|"
        if self.neg_mod:
            base = f"-{base}"
        return base


@dataclass(frozen=True)
class ASTRsqF16:
    dst: ASTOperand
    src0: ASTOperand
    clamp: bool = False
    omod: str = "none"
    ftz: bool = False

    def validate(self) -> None:
        self.dst.validate()
        self.src0.validate()
        if self.dst.op_type != OperandType.VGPR:
            raise TypeError("Destination operand for v_rsq_f16 must be a VGPR")
        if self.omod not in ("none", "mul:2", "mul:4", "div:2"):
            raise ValueError(f"Invalid omod modifier: {self.omod}")

    def to_assembly(self) -> str:
        parts = ["v_rsq_f16", f"{self.dst.to_assembly()},", self.src0.to_assembly()]
        if self.clamp:
            parts.append("clamp")
        if self.omod != "none":
            parts.append(self.omod)
        return " ".join(parts)

    def evaluate_symbolic(self, env: Dict[str, int]) -> int:
        self.validate()
        if self.src0.op_type == OperandType.VGPR:
            var_name = f"v{self.src0.reg_index}{'.l' if self.src0.half == HalfwordSelect.LOW_16 else '.h'}"
            src_val = env.get(var_name, 0)
        elif self.src0.op_type == OperandType.SGPR:
            var_name = f"s{self.src0.reg_index}{'.l' if self.src0.half == HalfwordSelect.LOW_16 else '.h'}"
            src_val = env.get(var_name, 0)
        elif self.src0.literal_val is not None:
            src_val = self.src0.literal_val
        else:
            src_val = 0

        return oracle_v_rsq_f16(
            src_val,
            abs_mod=self.src0.abs_mod,
            neg_mod=self.src0.neg_mod,
            clamp=self.clamp,
            omod=self.omod,
            ftz=self.ftz,
        )


@dataclass(frozen=True)
class SymbolicExpr:
    op: str
    args: Tuple[Union[SymbolicExpr, str, float, int], ...]

    def simplify(self) -> SymbolicExpr:
        if self.op == "abs":
            arg = self.args[0]
            if isinstance(arg, SymbolicExpr):
                if arg.op == "abs":
                    return arg.simplify()
                if arg.op == "neg":
                    return SymbolicExpr("abs", (arg.args[0],)).simplify()
        elif self.op == "neg":
            arg = self.args[0]
            if isinstance(arg, SymbolicExpr) and arg.op == "neg":
                inner = arg.args[0]
                if isinstance(inner, SymbolicExpr):
                    return inner.simplify()
                return inner  # type: ignore
        return self


# ---------------------------------------------------------------------------
# Deterministic Scalar Instruction Interpreter
# ---------------------------------------------------------------------------

class RegisterFile:
    """Architectural register file containing 256 VGPRs and 106 SGPRs (32-bit dwords)."""

    def __init__(self) -> None:
        self.vgprs: List[int] = [0] * 256
        self.sgprs: List[int] = [0] * 106

    def read_vgpr(self, reg_index: int) -> int:
        return self.vgprs[reg_index] & 0xFFFFFFFF

    def write_vgpr(self, reg_index: int, value: int) -> None:
        self.vgprs[reg_index] = value & 0xFFFFFFFF

    def read_vgpr_half(self, reg_index: int, half: HalfwordSelect) -> int:
        dword = self.read_vgpr(reg_index)
        if half == HalfwordSelect.LOW_16:
            return dword & 0xFFFF
        return (dword >> 16) & 0xFFFF

    def write_vgpr_half(self, reg_index: int, half: HalfwordSelect, value_16: int) -> None:
        current = self.read_vgpr(reg_index)
        val = value_16 & 0xFFFF
        if half == HalfwordSelect.LOW_16:
            updated = (current & 0xFFFF0000) | val
        else:
            updated = (current & 0x0000FFFF) | (val << 16)
        self.write_vgpr(reg_index, updated)

    def read_sgpr_half(self, reg_index: int, half: HalfwordSelect) -> int:
        dword = self.sgprs[reg_index] & 0xFFFFFFFF
        if half == HalfwordSelect.LOW_16:
            return dword & 0xFFFF
        return (dword >> 16) & 0xFFFF

    def write_sgpr_half(self, reg_index: int, half: HalfwordSelect, value_16: int) -> None:
        current = self.sgprs[reg_index] & 0xFFFFFFFF
        val = value_16 & 0xFFFF
        if half == HalfwordSelect.LOW_16:
            updated = (current & 0xFFFF0000) | val
        else:
            updated = (current & 0x0000FFFF) | (val << 16)
        self.sgprs[reg_index] = updated

    def read_operand_16(self, op: ASTOperand) -> int:
        op.validate()
        if op.op_type == OperandType.VGPR:
            raw = self.read_vgpr_half(op.reg_index, op.half)
        elif op.op_type == OperandType.SGPR:
            raw = self.read_sgpr_half(op.reg_index, op.half)
        elif op.op_type in (OperandType.INLINE_CONST, OperandType.LITERAL_CONST):
            raw = (op.literal_val or 0) & 0xFFFF
        else:
            raw = 0
        return raw


class ScalarInterpreter:
    """Deterministic scalar cycle-accurate execution engine for v_rsq_f16."""

    def __init__(self, rf: Optional[RegisterFile] = None) -> None:
        self.rf: RegisterFile = rf if rf is not None else RegisterFile()
        self.trace_log: List[str] = []
        self.cycle_count: int = 0

    def step(self, instr: ASTRsqF16) -> int:
        instr.validate()
        raw_src = self.rf.read_operand_16(instr.src0)
        res_16 = oracle_v_rsq_f16(
            raw_src,
            abs_mod=instr.src0.abs_mod,
            neg_mod=instr.src0.neg_mod,
            clamp=instr.clamp,
            omod=instr.omod,
            ftz=instr.ftz,
        )

        # Write result to destination VGPR halfword while preserving untouched halfword
        self.rf.write_vgpr_half(instr.dst.reg_index, instr.dst.half, res_16)

        # Update trace log & cycle count (transcendental/reciprocal VALU instructions = 4 cycles)
        self.cycle_count += 4
        trace_msg = (
            f"[Cycle {self.cycle_count:04d}] {instr.to_assembly()} => "
            f"dst=v{instr.dst.reg_index}.{('l' if instr.dst.half == HalfwordSelect.LOW_16 else 'h')} = {res_16:#06x}"
        )
        self.trace_log.append(trace_msg)
        return res_16


# ---------------------------------------------------------------------------
# SIMD Multi-Lane Divergence and 32/64-Bit EXEC Masking Model
# ---------------------------------------------------------------------------

class Wavefront:
    """SIMD Wavefront execution model for Wave32 and Wave64 execution on AMD RDNA2."""

    def __init__(self, wave_size: int = 32, exec_mask: Optional[int] = None) -> None:
        if wave_size not in (32, 64):
            raise ValueError(f"Wave size must be 32 or 64, got {wave_size}")
        self.wave_size: int = wave_size
        self.full_mask: int = (1 << wave_size) - 1
        self.exec_mask: int = exec_mask if exec_mask is not None else self.full_mask
        self.lanes: List[RegisterFile] = [RegisterFile() for _ in range(wave_size)]
        self.sgprs: List[int] = [0] * 106

    def is_lane_active(self, lane_id: int) -> bool:
        if not (0 <= lane_id < self.wave_size):
            return False
        return bool((self.exec_mask >> lane_id) & 1)

    def read_shared_sgpr_half(self, reg_index: int, half: HalfwordSelect) -> int:
        dword = self.sgprs[reg_index] & 0xFFFFFFFF
        if half == HalfwordSelect.LOW_16:
            return dword & 0xFFFF
        return (dword >> 16) & 0xFFFF

    def write_shared_sgpr_half(self, reg_index: int, half: HalfwordSelect, value_16: int) -> None:
        dword = self.sgprs[reg_index] & 0xFFFFFFFF
        val = value_16 & 0xFFFF
        if half == HalfwordSelect.LOW_16:
            self.sgprs[reg_index] = (dword & 0xFFFF0000) | val
        else:
            self.sgprs[reg_index] = (dword & 0x0000FFFF) | (val << 16)

    def execute_simd(self, instr: ASTRsqF16) -> List[Optional[int]]:
        instr.validate()
        results: List[Optional[int]] = []

        for lane_id in range(self.wave_size):
            if not self.is_lane_active(lane_id):
                results.append(None)
                continue

            rf = self.lanes[lane_id]
            if instr.src0.op_type == OperandType.SGPR:
                raw_src = self.read_shared_sgpr_half(instr.src0.reg_index, instr.src0.half)
            else:
                raw_src = rf.read_operand_16(instr.src0)

            res_16 = oracle_v_rsq_f16(
                raw_src,
                abs_mod=instr.src0.abs_mod,
                neg_mod=instr.src0.neg_mod,
                clamp=instr.clamp,
                omod=instr.omod,
                ftz=instr.ftz,
            )
            rf.write_vgpr_half(instr.dst.reg_index, instr.dst.half, res_16)
            results.append(res_16)

        return results


# ---------------------------------------------------------------------------
# Condition Codes, Status Flags, and Carry/Borrow Model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StatusFlags:
    """IEEE-754 / RDNA2 floating-point exception status flags."""
    invalid_op: bool = False
    div_by_zero: bool = False
    overflow: bool = False
    underflow: bool = False
    inexact: bool = False


def evaluate_status_flags_rsq_f16(
    src0_bits: int,
    abs_mod: bool = False,
    neg_mod: bool = False,
    ftz: bool = False,
) -> StatusFlags:
    """Evaluate floating-point exception flags according to IEEE-754 / RDNA2 spec."""
    s0 = apply_f16_input_modifiers(src0_bits, abs_mod=abs_mod, neg_mod=neg_mod)

    # Signaling NaN or Negative input (excluding negative zero) raises Invalid Op
    if is_snan_f16(s0):
        return StatusFlags(invalid_op=True)
    if is_nan_f16(s0):
        return StatusFlags()
    if bool(s0 & SIGN_MASK_16) and not is_zero_f16(s0):
        return StatusFlags(invalid_op=True)

    # Division by Zero raised on +/- 0.0
    if is_zero_f16(s0):
        return StatusFlags(div_by_zero=True)

    if s0 == H_POS_INF:
        return StatusFlags()

    # Exact powers of 2 with even exponent: 1/sqrt(2^(2k)) = 2^(-k) are exact
    # In binary16, powers of 4 (e.g. 0.0625, 0.25, 1.0, 4.0, 16.0, 64.0) have fraction == 0
    # and even biased exponent.
    _, exp, frac = split_f16_bits(s0)
    is_exact_power = (frac == 0) and ((exp - 15) % 2 == 0)
    return StatusFlags(inexact=not is_exact_power)


def classify_f16(bits: int) -> int:
    """Compute RDNA2 v_cmp_class_f16 classification bitmask.

    Bits:
      0: sNaN, 1: qNaN, 2: -inf, 3: -normal, 4: -subnormal,
      5: -zero, 6: +zero, 7: +subnormal, 8: +normal, 9: +inf
    """
    sign, exp, frac = split_f16_bits(bits)
    if exp == 0x1F:
        if frac == 0:
            return 1 << 2 if sign else 1 << 9
        return 1 << 0 if is_snan_f16(bits) else 1 << 1
    if exp == 0:
        if frac == 0:
            return 1 << 5 if sign else 1 << 6
        return 1 << 4 if sign else 1 << 7
    return 1 << 3 if sign else 1 << 8


# ---------------------------------------------------------------------------
# Unit Test Suites
# ---------------------------------------------------------------------------

class TestReferenceOracleRSQF16(unittest.TestCase):
    """Unit tests verifying the mathematical reference oracle for v_rsq_f16."""

    def test_exact_powers_of_four_and_two(self) -> None:
        table = [
            (H_ONE, H_ONE, "1.0 -> 1.0"),
            (H_FOUR, H_HALF, "4.0 -> 0.5"),
            (H_SIXTEEN, H_QUARTER, "16.0 -> 0.25"),
            (H_SIXTY_FOUR, H_0P125, "64.0 -> 0.125"),
            (H_QUARTER, H_TWO, "0.25 -> 2.0"),
            (H_0P0625, H_FOUR, "0.0625 -> 4.0"),
            (0x2400, H_EIGHT, "0.015625 -> 8.0"),
        ]
        for src, expected, desc in table:
            with self.subTest(msg=desc):
                self.assertEqual(oracle_v_rsq_f16(src), expected)

    def test_special_floating_point_boundaries(self) -> None:
        self.assertEqual(oracle_v_rsq_f16(H_POS_ZERO), H_POS_INF)
        self.assertEqual(oracle_v_rsq_f16(H_NEG_ZERO), H_POS_INF)
        self.assertEqual(oracle_v_rsq_f16(H_POS_INF), H_POS_ZERO)
        self.assertTrue(is_nan_f16(oracle_v_rsq_f16(H_NEG_INF)))

    def test_negative_values_produce_quiet_nan(self) -> None:
        for val in (-1.0, -4.0, -16.0, -0.25):
            bits = f16_to_bits(val)
            res = oracle_v_rsq_f16(bits)
            self.assertTrue(is_nan_f16(res))
            self.assertEqual(res, CANONICAL_QNAN_16)

    def test_nan_inputs_quietened(self) -> None:
        self.assertEqual(oracle_v_rsq_f16(H_QNAN), H_QNAN)
        res_snan = oracle_v_rsq_f16(H_SNAN)
        self.assertTrue(is_qnan_f16(res_snan))

    def test_input_modifiers(self) -> None:
        # abs(-4.0) -> +4.0 -> 0.5
        neg_four = f16_to_bits(-4.0)
        self.assertEqual(oracle_v_rsq_f16(neg_four, abs_mod=True), H_HALF)

        # neg(4.0) -> -4.0 -> NaN
        self.assertTrue(is_nan_f16(oracle_v_rsq_f16(H_FOUR, neg_mod=True)))

        # abs + neg of 4.0: abs first clears sign, then neg toggles -> -4.0 -> NaN
        self.assertTrue(is_nan_f16(oracle_v_rsq_f16(H_FOUR, abs_mod=True, neg_mod=True)))

    def test_clamp_and_omod_modifiers(self) -> None:
        # 0.25 -> 2.0; clamped -> 1.0
        self.assertEqual(oracle_v_rsq_f16(H_QUARTER, clamp=True), H_ONE)

        # 4.0 -> 0.5; omod mul:2 -> 1.0
        self.assertEqual(oracle_v_rsq_f16(H_FOUR, omod="mul:2"), H_ONE)
        # 4.0 -> 0.5; omod mul:4 -> 2.0
        self.assertEqual(oracle_v_rsq_f16(H_FOUR, omod="mul:4"), H_TWO)
        # 4.0 -> 0.5; omod div:2 -> 0.25
        self.assertEqual(oracle_v_rsq_f16(H_FOUR, omod="div:2"), H_QUARTER)

        # Clamped NaN -> +0.0
        self.assertEqual(oracle_v_rsq_f16(H_QNAN, clamp=True), H_POS_ZERO)

    def test_subnormal_handling(self) -> None:
        # 2^-24 subnormal min: 1 / sqrt(2^-24) = 4096.0
        self.assertEqual(oracle_v_rsq_f16(H_MIN_SUBNORMAL, ftz=False), H_4096)
        # Under FTZ, flushes to 0.0 -> +inf
        self.assertEqual(oracle_v_rsq_f16(H_MIN_SUBNORMAL, ftz=True), H_POS_INF)


class TestSymbolicASTRsqF16(unittest.TestCase):
    """Unit tests verifying symbolic AST opcode representation and expressions."""

    def test_ast_construction_and_formatting(self) -> None:
        dst = ASTOperand(op_type=OperandType.VGPR, reg_index=0, half=HalfwordSelect.LOW_16)
        src = ASTOperand(
            op_type=OperandType.VGPR,
            reg_index=1,
            half=HalfwordSelect.HIGH_16,
            abs_mod=True,
        )
        instr = ASTRsqF16(dst=dst, src0=src, clamp=True)
        self.assertEqual(instr.to_assembly(), "v_rsq_f16 v0.l, |v1.h| clamp")

    def test_ast_validation_rejects_invalid_dst(self) -> None:
        dst_sgpr = ASTOperand(op_type=OperandType.SGPR, reg_index=0)
        src = ASTOperand(op_type=OperandType.VGPR, reg_index=1)
        instr = ASTRsqF16(dst=dst_sgpr, src0=src)
        with self.assertRaises(TypeError):
            instr.validate()

    def test_ast_validation_rejects_out_of_range_vgpr(self) -> None:
        dst_oob = ASTOperand(op_type=OperandType.VGPR, reg_index=300)
        src = ASTOperand(op_type=OperandType.VGPR, reg_index=1)
        instr = ASTRsqF16(dst=dst_oob, src0=src)
        with self.assertRaises(ValueError):
            instr.validate()

    def test_symbolic_evaluation_with_env(self) -> None:
        dst = ASTOperand(op_type=OperandType.VGPR, reg_index=5, half=HalfwordSelect.LOW_16)
        src = ASTOperand(op_type=OperandType.VGPR, reg_index=6, half=HalfwordSelect.HIGH_16)
        instr = ASTRsqF16(dst=dst, src0=src, clamp=False)

        env = {"v6.h": H_FOUR}
        res = instr.evaluate_symbolic(env)
        self.assertEqual(res, H_HALF)

    def test_symbolic_expression_simplification(self) -> None:
        # abs(abs(x)) -> abs(x)
        e1 = SymbolicExpr("abs", (SymbolicExpr("abs", ("x",)),))
        self.assertEqual(e1.simplify(), SymbolicExpr("abs", ("x",)))

        # abs(neg(x)) -> abs(x)
        e2 = SymbolicExpr("abs", (SymbolicExpr("neg", ("y",)),))
        self.assertEqual(e2.simplify(), SymbolicExpr("abs", ("y",)))

        # neg(neg(z)) -> z
        e3 = SymbolicExpr("neg", (SymbolicExpr("neg", ("z",)),))
        self.assertEqual(e3.simplify(), "z")


class TestScalarInterpreterRSQF16(unittest.TestCase):
    """Unit tests verifying deterministic scalar execution of v_rsq_f16."""

    def test_scalar_execution_vgpr_low_and_high(self) -> None:
        interp = ScalarInterpreter()
        # Set v1 = [low=4.0 (0x4400), high=16.0 (0x4C00)]
        interp.rf.write_vgpr(1, (H_SIXTEEN << 16) | H_FOUR)

        # v2.l = v_rsq_f16 v1.l (rsq(4.0) = 0.5 -> 0x3800)
        dst_l = ASTOperand(op_type=OperandType.VGPR, reg_index=2, half=HalfwordSelect.LOW_16)
        src_l = ASTOperand(op_type=OperandType.VGPR, reg_index=1, half=HalfwordSelect.LOW_16)
        interp.step(ASTRsqF16(dst=dst_l, src0=src_l))
        self.assertEqual(interp.rf.read_vgpr_half(2, HalfwordSelect.LOW_16), H_HALF)
        self.assertEqual(interp.rf.read_vgpr_half(2, HalfwordSelect.HIGH_16), 0)

        # v2.h = v_rsq_f16 v1.h (rsq(16.0) = 0.25 -> 0x3400)
        dst_h = ASTOperand(op_type=OperandType.VGPR, reg_index=2, half=HalfwordSelect.HIGH_16)
        src_h = ASTOperand(op_type=OperandType.VGPR, reg_index=1, half=HalfwordSelect.HIGH_16)
        interp.step(ASTRsqF16(dst=dst_h, src0=src_h))
        self.assertEqual(interp.rf.read_vgpr_half(2, HalfwordSelect.HIGH_16), H_QUARTER)
        # Verify low half remained intact!
        self.assertEqual(interp.rf.read_vgpr_half(2, HalfwordSelect.LOW_16), H_HALF)

    def test_scalar_execution_sgpr_source(self) -> None:
        interp = ScalarInterpreter()
        interp.rf.write_sgpr_half(4, HalfwordSelect.LOW_16, H_QUARTER)  # 0.25

        # v0.l = v_rsq_f16 s4.l (rsq(0.25) = 2.0)
        dst = ASTOperand(op_type=OperandType.VGPR, reg_index=0, half=HalfwordSelect.LOW_16)
        src = ASTOperand(op_type=OperandType.SGPR, reg_index=4, half=HalfwordSelect.LOW_16)
        interp.step(ASTRsqF16(dst=dst, src0=src))
        self.assertEqual(interp.rf.read_vgpr_half(0, HalfwordSelect.LOW_16), H_TWO)

    def test_scalar_execution_literal_constant(self) -> None:
        interp = ScalarInterpreter()
        dst = ASTOperand(op_type=OperandType.VGPR, reg_index=10, half=HalfwordSelect.LOW_16)
        src = ASTOperand(op_type=OperandType.LITERAL_CONST, literal_val=H_0P0625)  # 0.0625 -> 4.0
        interp.step(ASTRsqF16(dst=dst, src0=src))
        self.assertEqual(interp.rf.read_vgpr_half(10, HalfwordSelect.LOW_16), H_FOUR)

    def test_cycle_count_and_trace_logging(self) -> None:
        interp = ScalarInterpreter()
        dst = ASTOperand(op_type=OperandType.VGPR, reg_index=0, half=HalfwordSelect.LOW_16)
        src = ASTOperand(op_type=OperandType.LITERAL_CONST, literal_val=H_ONE)
        interp.step(ASTRsqF16(dst=dst, src0=src))
        self.assertEqual(interp.cycle_count, 4)
        self.assertEqual(len(interp.trace_log), 1)
        self.assertIn("v_rsq_f16", interp.trace_log[0])


class TestSIMDMultiLaneDivergenceRSQF16(unittest.TestCase):
    """Unit tests verifying SIMD multi-lane execution and 32/64-bit EXEC masking."""

    def test_wave32_alternating_exec_mask(self) -> None:
        exec_mask = 0x55555555  # Even lanes active (0, 2, 4, ...), odd lanes inactive
        wave = Wavefront(wave_size=32, exec_mask=exec_mask)

        # Initialize v0 in all lanes to sentinel value 0xDEADBEEF
        for lane_id in range(32):
            wave.lanes[lane_id].write_vgpr(0, 0xDEADBEEF)
            # Set source v1.l = 4.0 (0x4400)
            wave.lanes[lane_id].write_vgpr_half(1, HalfwordSelect.LOW_16, H_FOUR)

        dst = ASTOperand(op_type=OperandType.VGPR, reg_index=0, half=HalfwordSelect.LOW_16)
        src = ASTOperand(op_type=OperandType.VGPR, reg_index=1, half=HalfwordSelect.LOW_16)
        results = wave.execute_simd(ASTRsqF16(dst=dst, src0=src))

        for lane_id in range(32):
            if lane_id % 2 == 0:
                self.assertEqual(results[lane_id], H_HALF)
                self.assertEqual(wave.lanes[lane_id].read_vgpr_half(0, HalfwordSelect.LOW_16), H_HALF)
                # Upper half should remain 0xDEAD
                self.assertEqual(wave.lanes[lane_id].read_vgpr_half(0, HalfwordSelect.HIGH_16), 0xDEAD)
            else:
                self.assertIsNone(results[lane_id])
                # Inactive lane must remain completely untouched!
                self.assertEqual(wave.lanes[lane_id].read_vgpr(0), 0xDEADBEEF)

    def test_wave64_boundary_masks(self) -> None:
        # High 32 lanes active, low 32 lanes inactive
        exec_mask = 0xFFFFFFFF00000000
        wave = Wavefront(wave_size=64, exec_mask=exec_mask)

        for lane_id in range(64):
            wave.lanes[lane_id].write_vgpr(3, 0xCAFEBABE)
            wave.lanes[lane_id].write_vgpr_half(4, HalfwordSelect.LOW_16, H_SIXTEEN)

        dst = ASTOperand(op_type=OperandType.VGPR, reg_index=3, half=HalfwordSelect.LOW_16)
        src = ASTOperand(op_type=OperandType.VGPR, reg_index=4, half=HalfwordSelect.LOW_16)
        results = wave.execute_simd(ASTRsqF16(dst=dst, src0=src))

        for lane_id in range(64):
            if lane_id < 32:
                self.assertIsNone(results[lane_id])
                self.assertEqual(wave.lanes[lane_id].read_vgpr(3), 0xCAFEBABE)
            else:
                self.assertEqual(results[lane_id], H_QUARTER)
                self.assertEqual(wave.lanes[lane_id].read_vgpr_half(3, HalfwordSelect.LOW_16), H_QUARTER)
                self.assertEqual(wave.lanes[lane_id].read_vgpr_half(3, HalfwordSelect.HIGH_16), 0xCAFE)

    def test_divergent_operands_per_lane(self) -> None:
        # All 32 lanes active with divergent input values
        wave = Wavefront(wave_size=32, exec_mask=0xFFFFFFFF)
        inputs = [H_ONE, H_FOUR, H_SIXTEEN, H_QUARTER] * 8
        expected = [H_ONE, H_HALF, H_QUARTER, H_TWO] * 8

        for lane_id in range(32):
            wave.lanes[lane_id].write_vgpr_half(5, HalfwordSelect.LOW_16, inputs[lane_id])

        dst = ASTOperand(op_type=OperandType.VGPR, reg_index=6, half=HalfwordSelect.LOW_16)
        src = ASTOperand(op_type=OperandType.VGPR, reg_index=5, half=HalfwordSelect.LOW_16)
        results = wave.execute_simd(ASTRsqF16(dst=dst, src0=src))

        for lane_id in range(32):
            self.assertEqual(results[lane_id], expected[lane_id])
            self.assertEqual(wave.lanes[lane_id].read_vgpr_half(6, HalfwordSelect.LOW_16), expected[lane_id])

    def test_uniform_scalar_source_broadcast(self) -> None:
        wave = Wavefront(wave_size=32, exec_mask=0x0000FFFF)  # First 16 lanes active
        wave.write_shared_sgpr_half(2, HalfwordSelect.LOW_16, H_FOUR)  # 4.0

        dst = ASTOperand(op_type=OperandType.VGPR, reg_index=0, half=HalfwordSelect.LOW_16)
        src = ASTOperand(op_type=OperandType.SGPR, reg_index=2, half=HalfwordSelect.LOW_16)
        results = wave.execute_simd(ASTRsqF16(dst=dst, src0=src))

        for lane_id in range(32):
            if lane_id < 16:
                self.assertEqual(results[lane_id], H_HALF)
                self.assertEqual(wave.lanes[lane_id].read_vgpr_half(0, HalfwordSelect.LOW_16), H_HALF)
            else:
                self.assertIsNone(results[lane_id])


class TestRegisterAliasingRSQF16(unittest.TestCase):
    """Unit tests verifying register aliasing and in-place operand preservation."""

    def test_in_place_same_halfword_overwrite(self) -> None:
        interp = ScalarInterpreter()
        # Set v0 = [low=16.0 (0x4C00), high=0xBEEF]
        interp.rf.write_vgpr(0, (0xBEEF << 16) | H_SIXTEEN)

        # In-place overwrite: v0.l = v_rsq_f16 v0.l
        dst = ASTOperand(op_type=OperandType.VGPR, reg_index=0, half=HalfwordSelect.LOW_16)
        src = ASTOperand(op_type=OperandType.VGPR, reg_index=0, half=HalfwordSelect.LOW_16)
        res = interp.step(ASTRsqF16(dst=dst, src0=src))

        self.assertEqual(res, H_QUARTER)
        self.assertEqual(interp.rf.read_vgpr_half(0, HalfwordSelect.LOW_16), H_QUARTER)
        # High halfword untouched
        self.assertEqual(interp.rf.read_vgpr_half(0, HalfwordSelect.HIGH_16), 0xBEEF)

    def test_cross_halfword_same_vgpr_aliasing(self) -> None:
        interp = ScalarInterpreter()
        # Initial v7 = [low=4.0 (0x4400), high=0x1111]
        interp.rf.write_vgpr(7, (0x1111 << 16) | H_FOUR)

        # v7.h = v_rsq_f16 v7.l
        dst_h = ASTOperand(op_type=OperandType.VGPR, reg_index=7, half=HalfwordSelect.HIGH_16)
        src_l = ASTOperand(op_type=OperandType.VGPR, reg_index=7, half=HalfwordSelect.LOW_16)
        interp.step(ASTRsqF16(dst=dst_h, src0=src_l))

        # Result in v7.h should be 0.5 (0x3800), and v7.l remains 4.0 (0x4400)
        self.assertEqual(interp.rf.read_vgpr_half(7, HalfwordSelect.HIGH_16), H_HALF)
        self.assertEqual(interp.rf.read_vgpr_half(7, HalfwordSelect.LOW_16), H_FOUR)

        # Next: v7.l = v_rsq_f16 v7.h (rsq(0.5) = 1/sqrt(0.5) = sqrt(2) ~ 1.4142)
        dst_l = ASTOperand(op_type=OperandType.VGPR, reg_index=7, half=HalfwordSelect.LOW_16)
        src_h = ASTOperand(op_type=OperandType.VGPR, reg_index=7, half=HalfwordSelect.HIGH_16)
        res_l = interp.step(ASTRsqF16(dst=dst_l, src0=src_h))
        val_l = bits_to_f16(res_l)
        self.assertTrue(math.isclose(val_l, math.sqrt(2.0), rel_tol=1e-3))

    def test_chain_in_place_recurrence_pipeline(self) -> None:
        interp = ScalarInterpreter()
        # Initialize v1.l = 16.0
        interp.rf.write_vgpr_half(1, HalfwordSelect.LOW_16, H_SIXTEEN)

        # Step 1: v1.l = rsq(16.0) = 0.25
        dst = ASTOperand(op_type=OperandType.VGPR, reg_index=1, half=HalfwordSelect.LOW_16)
        src = ASTOperand(op_type=OperandType.VGPR, reg_index=1, half=HalfwordSelect.LOW_16)
        step1 = interp.step(ASTRsqF16(dst=dst, src0=src))
        self.assertEqual(step1, H_QUARTER)

        # Step 2: v1.l = rsq(0.25) = 2.0
        step2 = interp.step(ASTRsqF16(dst=dst, src0=src))
        self.assertEqual(step2, H_TWO)

        # Step 3: v1.l = rsq(2.0) = 1/sqrt(2) ~ 0.7071
        step3 = interp.step(ASTRsqF16(dst=dst, src0=src))
        val3 = bits_to_f16(step3)
        self.assertTrue(math.isclose(val3, 1.0 / math.sqrt(2.0), rel_tol=1e-3))


class TestConditionCodesAndStatusFlagsRSQF16(unittest.TestCase):
    """Unit tests verifying condition codes, IEEE-754 status flags, and classification."""

    def test_division_by_zero_flag(self) -> None:
        flags_pos_zero = evaluate_status_flags_rsq_f16(H_POS_ZERO)
        self.assertTrue(flags_pos_zero.div_by_zero)
        self.assertFalse(flags_pos_zero.invalid_op)

        flags_neg_zero = evaluate_status_flags_rsq_f16(H_NEG_ZERO)
        self.assertTrue(flags_neg_zero.div_by_zero)
        self.assertFalse(flags_neg_zero.invalid_op)

    def test_invalid_operation_flag_on_negatives_and_snan(self) -> None:
        # Negative finite values raise invalid operation
        flags_neg = evaluate_status_flags_rsq_f16(f16_to_bits(-4.0))
        self.assertTrue(flags_neg.invalid_op)
        self.assertFalse(flags_neg.div_by_zero)

        # -inf raises invalid op
        flags_neg_inf = evaluate_status_flags_rsq_f16(H_NEG_INF)
        self.assertTrue(flags_neg_inf.invalid_op)

        # Signaling NaN raises invalid op
        flags_snan = evaluate_status_flags_rsq_f16(H_SNAN)
        self.assertTrue(flags_snan.invalid_op)

        # Quiet NaN does not raise invalid op
        flags_qnan = evaluate_status_flags_rsq_f16(H_QNAN)
        self.assertFalse(flags_qnan.invalid_op)

    def test_inexact_flag_precision(self) -> None:
        # Exact powers have inexact == False
        flags_1 = evaluate_status_flags_rsq_f16(H_ONE)
        self.assertFalse(flags_1.inexact)
        flags_4 = evaluate_status_flags_rsq_f16(H_FOUR)
        self.assertFalse(flags_4.inexact)
        flags_16 = evaluate_status_flags_rsq_f16(H_SIXTEEN)
        self.assertFalse(flags_16.inexact)

        # Arbitrary floats (e.g. 2.0, 3.0, 5.0) have inexact == True
        flags_2 = evaluate_status_flags_rsq_f16(H_TWO)
        self.assertTrue(flags_2.inexact)
        flags_3 = evaluate_status_flags_rsq_f16(f16_to_bits(3.0))
        self.assertTrue(flags_3.inexact)

    def test_rdna2_classification_mask(self) -> None:
        # +0.0 -> bit 6 (0x040)
        self.assertEqual(classify_f16(H_POS_ZERO), 1 << 6)
        # -0.0 -> bit 5 (0x020)
        self.assertEqual(classify_f16(H_NEG_ZERO), 1 << 5)
        # +inf -> bit 9 (0x200)
        self.assertEqual(classify_f16(H_POS_INF), 1 << 9)
        # -inf -> bit 2 (0x004)
        self.assertEqual(classify_f16(H_NEG_INF), 1 << 2)
        # qNaN -> bit 1 (0x002)
        self.assertEqual(classify_f16(H_QNAN), 1 << 1)
        # sNaN -> bit 0 (0x001)
        self.assertEqual(classify_f16(H_SNAN), 1 << 0)
        # +normal 4.0 -> bit 8 (0x100)
        self.assertEqual(classify_f16(H_FOUR), 1 << 8)
        # -normal -4.0 -> bit 3 (0x008)
        self.assertEqual(classify_f16(f16_to_bits(-4.0)), 1 << 3)
        # +subnormal -> bit 7 (0x080)
        self.assertEqual(classify_f16(H_MIN_SUBNORMAL), 1 << 7)


class TestIEEE754BoundaryExtremesRSQF16(unittest.TestCase):
    """Unit tests verifying IEEE-754 boundary extremes, subnormals, and NaN handling."""

    def test_subnormal_progression_gradual_underflow(self) -> None:
        # Subnormals with even negative powers of 2 yield exact powers of 2
        powers = [
            (0x0001, H_4096, "2^-24 -> 4096.0"),
            (0x0004, 0x6800, "2^-22 -> 2048.0"),
            (0x0010, 0x6400, "2^-20 -> 1024.0"),
            (0x0040, 0x6000, "2^-18 -> 512.0"),
            (0x0100, 0x5C00, "2^-16 -> 256.0"),
        ]
        for sub_bits, expected_bits, desc in powers:
            with self.subTest(msg=desc):
                res = oracle_v_rsq_f16(sub_bits, ftz=False)
                self.assertEqual(res, expected_bits)

    def test_subnormal_sweep_ftz_mode(self) -> None:
        # In FTZ mode, all subnormal inputs flush to 0.0 -> result is +inf
        subnormals = [0x0001, 0x0002, 0x0004, 0x0010, 0x007F, 0x0100, 0x03FF]
        for sub in subnormals:
            with self.subTest(val=hex(sub)):
                self.assertEqual(oracle_v_rsq_f16(sub, ftz=True), H_POS_INF)

    def test_normal_boundaries_min_and_max(self) -> None:
        # Smallest positive normal: 2^-14 -> rsq = 2^7 = 128.0 (0x5800)
        res_min = oracle_v_rsq_f16(H_MIN_NORMAL)
        self.assertEqual(res_min, 0x5800)

        # Largest finite normal: 65504.0 -> rsq = 1/sqrt(65504) ~ 0.00390625 (0x1C00)
        res_max = oracle_v_rsq_f16(H_MAX_NORMAL)
        val_max = bits_to_f16(res_max)
        self.assertTrue(math.isclose(val_max, 1.0 / math.sqrt(65504.0), rel_tol=1e-3))

    def test_nan_payload_and_quietening(self) -> None:
        # Signaling NaN quietened by setting quiet bit (bit 9 in binary16)
        snan_payload = 0x7D55  # exp=0x1F, frac=0x155 (quiet bit 9 is 0)
        self.assertTrue(is_snan_f16(snan_payload))
        res = oracle_v_rsq_f16(snan_payload)
        self.assertTrue(is_qnan_f16(res))
        self.assertEqual(res & QNAN_BIT_16, QNAN_BIT_16)

        # Quiet NaN payload preserved
        qnan_payload = 0x7EAA
        self.assertEqual(oracle_v_rsq_f16(qnan_payload), qnan_payload)

    def test_strict_monotonicity_on_normals(self) -> None:
        values = [0.0625, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0]
        results = [bits_to_f16(oracle_v_rsq_f16(f16_to_bits(v))) for v in values]
        for i in range(len(results) - 1):
            self.assertGreaterEqual(
                results[i],
                results[i + 1],
                f"Monotonicity violated: rsq({values[i]})={results[i]} < rsq({values[i+1]})={results[i+1]}",
            )


class TestNegativeControlsAndMutationsRSQF16(unittest.TestCase):
    """Unit tests verifying negative controls, syntax rejection, and mutation detection."""

    def test_syntax_rejection_invalid_operand_types(self) -> None:
        # Destination cannot be an SGPR or literal
        op_sgpr = ASTOperand(op_type=OperandType.SGPR, reg_index=0)
        op_vgpr = ASTOperand(op_type=OperandType.VGPR, reg_index=1)
        instr_bad_dst = ASTRsqF16(dst=op_sgpr, src0=op_vgpr)
        with self.assertRaises(TypeError):
            instr_bad_dst.validate()

        # Invalid literal constant > 0xFFFF
        op_bad_lit = ASTOperand(op_type=OperandType.LITERAL_CONST, literal_val=0x10000)
        with self.assertRaises(ValueError):
            op_bad_lit.validate()

    def test_syntax_rejection_out_of_bounds_registers(self) -> None:
        # VGPR > 255
        op_oob_vgpr = ASTOperand(op_type=OperandType.VGPR, reg_index=256)
        with self.assertRaises(ValueError):
            op_oob_vgpr.validate()

        # SGPR > 105
        op_oob_sgpr = ASTOperand(op_type=OperandType.SGPR, reg_index=106)
        with self.assertRaises(ValueError):
            op_oob_sgpr.validate()

    def test_syntax_rejection_invalid_output_modifiers(self) -> None:
        dst = ASTOperand(op_type=OperandType.VGPR, reg_index=0)
        src = ASTOperand(op_type=OperandType.VGPR, reg_index=1)
        instr_bad_omod = ASTRsqF16(dst=dst, src0=src, omod="mul:8")
        with self.assertRaises(ValueError):
            instr_bad_omod.validate()

    def test_syntax_rejection_invalid_wave_size(self) -> None:
        with self.assertRaises(ValueError):
            Wavefront(wave_size=16)
        with self.assertRaises(ValueError):
            Wavefront(wave_size=128)

    def test_mutation_detection_omitted_reciprocal(self) -> None:
        # Mutant: sqrt(x) instead of 1/sqrt(x)
        def mutant_sqrt(src: int) -> int:
            return f16_to_bits(math.sqrt(bits_to_f16(src)))

        # For x=4.0, real rsq=0.5 (0x3800), mutant produces 2.0 (0x4000)
        self.assertNotEqual(mutant_sqrt(H_FOUR), oracle_v_rsq_f16(H_FOUR))

    def test_mutation_detection_negative_abs_fallback(self) -> None:
        # Mutant: silently takes abs of negative numbers instead of generating NaN
        def mutant_abs_fallback(src: int) -> int:
            return oracle_v_rsq_f16(src, abs_mod=True)

        neg_val = f16_to_bits(-4.0)
        # Correct oracle produces Canonical Quiet NaN
        self.assertTrue(is_nan_f16(oracle_v_rsq_f16(neg_val)))
        # Mutant produces 0.5 (not NaN)
        self.assertFalse(is_nan_f16(mutant_abs_fallback(neg_val)))

    def test_mutation_detection_missing_clamp(self) -> None:
        # Mutant: ignores clamp modifier
        def mutant_no_clamp(src: int, clamp: bool) -> int:
            return oracle_v_rsq_f16(src, clamp=False)

        # 0.25 -> 2.0, with clamp=True should be 1.0
        self.assertEqual(oracle_v_rsq_f16(H_QUARTER, clamp=True), H_ONE)
        self.assertNotEqual(mutant_no_clamp(H_QUARTER, clamp=True), H_ONE)

    def test_golden_vector_parity_table(self) -> None:
        golden_vectors = [
            (0x0000, 0x7C00),  # +0.0 -> +inf
            (0x8000, 0x7C00),  # -0.0 -> +inf
            (0x7C00, 0x0000),  # +inf -> +0.0
            (0xFC00, 0x7E00),  # -inf -> Canonical Quiet NaN
            (0x3C00, 0x3C00),  # 1.0  -> 1.0
            (0x4400, 0x3800),  # 4.0  -> 0.5
            (0x4C00, 0x3400),  # 16.0 -> 0.25
            (0x5400, 0x3000),  # 64.0 -> 0.125
            (0x3400, 0x4000),  # 0.25 -> 2.0
            (0x2C00, 0x4400),  # 0.0625 -> 4.0
            (0x2400, 0x4800),  # 0.015625 -> 8.0
        ]
        for src, expected in golden_vectors:
            with self.subTest(src=hex(src)):
                self.assertEqual(oracle_v_rsq_f16(src), expected)


if __name__ == "__main__":
    unittest.main()
