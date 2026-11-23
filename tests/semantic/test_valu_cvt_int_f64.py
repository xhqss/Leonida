from __future__ import annotations

import math
import struct
import unittest
from typing import Tuple

# ==============================================================================
# Architectural Constants for IEEE 754 Binary64 and 32-bit Integer Conversions
# ==============================================================================

SIGN_MASK_64: int = 0x8000_0000_0000_0000
EXP_MASK_64: int = 0x7FF0_0000_0000_0000
FRAC_MASK_64: int = 0x000F_FFFF_FFFF_FFFF
QNAN_BIT_64: int = 0x0008_0000_0000_0000
CANONICAL_QNAN_64: int = 0x7FF8_0000_0000_0000

POS_ZERO_64: int = 0x0000_0000_0000_0000
NEG_ZERO_64: int = 0x8000_0000_0000_0000
POS_INF_64: int = 0x7FF0_0000_0000_0000
NEG_INF_64: int = 0xFFF0_0000_0000_0000
POS_ONE_64: int = 0x3FF0_0000_0000_0000

MASK32: int = 0xFFFF_FFFF
MASK64: int = 0xFFFF_FFFF_FFFF_FFFF

INT32_MIN: int = -0x8000_0000
INT32_MAX: int = 0x7FFF_FFFF
UINT32_MAX: int = 0xFFFF_FFFF


def f64_to_bits(val: float) -> int:
    """Convert a Python float (IEEE 754 binary64) to an unsigned 64-bit integer."""
    return struct.unpack("<Q", struct.pack("<d", val))[0]


def bits_to_f64(bits: int) -> float:
    """Convert an unsigned 64-bit integer to a Python float (IEEE 754 binary64)."""
    return struct.unpack("<d", struct.pack("<Q", bits & MASK64))[0]


def split_f64_bits(bits: int) -> Tuple[int, int, int]:
    """Unpack bits into (sign, exponent, fraction) components."""
    sign = (bits & SIGN_MASK_64) >> 63
    exponent = (bits & EXP_MASK_64) >> 52
    fraction = bits & FRAC_MASK_64
    return sign, exponent, fraction


def words_to_f64_bits(lo: int, hi: int) -> int:
    """Combine two 32-bit VGPR words (lo, hi) into a 64-bit integer representation."""
    return ((hi & MASK32) << 32) | (lo & MASK32)


def f64_bits_to_words(bits: int) -> Tuple[int, int]:
    """Split a 64-bit integer into two 32-bit VGPR words (lo, hi)."""
    lo = bits & MASK32
    hi = (bits >> 32) & MASK32
    return lo, hi


def i32_to_u32(val: int) -> int:
    """Convert a signed 32-bit integer to an unsigned 32-bit word representation."""
    return val & MASK32


def u32_to_i32(val: int) -> int:
    """Interpret an unsigned 32-bit word as a signed two's complement 32-bit integer."""
    val = val & MASK32
    return val if val < 0x8000_0000 else val - 0x1_0000_0000


def is_nan_f64(bits: int) -> bool:
    """Return True if bits represent an IEEE 754 double NaN."""
    return ((bits & EXP_MASK_64) == EXP_MASK_64) and ((bits & FRAC_MASK_64) != 0)


def is_qnan_f64(bits: int) -> bool:
    """Return True if bits represent a Quiet NaN."""
    return is_nan_f64(bits) and bool(bits & QNAN_BIT_64)


def is_snan_f64(bits: int) -> bool:
    """Return True if bits represent a Signaling NaN."""
    return is_nan_f64(bits) and not bool(bits & QNAN_BIT_64)


def is_inf_f64(bits: int) -> bool:
    """Return True if bits represent positive or negative infinity."""
    return ((bits & EXP_MASK_64) == EXP_MASK_64) and ((bits & FRAC_MASK_64) == 0)


def is_subnormal_f64(bits: int) -> bool:
    """Return True if bits represent a subnormal double-precision float."""
    return ((bits & EXP_MASK_64) == 0) and ((bits & FRAC_MASK_64) != 0)


def is_zero_f64(bits: int) -> bool:
    """Return True if bits represent positive or negative zero."""
    return (bits & ~SIGN_MASK_64) == 0


def apply_f64_modifiers(bits: int, abs_mod: bool = False, neg_mod: bool = False) -> int:
    """Apply VOP3 input modifiers: abs clears sign bit; neg toggles sign bit."""
    res = bits & MASK64
    if abs_mod:
        res &= ~SIGN_MASK_64
    if neg_mod:
        res ^= SIGN_MASK_64
    return res


def apply_f64_clamp(bits: int) -> int:
    """Apply VOP3 destination clamp [0.0, 1.0] modifier on f64 results."""
    if is_nan_f64(bits):
        return POS_ZERO_64
    if bits == NEG_ZERO_64:
        return POS_ZERO_64
    val = bits_to_f64(bits)
    if val < 0.0:
        return POS_ZERO_64
    if val > 1.0 or bits == POS_INF_64:
        return POS_ONE_64
    return bits


# ==============================================================================
# Reference Mathematical Oracles for Integer <-> Float64 Conversions
# ==============================================================================

def oracle_v_cvt_f64_i32(src0_i32: int, clamp: bool = False) -> int:
    """Reference mathematical oracle for RDNA2 v_cvt_f64_i32.
    
    Converts a 32-bit signed integer to a 64-bit IEEE 754 float.
    Because binary64 has 53 bits of mantissa, all 32-bit integers are converted exactly.
    If clamp is True, the result is clamped to [0.0, 1.0].
    """
    s_val = u32_to_i32(src0_i32)
    f_val = float(s_val)
    res_bits = f64_to_bits(f_val)
    if clamp:
        res_bits = apply_f64_clamp(res_bits)
    return res_bits


def oracle_v_cvt_f64_u32(src0_u32: int, clamp: bool = False) -> int:
    """Reference mathematical oracle for RDNA2 v_cvt_f64_u32.
    
    Converts a 32-bit unsigned integer to a 64-bit IEEE 754 float.
    All 32-bit unsigned integers are converted exactly without precision loss.
    If clamp is True, the result is clamped to [0.0, 1.0].
    """
    u_val = src0_u32 & MASK32
    f_val = float(u_val)
    res_bits = f64_to_bits(f_val)
    if clamp:
        res_bits = apply_f64_clamp(res_bits)
    return res_bits


def oracle_v_cvt_i32_f64(src0_bits: int, abs_mod: bool = False, neg_mod: bool = False) -> int:
    """Reference mathematical oracle for RDNA2 v_cvt_i32_f64.
    
    Converts an IEEE 754 double precision float to a 32-bit signed integer (i32).
    Applies VOP3 input modifiers (abs, neg).
    Rounding mode: Round-toward-zero (truncate).
    NaN inputs produce 0.
    Values exceeding INT32_MAX or +inf saturate to INT32_MAX (0x7FFFFFFF).
    Values below INT32_MIN or -inf saturate to INT32_MIN (0x80000000).
    """
    s0 = apply_f64_modifiers(src0_bits, abs_mod, neg_mod)
    if is_nan_f64(s0):
        return 0
    if is_zero_f64(s0):
        return 0
    if s0 == POS_INF_64:
        return INT32_MAX
    if s0 == NEG_INF_64:
        return i32_to_u32(INT32_MIN)

    val = bits_to_f64(s0)
    trunc_val = int(math.trunc(val))
    if trunc_val >= INT32_MAX:
        return INT32_MAX
    if trunc_val <= INT32_MIN:
        return i32_to_u32(INT32_MIN)
    return i32_to_u32(trunc_val)


def oracle_v_cvt_u32_f64(src0_bits: int, abs_mod: bool = False, neg_mod: bool = False) -> int:
    """Reference mathematical oracle for RDNA2 v_cvt_u32_f64.
    
    Converts an IEEE 754 double precision float to a 32-bit unsigned integer (u32).
    Applies VOP3 input modifiers (abs, neg).
    Rounding mode: Round-toward-zero (truncate).
    NaN inputs produce 0.
    Negative values (including -inf, -0.0, and any finite negative float) clamp to 0.
    Values exceeding UINT32_MAX or +inf saturate to UINT32_MAX (0xFFFFFFFF).
    """
    s0 = apply_f64_modifiers(src0_bits, abs_mod, neg_mod)
    if is_nan_f64(s0):
        return 0
    if is_zero_f64(s0):
        return 0
    if (s0 & SIGN_MASK_64) != 0:
        return 0
    if s0 == POS_INF_64:
        return UINT32_MAX

    val = bits_to_f64(s0)
    trunc_val = int(math.trunc(val))
    if trunc_val >= UINT32_MAX:
        return UINT32_MAX
    if trunc_val <= 0:
        return 0
    return trunc_val & MASK32



# ==============================================================================
# Symbolic AST Opcode Definitions and Typed Expressions
# ==============================================================================

from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional, Union


class Opcode(Enum):
    V_CVT_F64_I32 = auto()
    V_CVT_F64_U32 = auto()
    V_CVT_I32_F64 = auto()
    V_CVT_U32_F64 = auto()


class RegKind(Enum):
    VGPR = auto()
    SGPR = auto()


@dataclass(frozen=True)
class Register:
    """Architectural register or register pair."""
    kind: RegKind
    index: int
    count: int = 1

    def __post_init__(self) -> None:
        if not (0 <= self.index <= 255):
            raise ValueError(f"Register index {self.index} out of valid range [0, 255]")
        if self.count not in (1, 2):
            raise ValueError(f"Register count must be 1 (32-bit) or 2 (64-bit), got {self.count}")
        if self.index + self.count > 256:
            raise ValueError(f"Register {self.kind.name} {self.index} with count {self.count} exceeds index 255")

    @property
    def name(self) -> str:
        prefix = "v" if self.kind == RegKind.VGPR else "s"
        if self.count == 1:
            return f"{prefix}{self.index}"
        return f"{prefix}[{self.index}:{self.index + 1}]"


@dataclass(frozen=True)
class Literal32:
    """32-bit integer literal or inline constant."""
    bits: int

    @property
    def as_uint(self) -> int:
        return self.bits & MASK32

    @property
    def as_int(self) -> int:
        return u32_to_i32(self.bits)

    @property
    def name(self) -> str:
        return str(self.as_int)


@dataclass(frozen=True)
class Literal64:
    """64-bit double-precision float literal or inline constant."""
    bits: int

    @classmethod
    def from_float(cls, val: float) -> Literal64:
        return cls(f64_to_bits(val))

    @property
    def float_value(self) -> float:
        return bits_to_f64(self.bits)

    @property
    def name(self) -> str:
        if self.bits == POS_ZERO_64:
            return "0.0"
        if self.bits == NEG_ZERO_64:
            return "-0.0"
        f = self.float_value
        if math.isnan(f):
            return "nan"
        if math.isinf(f):
            return "inf" if f > 0 else "-inf"
        return f"{f:g}"


@dataclass(frozen=True)
class VOP1Operand:
    """Typed source operand with optional VOP3 modifiers (abs, neg)."""
    source: Union[Register, Literal32, Literal64]
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
class VCvtInstruction:
    """Symbolic AST node for RDNA2 integer <-> float64 conversion instructions."""
    opcode: Opcode
    dst: Register
    src0: VOP1Operand
    clamp: bool = False

    def __post_init__(self) -> None:
        if self.dst.kind != RegKind.VGPR:
            raise TypeError(f"VALU instruction destination must be a VGPR, got {self.dst.kind}")

        if self.opcode in (Opcode.V_CVT_F64_I32, Opcode.V_CVT_F64_U32):
            if self.dst.count != 2:
                raise ValueError(f"{self.opcode.name} destination must be a 64-bit VGPR pair, got count={self.dst.count}")
            if isinstance(self.src0.source, Register) and self.src0.source.count != 1:
                raise ValueError(f"{self.opcode.name} source register must be 32-bit (count=1), got count={self.src0.source.count}")
            if isinstance(self.src0.source, Literal64):
                raise TypeError(f"{self.opcode.name} source cannot be Literal64")
            if self.src0.abs_mod or self.src0.neg_mod:
                raise ValueError(f"{self.opcode.name} does not support float input modifiers on integer source")

        elif self.opcode in (Opcode.V_CVT_I32_F64, Opcode.V_CVT_U32_F64):
            if self.dst.count != 1:
                raise ValueError(f"{self.opcode.name} destination must be a 32-bit VGPR (count=1), got count={self.dst.count}")
            if isinstance(self.src0.source, Register) and self.src0.source.count != 2:
                raise ValueError(f"{self.opcode.name} source register must be a 64-bit pair (count=2), got count={self.src0.source.count}")
            if isinstance(self.src0.source, Literal32):
                raise TypeError(f"{self.opcode.name} source cannot be Literal32")
            if self.clamp:
                raise ValueError(f"{self.opcode.name} cannot clamp integer destination")

    def disassemble(self) -> str:
        op_name = self.opcode.name.lower()
        s = f"{op_name} {self.dst.name}, {self.src0.disassemble()}"
        if self.clamp:
            s += " clamp"
        return s



# ==============================================================================
# Deterministic Scalar Instruction Interpreter
# ==============================================================================

from dataclasses import field
from typing import List


@dataclass
class ScalarMachineState:
    """Architectural register file for a single wavefront execution thread."""
    vgprs: List[int] = field(default_factory=lambda: [0] * 256)
    sgprs: List[int] = field(default_factory=lambda: [0] * 256)

    def read_reg(self, kind: RegKind, index: int, count: int = 1) -> int:
        if index < 0 or index + count > 256:
            raise IndexError(f"Register index {index} + count {count} out of bounds [0, 256)")
        regs = self.vgprs if kind == RegKind.VGPR else self.sgprs
        if count == 1:
            return regs[index] & MASK32
        elif count == 2:
            return words_to_f64_bits(regs[index], regs[index + 1])
        raise ValueError(f"Unsupported register count: {count}")

    def write_reg(self, kind: RegKind, index: int, count: int, value: int) -> None:
        if index < 0 or index + count > 256:
            raise IndexError(f"Register index {index} + count {count} out of bounds [0, 256)")
        regs = self.vgprs if kind == RegKind.VGPR else self.sgprs
        if count == 1:
            regs[index] = value & MASK32
        elif count == 2:
            lo, hi = f64_bits_to_words(value)
            regs[index] = lo
            regs[index + 1] = hi
        else:
            raise ValueError(f"Unsupported register count: {count}")


class ScalarInterpreter:
    """Deterministic scalar instruction interpreter for integer <-> float64 conversions."""

    def resolve_operand(self, op: VOP1Operand, state: ScalarMachineState) -> int:
        if isinstance(op.source, Register):
            return state.read_reg(op.source.kind, op.source.index, op.source.count)
        elif isinstance(op.source, (Literal32, Literal64)):
            return op.source.bits
        raise TypeError(f"Unknown operand source type: {type(op.source)}")

    def step(self, instr: VCvtInstruction, state: ScalarMachineState) -> int:
        # Atomic operand read phase
        raw_src = self.resolve_operand(instr.src0, state)

        # Execution phase via reference mathematical oracle
        if instr.opcode == Opcode.V_CVT_F64_I32:
            res = oracle_v_cvt_f64_i32(raw_src, clamp=instr.clamp)
        elif instr.opcode == Opcode.V_CVT_F64_U32:
            res = oracle_v_cvt_f64_u32(raw_src, clamp=instr.clamp)
        elif instr.opcode == Opcode.V_CVT_I32_F64:
            res = oracle_v_cvt_i32_f64(raw_src, abs_mod=instr.src0.abs_mod, neg_mod=instr.src0.neg_mod)
        elif instr.opcode == Opcode.V_CVT_U32_F64:
            res = oracle_v_cvt_u32_f64(raw_src, abs_mod=instr.src0.abs_mod, neg_mod=instr.src0.neg_mod)
        else:
            raise NotImplementedError(f"Unsupported opcode {instr.opcode}")

        # Destination write-back phase
        state.write_reg(instr.dst.kind, instr.dst.index, instr.dst.count, res)
        return res



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

    def read_lane_reg(self, lane: int, kind: RegKind, index: int, count: int = 1) -> int:
        if not (0 <= lane < self.num_lanes):
            raise IndexError(f"Lane {lane} out of range [0, {self.num_lanes})")
        if index < 0 or index + count > 256:
            raise IndexError(f"Register index {index} + count {count} out of bounds")
        if kind == RegKind.VGPR:
            if count == 1:
                return self.vgprs[lane][index] & MASK32
            elif count == 2:
                return words_to_f64_bits(self.vgprs[lane][index], self.vgprs[lane][index + 1])
        else:
            if count == 1:
                return self.sgprs[index] & MASK32
            elif count == 2:
                return words_to_f64_bits(self.sgprs[index], self.sgprs[index + 1])
        raise ValueError(f"Unsupported register count: {count}")

    def write_lane_reg(self, lane: int, kind: RegKind, index: int, count: int, value: int) -> None:
        if not (0 <= lane < self.num_lanes):
            raise IndexError(f"Lane {lane} out of range [0, {self.num_lanes})")
        if index < 0 or index + count > 256:
            raise IndexError(f"Register index {index} + count {count} out of bounds")
        if kind == RegKind.VGPR:
            if count == 1:
                self.vgprs[lane][index] = value & MASK32
            elif count == 2:
                lo, hi = f64_bits_to_words(value)
                self.vgprs[lane][index] = lo
                self.vgprs[lane][index + 1] = hi
            else:
                raise ValueError(f"Unsupported count: {count}")
        else:
            if count == 1:
                self.sgprs[index] = value & MASK32
            elif count == 2:
                lo, hi = f64_bits_to_words(value)
                self.sgprs[index] = lo
                self.sgprs[index + 1] = hi
            else:
                raise ValueError(f"Unsupported count: {count}")


class SIMDInterpreter:
    """SIMD instruction interpreter handling multi-lane divergence and EXEC masking."""

    def resolve_lane_operand(self, op: VOP1Operand, state: SIMDState, lane: int) -> int:
        if isinstance(op.source, Register):
            return state.read_lane_reg(lane, op.source.kind, op.source.index, op.source.count)
        elif isinstance(op.source, (Literal32, Literal64)):
            return op.source.bits
        raise TypeError(f"Unknown operand source type: {type(op.source)}")

    def step(self, instr: VCvtInstruction, state: SIMDState) -> List[Optional[int]]:
        results: List[Optional[int]] = [None] * state.num_lanes
        # Atomic read phase across all active lanes
        raw_inputs = [
            self.resolve_lane_operand(instr.src0, state, lane)
            for lane in range(state.num_lanes)
        ]

        # Execution phase
        for lane in range(state.num_lanes):
            if (state.exec_mask & (1 << lane)) != 0:
                raw_src = raw_inputs[lane]
                if instr.opcode == Opcode.V_CVT_F64_I32:
                    res = oracle_v_cvt_f64_i32(raw_src, clamp=instr.clamp)
                elif instr.opcode == Opcode.V_CVT_F64_U32:
                    res = oracle_v_cvt_f64_u32(raw_src, clamp=instr.clamp)
                elif instr.opcode == Opcode.V_CVT_I32_F64:
                    res = oracle_v_cvt_i32_f64(raw_src, abs_mod=instr.src0.abs_mod, neg_mod=instr.src0.neg_mod)
                elif instr.opcode == Opcode.V_CVT_U32_F64:
                    res = oracle_v_cvt_u32_f64(raw_src, abs_mod=instr.src0.abs_mod, neg_mod=instr.src0.neg_mod)
                else:
                    raise NotImplementedError(f"Unsupported opcode {instr.opcode}")

                state.write_lane_reg(lane, instr.dst.kind, instr.dst.index, instr.dst.count, res)
                results[lane] = res

        return results



# ==============================================================================
# Condition Codes, Status Flags, and Carry/Borrow Generation
# ==============================================================================

@dataclass(frozen=True)
class CvtStatusFlags:
    """Architectural status flags, condition codes, and saturation indicators."""
    invalid: bool = False
    overflow: bool = False
    underflow: bool = False
    inexact: bool = False
    zero: bool = False
    negative: bool = False
    saturated: bool = False


def evaluate_cvt_flags(
    opcode: Opcode,
    src_val: int,
    res_val: int,
    abs_mod: bool = False,
    neg_mod: bool = False,
    clamp: bool = False,
) -> CvtStatusFlags:
    """Evaluate architectural condition codes and status flags for conversion instructions."""
    invalid = False
    overflow = False
    underflow = False
    inexact = False
    zero = False
    negative = False
    saturated = False

    if opcode == Opcode.V_CVT_F64_I32:
        s_val = u32_to_i32(src_val)
        zero = (res_val == POS_ZERO_64 or res_val == NEG_ZERO_64)
        negative = bool(res_val & SIGN_MASK_64)
        if clamp:
            if s_val < 0 or s_val > 1:
                saturated = True
            if s_val < 0:
                underflow = True
            elif s_val > 1:
                overflow = True

    elif opcode == Opcode.V_CVT_F64_U32:
        u_val = src_val & MASK32
        zero = (res_val == POS_ZERO_64)
        negative = False
        if clamp:
            if u_val > 1:
                saturated = True
                overflow = True

    elif opcode == Opcode.V_CVT_I32_F64:
        s0 = apply_f64_modifiers(src_val, abs_mod, neg_mod)
        if is_nan_f64(s0):
            invalid = True
            zero = True
        elif is_inf_f64(s0):
            invalid = True
            overflow = True
            saturated = True
            if s0 == NEG_INF_64:
                negative = True
        else:
            f = bits_to_f64(s0)
            zero = (res_val == 0)
            negative = bool(res_val & 0x8000_0000)
            if math.trunc(f) != f:
                inexact = True
            if f >= INT32_MAX:
                overflow = True
                if f > INT32_MAX:
                    saturated = True
            elif f <= INT32_MIN:
                underflow = True
                if f < INT32_MIN:
                    saturated = True
            if is_subnormal_f64(s0):
                underflow = True

    elif opcode == Opcode.V_CVT_U32_F64:
        s0 = apply_f64_modifiers(src_val, abs_mod, neg_mod)
        if is_nan_f64(s0):
            invalid = True
            zero = True
        elif is_inf_f64(s0):
            invalid = True
            if s0 == POS_INF_64:
                overflow = True
                saturated = True
            else:
                underflow = True
                zero = True
                saturated = True
        else:
            f = bits_to_f64(s0)
            zero = (res_val == 0)
            negative = False
            if math.trunc(f) != f:
                inexact = True
            if f >= UINT32_MAX:
                overflow = True
                if f > UINT32_MAX:
                    saturated = True
            elif f < 0.0:
                underflow = True
                saturated = True
            if is_subnormal_f64(s0):
                underflow = True

    return CvtStatusFlags(
        invalid=invalid,
        overflow=overflow,
        underflow=underflow,
        inexact=inexact,
        zero=zero,
        negative=negative,
        saturated=saturated,
    )


def compute_f64_to_int_carry_borrow(val: float, is_signed: bool) -> Tuple[int, int]:
    """Demonstrate 32-bit architectural boundary carry/borrow generation for float truncation."""
    if math.isnan(val):
        return 0, 0
    t = int(math.trunc(val))
    if is_signed:
        carry = 1 if t > INT32_MAX else 0
        borrow = 1 if t < INT32_MIN else 0
    else:
        carry = 1 if t > UINT32_MAX else 0
        borrow = 1 if t < 0 else 0
    return carry, borrow


# ==============================================================================
# Unit Tests: Mathematical Model and Reference Oracles
# ==============================================================================

class TestVCvtIntF64ModelAndOracle(unittest.TestCase):
    """Unit tests for binary64 bit manipulation, integer conversion, and reference oracles."""

    def test_f64_bit_packing_and_predicates(self) -> None:
        values = [0.0, -0.0, 1.0, -1.0, 42.5, -123.75, math.pi, 1e-100, 1e100]
        for v in values:
            bits = f64_to_bits(v)
            self.assertEqual(bits_to_f64(bits), v)
            lo, hi = f64_bits_to_words(bits)
            self.assertEqual(words_to_f64_bits(lo, hi), bits)

        self.assertTrue(is_zero_f64(POS_ZERO_64))
        self.assertTrue(is_zero_f64(NEG_ZERO_64))
        self.assertFalse(is_zero_f64(POS_ONE_64))

        self.assertTrue(is_inf_f64(POS_INF_64))
        self.assertTrue(is_inf_f64(NEG_INF_64))
        self.assertFalse(is_inf_f64(POS_ZERO_64))

        self.assertTrue(is_nan_f64(CANONICAL_QNAN_64))
        self.assertTrue(is_qnan_f64(CANONICAL_QNAN_64))
        self.assertFalse(is_snan_f64(CANONICAL_QNAN_64))

        snan = 0x7FF0_0000_0000_0001
        self.assertTrue(is_nan_f64(snan))
        self.assertTrue(is_snan_f64(snan))
        self.assertFalse(is_qnan_f64(snan))

        subnorm = 0x0000_0000_0000_0001
        self.assertTrue(is_subnormal_f64(subnorm))
        self.assertFalse(is_subnormal_f64(POS_ZERO_64))

    def test_i32_u32_conversions(self) -> None:
        self.assertEqual(i32_to_u32(0), 0)
        self.assertEqual(i32_to_u32(-1), 0xFFFF_FFFF)
        self.assertEqual(i32_to_u32(INT32_MIN), 0x8000_0000)
        self.assertEqual(i32_to_u32(INT32_MAX), 0x7FFF_FFFF)

        self.assertEqual(u32_to_i32(0), 0)
        self.assertEqual(u32_to_i32(0xFFFF_FFFF), -1)
        self.assertEqual(u32_to_i32(0x8000_0000), INT32_MIN)
        self.assertEqual(u32_to_i32(0x7FFF_FFFF), INT32_MAX)

    def test_oracle_v_cvt_f64_i32_exact(self) -> None:
        test_cases = [
            (0, POS_ZERO_64),
            (1, POS_ONE_64),
            (-1, f64_to_bits(-1.0)),
            (42, f64_to_bits(42.0)),
            (-1337, f64_to_bits(-1337.0)),
            (INT32_MAX, f64_to_bits(2147483647.0)),
            (i32_to_u32(INT32_MIN), f64_to_bits(-2147483648.0)),
        ]
        for src, expected in test_cases:
            with self.subTest(src=src):
                res = oracle_v_cvt_f64_i32(src)
                self.assertEqual(res, expected)
                self.assertEqual(bits_to_f64(res), bits_to_f64(expected))

    def test_oracle_v_cvt_f64_i32_clamp(self) -> None:
        # Clamping negative integer to [0.0, 1.0] produces +0.0
        self.assertEqual(oracle_v_cvt_f64_i32(-50, clamp=True), POS_ZERO_64)
        self.assertEqual(oracle_v_cvt_f64_i32(i32_to_u32(INT32_MIN), clamp=True), POS_ZERO_64)
        # Clamping 0 produces +0.0
        self.assertEqual(oracle_v_cvt_f64_i32(0, clamp=True), POS_ZERO_64)
        # Clamping 1 produces 1.0
        self.assertEqual(oracle_v_cvt_f64_i32(1, clamp=True), POS_ONE_64)
        # Clamping integer > 1 produces 1.0
        self.assertEqual(oracle_v_cvt_f64_i32(2, clamp=True), POS_ONE_64)
        self.assertEqual(oracle_v_cvt_f64_i32(INT32_MAX, clamp=True), POS_ONE_64)

    def test_oracle_v_cvt_f64_u32_exact(self) -> None:
        test_cases = [
            (0, POS_ZERO_64),
            (1, POS_ONE_64),
            (255, f64_to_bits(255.0)),
            (65535, f64_to_bits(65535.0)),
            (0x8000_0000, f64_to_bits(2147483648.0)),
            (UINT32_MAX, f64_to_bits(4294967295.0)),
        ]
        for src, expected in test_cases:
            with self.subTest(src=src):
                res = oracle_v_cvt_f64_u32(src)
                self.assertEqual(res, expected)
                self.assertEqual(bits_to_f64(res), bits_to_f64(expected))

    def test_oracle_v_cvt_f64_u32_clamp(self) -> None:
        self.assertEqual(oracle_v_cvt_f64_u32(0, clamp=True), POS_ZERO_64)
        self.assertEqual(oracle_v_cvt_f64_u32(1, clamp=True), POS_ONE_64)
        self.assertEqual(oracle_v_cvt_f64_u32(2, clamp=True), POS_ONE_64)
        self.assertEqual(oracle_v_cvt_f64_u32(UINT32_MAX, clamp=True), POS_ONE_64)

    def test_oracle_v_cvt_i32_f64_finite_and_trunc(self) -> None:
        self.assertEqual(oracle_v_cvt_i32_f64(f64_to_bits(0.0)), 0)
        self.assertEqual(oracle_v_cvt_i32_f64(f64_to_bits(-0.0)), 0)
        # Truncation towards zero
        self.assertEqual(oracle_v_cvt_i32_f64(f64_to_bits(2.0)), 2)
        self.assertEqual(oracle_v_cvt_i32_f64(f64_to_bits(2.1)), 2)
        self.assertEqual(oracle_v_cvt_i32_f64(f64_to_bits(2.999)), 2)
        self.assertEqual(oracle_v_cvt_i32_f64(f64_to_bits(-2.0)), i32_to_u32(-2))
        self.assertEqual(oracle_v_cvt_i32_f64(f64_to_bits(-2.1)), i32_to_u32(-2))
        self.assertEqual(oracle_v_cvt_i32_f64(f64_to_bits(-2.999)), i32_to_u32(-2))

    def test_oracle_v_cvt_i32_f64_saturation_and_nan(self) -> None:
        # NaN returns 0
        self.assertEqual(oracle_v_cvt_i32_f64(CANONICAL_QNAN_64), 0)
        self.assertEqual(oracle_v_cvt_i32_f64(0x7FF0_0000_1234_5678), 0)

        # Infinities
        self.assertEqual(oracle_v_cvt_i32_f64(POS_INF_64), INT32_MAX)
        self.assertEqual(oracle_v_cvt_i32_f64(NEG_INF_64), i32_to_u32(INT32_MIN))

        # Saturation thresholds
        self.assertEqual(oracle_v_cvt_i32_f64(f64_to_bits(2147483647.0)), INT32_MAX)
        self.assertEqual(oracle_v_cvt_i32_f64(f64_to_bits(2147483647.9)), INT32_MAX)
        self.assertEqual(oracle_v_cvt_i32_f64(f64_to_bits(2147483648.0)), INT32_MAX)
        self.assertEqual(oracle_v_cvt_i32_f64(f64_to_bits(1e30)), INT32_MAX)

        self.assertEqual(oracle_v_cvt_i32_f64(f64_to_bits(-2147483648.0)), i32_to_u32(INT32_MIN))
        self.assertEqual(oracle_v_cvt_i32_f64(f64_to_bits(-2147483648.9)), i32_to_u32(INT32_MIN))
        self.assertEqual(oracle_v_cvt_i32_f64(f64_to_bits(-2147483649.0)), i32_to_u32(INT32_MIN))
        self.assertEqual(oracle_v_cvt_i32_f64(f64_to_bits(-1e30)), i32_to_u32(INT32_MIN))

    def test_oracle_v_cvt_i32_f64_modifiers(self) -> None:
        # abs modifier
        self.assertEqual(oracle_v_cvt_i32_f64(f64_to_bits(-55.7), abs_mod=True), 55)
        # neg modifier
        self.assertEqual(oracle_v_cvt_i32_f64(f64_to_bits(42.3), neg_mod=True), i32_to_u32(-42))
        # abs + neg modifier: -|-12.3| = -12.3 -> -12
        self.assertEqual(oracle_v_cvt_i32_f64(f64_to_bits(-12.3), abs_mod=True, neg_mod=True), i32_to_u32(-12))

    def test_oracle_v_cvt_u32_f64_finite_and_trunc(self) -> None:
        self.assertEqual(oracle_v_cvt_u32_f64(f64_to_bits(0.0)), 0)
        self.assertEqual(oracle_v_cvt_u32_f64(f64_to_bits(-0.0)), 0)
        self.assertEqual(oracle_v_cvt_u32_f64(f64_to_bits(5.0)), 5)
        self.assertEqual(oracle_v_cvt_u32_f64(f64_to_bits(5.8)), 5)
        self.assertEqual(oracle_v_cvt_u32_f64(f64_to_bits(100.99)), 100)

    def test_oracle_v_cvt_u32_f64_saturation_and_nan(self) -> None:
        # NaN returns 0
        self.assertEqual(oracle_v_cvt_u32_f64(CANONICAL_QNAN_64), 0)

        # Negative floats clamp to 0
        self.assertEqual(oracle_v_cvt_u32_f64(f64_to_bits(-0.5)), 0)
        self.assertEqual(oracle_v_cvt_u32_f64(f64_to_bits(-100.0)), 0)
        self.assertEqual(oracle_v_cvt_u32_f64(NEG_INF_64), 0)

        # Positive infinity and overflow saturate to UINT32_MAX
        self.assertEqual(oracle_v_cvt_u32_f64(POS_INF_64), UINT32_MAX)
        self.assertEqual(oracle_v_cvt_u32_f64(f64_to_bits(4294967295.0)), UINT32_MAX)
        self.assertEqual(oracle_v_cvt_u32_f64(f64_to_bits(4294967295.9)), UINT32_MAX)
        self.assertEqual(oracle_v_cvt_u32_f64(f64_to_bits(4294967296.0)), UINT32_MAX)
        self.assertEqual(oracle_v_cvt_u32_f64(f64_to_bits(1e30)), UINT32_MAX)

    def test_oracle_v_cvt_u32_f64_modifiers(self) -> None:
        # abs converts negative to positive before conversion
        self.assertEqual(oracle_v_cvt_u32_f64(f64_to_bits(-123.45), abs_mod=True), 123)
        # neg converts positive to negative -> clamps to 0
        self.assertEqual(oracle_v_cvt_u32_f64(f64_to_bits(50.0), neg_mod=True), 0)


class TestSymbolicAST(unittest.TestCase):
    """Unit tests for symbolic AST opcode definitions and typed expressions."""

    def test_register_creation_and_bounds(self) -> None:
        v0 = Register(RegKind.VGPR, 0, count=1)
        self.assertEqual(v0.name, "v0")
        v_pair = Register(RegKind.VGPR, 2, count=2)
        self.assertEqual(v_pair.name, "v[2:3]")

        s8 = Register(RegKind.SGPR, 8, count=1)
        self.assertEqual(s8.name, "s8")
        s_pair = Register(RegKind.SGPR, 10, count=2)
        self.assertEqual(s_pair.name, "s[10:11]")

        with self.assertRaises(ValueError):
            Register(RegKind.VGPR, -1)
        with self.assertRaises(ValueError):
            Register(RegKind.VGPR, 256)
        with self.assertRaises(ValueError):
            Register(RegKind.VGPR, 0, count=3)
        with self.assertRaises(ValueError):
            # Index 255 with count 2 spills past 255
            Register(RegKind.VGPR, 255, count=2)

    def test_literal_properties(self) -> None:
        lit32 = Literal32(0xFFFF_FFFF)
        self.assertEqual(lit32.as_uint, 0xFFFF_FFFF)
        self.assertEqual(lit32.as_int, -1)
        self.assertEqual(lit32.name, "-1")

        lit64 = Literal64.from_float(3.14)
        self.assertAlmostEqual(lit64.float_value, 3.14)
        self.assertEqual(lit64.name, "3.14")

    def test_operand_modifiers_disassembly(self) -> None:
        v_pair = Register(RegKind.VGPR, 4, count=2)
        op_plain = VOP1Operand(v_pair)
        self.assertEqual(op_plain.disassemble(), "v[4:5]")

        op_abs = VOP1Operand(v_pair, abs_mod=True)
        self.assertEqual(op_abs.disassemble(), "|v[4:5]|")

        op_neg = VOP1Operand(v_pair, neg_mod=True)
        self.assertEqual(op_neg.disassemble(), "-v[4:5]")

        op_neg_abs = VOP1Operand(v_pair, abs_mod=True, neg_mod=True)
        self.assertEqual(op_neg_abs.disassemble(), "-|v[4:5]|")

    def test_instruction_construction_all_opcodes(self) -> None:
        # v_cvt_f64_i32
        i1 = VCvtInstruction(
            opcode=Opcode.V_CVT_F64_I32,
            dst=Register(RegKind.VGPR, 0, count=2),
            src0=VOP1Operand(Register(RegKind.VGPR, 4, count=1)),
            clamp=True,
        )
        self.assertEqual(i1.disassemble(), "v_cvt_f64_i32 v[0:1], v4 clamp")

        # v_cvt_f64_u32
        i2 = VCvtInstruction(
            opcode=Opcode.V_CVT_F64_U32,
            dst=Register(RegKind.VGPR, 2, count=2),
            src0=VOP1Operand(Register(RegKind.SGPR, 6, count=1)),
        )
        self.assertEqual(i2.disassemble(), "v_cvt_f64_u32 v[2:3], s6")

        # v_cvt_i32_f64
        i3 = VCvtInstruction(
            opcode=Opcode.V_CVT_I32_F64,
            dst=Register(RegKind.VGPR, 0, count=1),
            src0=VOP1Operand(Register(RegKind.VGPR, 2, count=2), abs_mod=True, neg_mod=True),
        )
        self.assertEqual(i3.disassemble(), "v_cvt_i32_f64 v0, -|v[2:3]|")

        # v_cvt_u32_f64
        i4 = VCvtInstruction(
            opcode=Opcode.V_CVT_U32_F64,
            dst=Register(RegKind.VGPR, 10, count=1),
            src0=VOP1Operand(Literal64.from_float(10.5)),
        )
        self.assertEqual(i4.disassemble(), "v_cvt_u32_f64 v10, 10.5")

    def test_instruction_validation_errors(self) -> None:
        # Non-VGPR destination
        with self.assertRaises(TypeError):
            VCvtInstruction(
                opcode=Opcode.V_CVT_F64_I32,
                dst=Register(RegKind.SGPR, 0, count=2),
                src0=VOP1Operand(Register(RegKind.VGPR, 2, count=1)),
            )

        # Count mismatch: v_cvt_f64_i32 requires dst count=2
        with self.assertRaises(ValueError):
            VCvtInstruction(
                opcode=Opcode.V_CVT_F64_I32,
                dst=Register(RegKind.VGPR, 0, count=1),
                src0=VOP1Operand(Register(RegKind.VGPR, 2, count=1)),
            )

        # Count mismatch: v_cvt_i32_f64 requires dst count=1
        with self.assertRaises(ValueError):
            VCvtInstruction(
                opcode=Opcode.V_CVT_I32_F64,
                dst=Register(RegKind.VGPR, 0, count=2),
                src0=VOP1Operand(Register(RegKind.VGPR, 2, count=2)),
            )

        # Clamp invalid on integer destination
        with self.assertRaises(ValueError):
            VCvtInstruction(
                opcode=Opcode.V_CVT_I32_F64,
                dst=Register(RegKind.VGPR, 0, count=1),
                src0=VOP1Operand(Register(RegKind.VGPR, 2, count=2)),
                clamp=True,
            )

    def test_structural_equality(self) -> None:
        inst_a = VCvtInstruction(
            opcode=Opcode.V_CVT_F64_U32,
            dst=Register(RegKind.VGPR, 0, count=2),
            src0=VOP1Operand(Literal32(42)),
        )
        inst_b = VCvtInstruction(
            opcode=Opcode.V_CVT_F64_U32,
            dst=Register(RegKind.VGPR, 0, count=2),
            src0=VOP1Operand(Literal32(42)),
        )
        self.assertEqual(inst_a, inst_b)


class TestScalarInterpreter(unittest.TestCase):
    """Unit tests for the deterministic scalar instruction interpreter."""

    def setUp(self) -> None:
        self.interp = ScalarInterpreter()
        self.state = ScalarMachineState()

    def test_scalar_v_cvt_f64_i32(self) -> None:
        # Load signed int -42 in v4
        self.state.write_reg(RegKind.VGPR, 4, 1, i32_to_u32(-42))
        inst = VCvtInstruction(
            opcode=Opcode.V_CVT_F64_I32,
            dst=Register(RegKind.VGPR, 0, count=2),
            src0=VOP1Operand(Register(RegKind.VGPR, 4, count=1)),
        )
        res = self.interp.step(inst, self.state)
        self.assertEqual(res, f64_to_bits(-42.0))
        self.assertEqual(self.state.read_reg(RegKind.VGPR, 0, count=2), f64_to_bits(-42.0))

        # With clamp: negative int clamps to +0.0
        inst_clamp = VCvtInstruction(
            opcode=Opcode.V_CVT_F64_I32,
            dst=Register(RegKind.VGPR, 0, count=2),
            src0=VOP1Operand(Register(RegKind.VGPR, 4, count=1)),
            clamp=True,
        )
        res_clamp = self.interp.step(inst_clamp, self.state)
        self.assertEqual(res_clamp, POS_ZERO_64)

    def test_scalar_v_cvt_f64_u32(self) -> None:
        # From SGPR literal / constant: 0x8000_0000 = 2147483648
        self.state.write_reg(RegKind.SGPR, 12, 1, 0x8000_0000)
        inst = VCvtInstruction(
            opcode=Opcode.V_CVT_F64_U32,
            dst=Register(RegKind.VGPR, 6, count=2),
            src0=VOP1Operand(Register(RegKind.SGPR, 12, count=1)),
        )
        res = self.interp.step(inst, self.state)
        self.assertEqual(res, f64_to_bits(2147483648.0))
        self.assertEqual(self.state.read_reg(RegKind.VGPR, 6, count=2), f64_to_bits(2147483648.0))

    def test_scalar_v_cvt_i32_f64(self) -> None:
        # Load double 123.75 into v[2:3]
        self.state.write_reg(RegKind.VGPR, 2, 2, f64_to_bits(123.75))
        inst = VCvtInstruction(
            opcode=Opcode.V_CVT_I32_F64,
            dst=Register(RegKind.VGPR, 0, count=1),
            src0=VOP1Operand(Register(RegKind.VGPR, 2, count=2)),
        )
        res = self.interp.step(inst, self.state)
        self.assertEqual(res, 123)
        self.assertEqual(self.state.read_reg(RegKind.VGPR, 0, count=1), 123)

        # With neg modifier: -123.75 -> -123
        inst_neg = VCvtInstruction(
            opcode=Opcode.V_CVT_I32_F64,
            dst=Register(RegKind.VGPR, 1, count=1),
            src0=VOP1Operand(Register(RegKind.VGPR, 2, count=2), neg_mod=True),
        )
        res_neg = self.interp.step(inst_neg, self.state)
        self.assertEqual(res_neg, i32_to_u32(-123))

    def test_scalar_v_cvt_u32_f64(self) -> None:
        # Load float -999.0 in s[4:5], with abs modifier -> 999
        self.state.write_reg(RegKind.SGPR, 4, 2, f64_to_bits(-999.0))
        inst = VCvtInstruction(
            opcode=Opcode.V_CVT_U32_F64,
            dst=Register(RegKind.VGPR, 8, count=1),
            src0=VOP1Operand(Register(RegKind.SGPR, 4, count=2), abs_mod=True),
        )
        res = self.interp.step(inst, self.state)
        self.assertEqual(res, 999)
        self.assertEqual(self.state.read_reg(RegKind.VGPR, 8, count=1), 999)

    def test_scalar_determinism(self) -> None:
        runs = []
        for _ in range(50):
            st = ScalarMachineState()
            st.write_reg(RegKind.VGPR, 0, 2, f64_to_bits(42.8))
            inst = VCvtInstruction(
                opcode=Opcode.V_CVT_I32_F64,
                dst=Register(RegKind.VGPR, 2, count=1),
                src0=VOP1Operand(Register(RegKind.VGPR, 0, count=2)),
            )
            runs.append(self.interp.step(inst, st))
        self.assertTrue(all(r == runs[0] for r in runs))
        self.assertEqual(runs[0], 42)


class TestSIMDMultiLaneDivergence(unittest.TestCase):
    """Unit tests for SIMD execution, lane divergence, and 32/64-bit EXEC masking."""

    def setUp(self) -> None:
        self.interp = SIMDInterpreter()

    def test_wave32_full_exec(self) -> None:
        state = SIMDState(num_lanes=32, exec_mask=0xFFFF_FFFF)
        for lane in range(32):
            # Write lane number as signed integer into v4
            state.write_lane_reg(lane, RegKind.VGPR, 4, 1, i32_to_u32(lane - 16))

        inst = VCvtInstruction(
            opcode=Opcode.V_CVT_F64_I32,
            dst=Register(RegKind.VGPR, 0, count=2),
            src0=VOP1Operand(Register(RegKind.VGPR, 4, count=1)),
        )
        results = self.interp.step(inst, state)
        for lane in range(32):
            self.assertIsNotNone(results[lane])
            expected_f = float(lane - 16)
            self.assertEqual(bits_to_f64(state.read_lane_reg(lane, RegKind.VGPR, 0, count=2)), expected_f)

    def test_wave32_divergent_masking(self) -> None:
        canary = 0xDEAD_BEEF
        # Alternating mask: even lanes active (0x55555555)
        state = SIMDState(num_lanes=32, exec_mask=0x5555_5555)
        for lane in range(32):
            state.write_lane_reg(lane, RegKind.VGPR, 2, 2, f64_to_bits(float(lane) * 2.5))
            state.write_lane_reg(lane, RegKind.VGPR, 0, 1, canary)

        inst = VCvtInstruction(
            opcode=Opcode.V_CVT_I32_F64,
            dst=Register(RegKind.VGPR, 0, count=1),
            src0=VOP1Operand(Register(RegKind.VGPR, 2, count=2)),
        )
        results = self.interp.step(inst, state)
        for lane in range(32):
            if lane % 2 == 0:
                self.assertIsNotNone(results[lane])
                expected_int = int(math.trunc(float(lane) * 2.5))
                self.assertEqual(state.read_lane_reg(lane, RegKind.VGPR, 0, count=1), expected_int)
            else:
                self.assertIsNone(results[lane])
                self.assertEqual(state.read_lane_reg(lane, RegKind.VGPR, 0, count=1), canary)

    def test_wave64_full_exec(self) -> None:
        state = SIMDState(num_lanes=64, exec_mask=0xFFFF_FFFF_FFFF_FFFF)
        for lane in range(64):
            state.write_lane_reg(lane, RegKind.VGPR, 6, 1, lane * 100)

        inst = VCvtInstruction(
            opcode=Opcode.V_CVT_F64_U32,
            dst=Register(RegKind.VGPR, 0, count=2),
            src0=VOP1Operand(Register(RegKind.VGPR, 6, count=1)),
        )
        self.interp.step(inst, state)
        for lane in range(64):
            self.assertEqual(
                bits_to_f64(state.read_lane_reg(lane, RegKind.VGPR, 0, count=2)),
                float(lane * 100),
            )

    def test_wave64_half_disabled(self) -> None:
        canary = 0xCAFE_BABE
        # Lower 32 lanes active, upper 32 inactive
        state = SIMDState(num_lanes=64, exec_mask=0x0000_0000_FFFF_FFFF)
        for lane in range(64):
            state.write_lane_reg(lane, RegKind.VGPR, 2, 2, f64_to_bits(100.9))
            state.write_lane_reg(lane, RegKind.VGPR, 0, 1, canary)

        inst = VCvtInstruction(
            opcode=Opcode.V_CVT_U32_F64,
            dst=Register(RegKind.VGPR, 0, count=1),
            src0=VOP1Operand(Register(RegKind.VGPR, 2, count=2)),
        )
        results = self.interp.step(inst, state)
        for lane in range(64):
            if lane < 32:
                self.assertIsNotNone(results[lane])
                self.assertEqual(state.read_lane_reg(lane, RegKind.VGPR, 0, count=1), 100)
            else:
                self.assertIsNone(results[lane])
                self.assertEqual(state.read_lane_reg(lane, RegKind.VGPR, 0, count=1), canary)

    def test_wave_exec_all_zero(self) -> None:
        canary_lo = 0x1111_2222
        canary_hi = 0x3333_4444
        state = SIMDState(num_lanes=32, exec_mask=0)
        for lane in range(32):
            state.write_lane_reg(lane, RegKind.VGPR, 0, 2, words_to_f64_bits(canary_lo, canary_hi))

        inst = VCvtInstruction(
            opcode=Opcode.V_CVT_F64_I32,
            dst=Register(RegKind.VGPR, 0, count=2),
            src0=VOP1Operand(Literal32(99)),
        )
        results = self.interp.step(inst, state)
        self.assertTrue(all(r is None for r in results))
        for lane in range(32):
            self.assertEqual(
                state.read_lane_reg(lane, RegKind.VGPR, 0, count=2),
                words_to_f64_bits(canary_lo, canary_hi),
            )

    def test_simd_sgpr_uniform_broadcast(self) -> None:
        state = SIMDState(num_lanes=32, exec_mask=0xFFFF_FFFF)
        state.write_lane_reg(0, RegKind.SGPR, 8, 2, f64_to_bits(777.9))

        inst = VCvtInstruction(
            opcode=Opcode.V_CVT_I32_F64,
            dst=Register(RegKind.VGPR, 0, count=1),
            src0=VOP1Operand(Register(RegKind.SGPR, 8, count=2)),
        )
        self.interp.step(inst, state)
        for lane in range(32):
            self.assertEqual(state.read_lane_reg(lane, RegKind.VGPR, 0, count=1), 777)


class TestRegisterAliasing(unittest.TestCase):
    """Unit tests for register aliasing and in-place operand preservation."""

    def setUp(self) -> None:
        self.scalar_interp = ScalarInterpreter()
        self.simd_interp = SIMDInterpreter()

    def test_dst_pair_alias_src_low_word(self) -> None:
        # dst = v[0:1], src = v0 (dst.lo aliases src)
        state = ScalarMachineState()
        state.write_reg(RegKind.VGPR, 0, 1, i32_to_u32(-999))
        state.write_reg(RegKind.VGPR, 1, 1, 0xAAAA_BBBB)  # previous high word

        inst = VCvtInstruction(
            opcode=Opcode.V_CVT_F64_I32,
            dst=Register(RegKind.VGPR, 0, count=2),
            src0=VOP1Operand(Register(RegKind.VGPR, 0, count=1)),
        )
        res = self.scalar_interp.step(inst, state)
        self.assertEqual(res, f64_to_bits(-999.0))
        self.assertEqual(state.read_reg(RegKind.VGPR, 0, count=2), f64_to_bits(-999.0))

    def test_dst_pair_alias_src_high_word(self) -> None:
        # dst = v[0:1], src = v1 (dst.hi aliases src)
        state = ScalarMachineState()
        state.write_reg(RegKind.VGPR, 0, 1, 0x1234_5678)
        state.write_reg(RegKind.VGPR, 1, 1, 12345)

        inst = VCvtInstruction(
            opcode=Opcode.V_CVT_F64_U32,
            dst=Register(RegKind.VGPR, 0, count=2),
            src0=VOP1Operand(Register(RegKind.VGPR, 1, count=1)),
        )
        res = self.scalar_interp.step(inst, state)
        self.assertEqual(res, f64_to_bits(12345.0))
        self.assertEqual(state.read_reg(RegKind.VGPR, 0, count=2), f64_to_bits(12345.0))

    def test_dst_alias_src_pair_low_word(self) -> None:
        # dst = v0 (32-bit), src = v[0:1] (64-bit float).
        # v0 gets converted integer, v1 must remain strictly untouched!
        state = ScalarMachineState()
        original_float = 54321.875
        float_bits = f64_to_bits(original_float)
        lo_orig, hi_orig = f64_bits_to_words(float_bits)

        state.write_reg(RegKind.VGPR, 0, 2, float_bits)
        self.assertEqual(state.vgprs[0], lo_orig)
        self.assertEqual(state.vgprs[1], hi_orig)

        inst = VCvtInstruction(
            opcode=Opcode.V_CVT_I32_F64,
            dst=Register(RegKind.VGPR, 0, count=1),
            src0=VOP1Operand(Register(RegKind.VGPR, 0, count=2)),
        )
        res = self.scalar_interp.step(inst, state)
        self.assertEqual(res, 54321)
        self.assertEqual(state.read_reg(RegKind.VGPR, 0, count=1), 54321)
        # v1 must remain identical to hi_orig
        self.assertEqual(state.vgprs[1], hi_orig)

    def test_dst_alias_src_pair_high_word(self) -> None:
        # dst = v1 (32-bit), src = v[0:1] (64-bit float).
        # v1 gets converted integer, v0 must remain strictly untouched!
        state = ScalarMachineState()
        original_float = -888.25
        float_bits = f64_to_bits(original_float)
        lo_orig, hi_orig = f64_bits_to_words(float_bits)

        state.write_reg(RegKind.VGPR, 0, 2, float_bits)
        inst = VCvtInstruction(
            opcode=Opcode.V_CVT_I32_F64,
            dst=Register(RegKind.VGPR, 1, count=1),
            src0=VOP1Operand(Register(RegKind.VGPR, 0, count=2)),
        )
        res = self.scalar_interp.step(inst, state)
        self.assertEqual(res, i32_to_u32(-888))
        self.assertEqual(state.read_reg(RegKind.VGPR, 1, count=1), i32_to_u32(-888))
        # v0 must remain identical to lo_orig
        self.assertEqual(state.vgprs[0], lo_orig)

    def test_adjacent_register_preservation(self) -> None:
        # dst = v2, src = v[3:4]. v1 and v5 must remain untouched.
        state = ScalarMachineState()
        state.write_reg(RegKind.VGPR, 1, 1, 0x1111_1111)
        state.write_reg(RegKind.VGPR, 2, 1, 0x2222_2222)
        state.write_reg(RegKind.VGPR, 3, 2, f64_to_bits(99.0))
        state.write_reg(RegKind.VGPR, 5, 1, 0x5555_5555)

        inst = VCvtInstruction(
            opcode=Opcode.V_CVT_U32_F64,
            dst=Register(RegKind.VGPR, 2, count=1),
            src0=VOP1Operand(Register(RegKind.VGPR, 3, count=2)),
        )
        self.scalar_interp.step(inst, state)
        self.assertEqual(state.read_reg(RegKind.VGPR, 2, count=1), 99)
        self.assertEqual(state.read_reg(RegKind.VGPR, 1, count=1), 0x1111_1111)
        self.assertEqual(state.read_reg(RegKind.VGPR, 5, count=1), 0x5555_5555)

    def test_simd_multi_lane_aliasing(self) -> None:
        state = SIMDState(num_lanes=32, exec_mask=0xFFFF_FFFF)
        for lane in range(32):
            val = float(lane * 10) + 0.75
            state.write_lane_reg(lane, RegKind.VGPR, 0, 2, f64_to_bits(val))

        inst = VCvtInstruction(
            opcode=Opcode.V_CVT_I32_F64,
            dst=Register(RegKind.VGPR, 0, count=1),
            src0=VOP1Operand(Register(RegKind.VGPR, 0, count=2)),
        )
        self.simd_interp.step(inst, state)
        for lane in range(32):
            self.assertEqual(state.read_lane_reg(lane, RegKind.VGPR, 0, count=1), lane * 10)
            # High word of original double in v1 remains untouched
            orig_hi = f64_bits_to_words(f64_to_bits(float(lane * 10) + 0.75))[1]
            self.assertEqual(state.vgprs[lane][1], orig_hi)


class TestConditionCodesAndStatusFlags(unittest.TestCase):
    """Unit tests for condition codes, status flags, and carry/borrow generation."""

    def test_exact_integer_flags(self) -> None:
        # v_cvt_f64_i32(0) -> 0.0
        res = oracle_v_cvt_f64_i32(0)
        flags = evaluate_cvt_flags(Opcode.V_CVT_F64_I32, 0, res)
        self.assertTrue(flags.zero)
        self.assertFalse(flags.negative)
        self.assertFalse(flags.inexact)
        self.assertFalse(flags.saturated)

        # v_cvt_f64_i32(-5) -> -5.0
        res_neg = oracle_v_cvt_f64_i32(-5)
        flags_neg = evaluate_cvt_flags(Opcode.V_CVT_F64_I32, -5, res_neg)
        self.assertFalse(flags_neg.zero)
        self.assertTrue(flags_neg.negative)

    def test_clamp_saturation_flags(self) -> None:
        # v_cvt_f64_i32(-10, clamp=True) -> 0.0
        res = oracle_v_cvt_f64_i32(-10, clamp=True)
        flags = evaluate_cvt_flags(Opcode.V_CVT_F64_I32, -10, res, clamp=True)
        self.assertTrue(flags.underflow)
        self.assertTrue(flags.saturated)
        self.assertTrue(flags.zero)

        # v_cvt_f64_u32(100, clamp=True) -> 1.0
        res_ov = oracle_v_cvt_f64_u32(100, clamp=True)
        flags_ov = evaluate_cvt_flags(Opcode.V_CVT_F64_U32, 100, res_ov, clamp=True)
        self.assertTrue(flags_ov.overflow)
        self.assertTrue(flags_ov.saturated)

    def test_inexact_truncation_flag(self) -> None:
        # 12.345 truncated to 12
        src = f64_to_bits(12.345)
        res = oracle_v_cvt_i32_f64(src)
        flags = evaluate_cvt_flags(Opcode.V_CVT_I32_F64, src, res)
        self.assertTrue(flags.inexact)
        self.assertFalse(flags.overflow)
        self.assertFalse(flags.saturated)

        # 12.0 exact integer
        src_exact = f64_to_bits(12.0)
        res_exact = oracle_v_cvt_i32_f64(src_exact)
        flags_exact = evaluate_cvt_flags(Opcode.V_CVT_I32_F64, src_exact, res_exact)
        self.assertFalse(flags_exact.inexact)

    def test_overflow_and_saturation_flags(self) -> None:
        # Float exceeding INT32_MAX
        src_large = f64_to_bits(3e9)
        res_large = oracle_v_cvt_i32_f64(src_large)
        flags_large = evaluate_cvt_flags(Opcode.V_CVT_I32_F64, src_large, res_large)
        self.assertTrue(flags_large.overflow)
        self.assertTrue(flags_large.saturated)
        self.assertEqual(res_large, INT32_MAX)

        # Negative float underflow in u32
        src_neg = f64_to_bits(-42.5)
        res_u32 = oracle_v_cvt_u32_f64(src_neg)
        flags_u32 = evaluate_cvt_flags(Opcode.V_CVT_U32_F64, src_neg, res_u32)
        self.assertTrue(flags_u32.underflow)
        self.assertTrue(flags_u32.saturated)
        self.assertTrue(flags_u32.zero)

    def test_invalid_nan_flags(self) -> None:
        res = oracle_v_cvt_i32_f64(CANONICAL_QNAN_64)
        flags = evaluate_cvt_flags(Opcode.V_CVT_I32_F64, CANONICAL_QNAN_64, res)
        self.assertTrue(flags.invalid)
        self.assertTrue(flags.zero)
        self.assertEqual(res, 0)

    def test_carry_borrow_generation(self) -> None:
        # Float within signed range
        c, b = compute_f64_to_int_carry_borrow(100.5, is_signed=True)
        self.assertEqual(c, 0)
        self.assertEqual(b, 0)

        # Float exceeding INT32_MAX triggers carry
        c_ov, b_ov = compute_f64_to_int_carry_borrow(2147483648.0, is_signed=True)
        self.assertEqual(c_ov, 1)
        self.assertEqual(b_ov, 0)

        # Float below INT32_MIN triggers borrow
        c_un, b_un = compute_f64_to_int_carry_borrow(-2147483649.0, is_signed=True)
        self.assertEqual(c_un, 0)
        self.assertEqual(b_un, 1)

        # Negative float in unsigned context triggers borrow
        c_u, b_u = compute_f64_to_int_carry_borrow(-1.0, is_signed=False)
        self.assertEqual(c_u, 0)
        self.assertEqual(b_u, 1)


class TestIEEE754BoundaryExtremes(unittest.TestCase):
    """Unit tests for IEEE 754 boundary extremes, subnormals, and NaN handling."""

    def test_subnormal_conversion_suppression(self) -> None:
        subnormals = [
            0x0000_0000_0000_0001,  # min positive subnormal
            0x000F_FFFF_FFFF_FFFF,  # max positive subnormal
            0x8000_0000_0000_0001,  # min negative subnormal
            0x800F_FFFF_FFFF_FFFF,  # max negative subnormal
        ]
        for sub in subnormals:
            with self.subTest(sub=hex(sub)):
                self.assertTrue(is_subnormal_f64(sub))
                self.assertEqual(oracle_v_cvt_i32_f64(sub), 0)
                self.assertEqual(oracle_v_cvt_u32_f64(sub), 0)

    def test_signed_zero_invariance(self) -> None:
        self.assertEqual(oracle_v_cvt_i32_f64(POS_ZERO_64), 0)
        self.assertEqual(oracle_v_cvt_i32_f64(NEG_ZERO_64), 0)
        self.assertEqual(oracle_v_cvt_u32_f64(POS_ZERO_64), 0)
        self.assertEqual(oracle_v_cvt_u32_f64(NEG_ZERO_64), 0)

        # With modifiers
        self.assertEqual(oracle_v_cvt_i32_f64(NEG_ZERO_64, abs_mod=True), 0)
        self.assertEqual(oracle_v_cvt_i32_f64(POS_ZERO_64, neg_mod=True), 0)
        self.assertEqual(oracle_v_cvt_u32_f64(NEG_ZERO_64, abs_mod=True), 0)

    def test_nan_payload_robustness(self) -> None:
        nans = [
            CANONICAL_QNAN_64,
            0x7FF0_0000_0000_0001,  # signaling NaN
            0x7FF8_DEAD_BEEF_CAFE,  # quiet NaN with payload
            0xFFF8_1234_5678_9ABC,  # negative quiet NaN
            0xFFF0_0000_0000_0002,  # negative signaling NaN
        ]
        for n in nans:
            with self.subTest(nan=hex(n)):
                self.assertTrue(is_nan_f64(n))
                self.assertEqual(oracle_v_cvt_i32_f64(n), 0)
                self.assertEqual(oracle_v_cvt_u32_f64(n), 0)
                # Modifiers on NaN must still produce 0
                self.assertEqual(oracle_v_cvt_i32_f64(n, abs_mod=True), 0)
                self.assertEqual(oracle_v_cvt_i32_f64(n, neg_mod=True), 0)
                self.assertEqual(oracle_v_cvt_u32_f64(n, abs_mod=True), 0)
                self.assertEqual(oracle_v_cvt_u32_f64(n, neg_mod=True), 0)

    def test_int32_and_uint32_exact_powers_of_two(self) -> None:
        # 2^31 - 1 = 2147483647.0
        p31_minus_1 = f64_to_bits(2147483647.0)
        self.assertEqual(oracle_v_cvt_i32_f64(p31_minus_1), 2147483647)
        self.assertEqual(oracle_v_cvt_u32_f64(p31_minus_1), 2147483647)

        # 2^31 = 2147483648.0
        p31 = f64_to_bits(2147483648.0)
        self.assertEqual(oracle_v_cvt_i32_f64(p31), INT32_MAX)  # saturates in i32
        self.assertEqual(oracle_v_cvt_u32_f64(p31), 0x8000_0000)  # exact in u32

        # 2^32 - 1 = 4294967295.0
        p32_minus_1 = f64_to_bits(4294967295.0)
        self.assertEqual(oracle_v_cvt_i32_f64(p32_minus_1), INT32_MAX)  # saturates in i32
        self.assertEqual(oracle_v_cvt_u32_f64(p32_minus_1), UINT32_MAX)  # exact in u32

        # 2^32 = 4294967296.0
        p32 = f64_to_bits(4294967296.0)
        self.assertEqual(oracle_v_cvt_i32_f64(p32), INT32_MAX)
        self.assertEqual(oracle_v_cvt_u32_f64(p32), UINT32_MAX)

        # -2^31 = -2147483648.0
        neg_p31 = f64_to_bits(-2147483648.0)
        self.assertEqual(oracle_v_cvt_i32_f64(neg_p31), i32_to_u32(INT32_MIN))
        self.assertEqual(oracle_v_cvt_u32_f64(neg_p31), 0)

        # -2^31 - 1 = -2147483649.0
        neg_p31_minus_1 = f64_to_bits(-2147483649.0)
        self.assertEqual(oracle_v_cvt_i32_f64(neg_p31_minus_1), i32_to_u32(INT32_MIN))
        self.assertEqual(oracle_v_cvt_u32_f64(neg_p31_minus_1), 0)

    def test_fractional_truncation_near_boundaries(self) -> None:
        self.assertEqual(oracle_v_cvt_i32_f64(f64_to_bits(2147483647.999999)), INT32_MAX)
        self.assertEqual(oracle_v_cvt_i32_f64(f64_to_bits(-2147483648.999999)), i32_to_u32(INT32_MIN))
        self.assertEqual(oracle_v_cvt_u32_f64(f64_to_bits(4294967295.999999)), UINT32_MAX)
        self.assertEqual(oracle_v_cvt_i32_f64(f64_to_bits(0.999999)), 0)
        self.assertEqual(oracle_v_cvt_i32_f64(f64_to_bits(-0.999999)), 0)
        self.assertEqual(oracle_v_cvt_u32_f64(f64_to_bits(0.999999)), 0)
        self.assertEqual(oracle_v_cvt_u32_f64(f64_to_bits(-0.999999)), 0)

    def test_infinities_with_modifiers(self) -> None:
        # +inf with neg modifier -> -inf
        self.assertEqual(oracle_v_cvt_i32_f64(POS_INF_64, neg_mod=True), i32_to_u32(INT32_MIN))
        self.assertEqual(oracle_v_cvt_u32_f64(POS_INF_64, neg_mod=True), 0)

        # -inf with abs modifier -> +inf
        self.assertEqual(oracle_v_cvt_i32_f64(NEG_INF_64, abs_mod=True), INT32_MAX)
        self.assertEqual(oracle_v_cvt_u32_f64(NEG_INF_64, abs_mod=True), UINT32_MAX)

    def test_f64_clamping_extremes(self) -> None:
        # v_cvt_f64_i32 with clamp
        self.assertEqual(oracle_v_cvt_f64_i32(i32_to_u32(INT32_MIN), clamp=True), POS_ZERO_64)
        self.assertEqual(oracle_v_cvt_f64_i32(i32_to_u32(-1), clamp=True), POS_ZERO_64)
        self.assertEqual(oracle_v_cvt_f64_i32(0, clamp=True), POS_ZERO_64)
        self.assertEqual(oracle_v_cvt_f64_i32(1, clamp=True), POS_ONE_64)
        self.assertEqual(oracle_v_cvt_f64_i32(INT32_MAX, clamp=True), POS_ONE_64)

        # v_cvt_f64_u32 with clamp
        self.assertEqual(oracle_v_cvt_f64_u32(0, clamp=True), POS_ZERO_64)
        self.assertEqual(oracle_v_cvt_f64_u32(1, clamp=True), POS_ONE_64)
        self.assertEqual(oracle_v_cvt_f64_u32(UINT32_MAX, clamp=True), POS_ONE_64)


class TestNegativeControls(unittest.TestCase):
    """Unit tests for negative controls, syntax rejection, and invalid AST inputs."""

    def test_invalid_register_indices(self) -> None:
        with self.assertRaises(ValueError):
            Register(RegKind.VGPR, -1, count=1)
        with self.assertRaises(ValueError):
            Register(RegKind.VGPR, 256, count=1)
        with self.assertRaises(ValueError):
            Register(RegKind.SGPR, -5, count=2)
        with self.assertRaises(ValueError):
            Register(RegKind.VGPR, 255, count=2)

    def test_invalid_register_counts(self) -> None:
        with self.assertRaises(ValueError):
            Register(RegKind.VGPR, 0, count=0)
        with self.assertRaises(ValueError):
            Register(RegKind.VGPR, 0, count=3)
        with self.assertRaises(ValueError):
            Register(RegKind.SGPR, 4, count=4)

    def test_invalid_destination_kind(self) -> None:
        # VALU destination cannot be SGPR
        with self.assertRaises(TypeError):
            VCvtInstruction(
                opcode=Opcode.V_CVT_F64_I32,
                dst=Register(RegKind.SGPR, 0, count=2),
                src0=VOP1Operand(Register(RegKind.VGPR, 2, count=1)),
            )

    def test_invalid_destination_count(self) -> None:
        # v_cvt_f64_i32 requires 64-bit dst (count=2)
        with self.assertRaises(ValueError):
            VCvtInstruction(
                opcode=Opcode.V_CVT_F64_I32,
                dst=Register(RegKind.VGPR, 0, count=1),
                src0=VOP1Operand(Register(RegKind.VGPR, 2, count=1)),
            )
        # v_cvt_i32_f64 requires 32-bit dst (count=1)
        with self.assertRaises(ValueError):
            VCvtInstruction(
                opcode=Opcode.V_CVT_I32_F64,
                dst=Register(RegKind.VGPR, 0, count=2),
                src0=VOP1Operand(Register(RegKind.VGPR, 2, count=2)),
            )

    def test_invalid_source_count(self) -> None:
        # v_cvt_f64_i32 requires 32-bit src0 (count=1)
        with self.assertRaises(ValueError):
            VCvtInstruction(
                opcode=Opcode.V_CVT_F64_I32,
                dst=Register(RegKind.VGPR, 0, count=2),
                src0=VOP1Operand(Register(RegKind.VGPR, 2, count=2)),
            )
        # v_cvt_i32_f64 requires 64-bit src0 (count=2)
        with self.assertRaises(ValueError):
            VCvtInstruction(
                opcode=Opcode.V_CVT_I32_F64,
                dst=Register(RegKind.VGPR, 0, count=1),
                src0=VOP1Operand(Register(RegKind.VGPR, 2, count=1)),
            )

    def test_invalid_modifiers(self) -> None:
        # Clamp invalid on integer destination
        with self.assertRaises(ValueError):
            VCvtInstruction(
                opcode=Opcode.V_CVT_I32_F64,
                dst=Register(RegKind.VGPR, 0, count=1),
                src0=VOP1Operand(Register(RegKind.VGPR, 2, count=2)),
                clamp=True,
            )
        # Float modifiers invalid on integer sources
        with self.assertRaises(ValueError):
            VCvtInstruction(
                opcode=Opcode.V_CVT_F64_I32,
                dst=Register(RegKind.VGPR, 0, count=2),
                src0=VOP1Operand(Register(RegKind.VGPR, 2, count=1), abs_mod=True),
            )

    def test_scalar_interpreter_out_of_bounds_access(self) -> None:
        state = ScalarMachineState()
        with self.assertRaises(IndexError):
            state.read_reg(RegKind.VGPR, 256, count=1)
        with self.assertRaises(IndexError):
            state.write_reg(RegKind.SGPR, 255, count=2, value=0)

    def test_simd_interpreter_invalid_wave_size(self) -> None:
        with self.assertRaises(ValueError):
            SIMDState(num_lanes=16)
        with self.assertRaises(ValueError):
            SIMDState(num_lanes=128)

    def test_simd_interpreter_lane_bounds(self) -> None:
        state = SIMDState(num_lanes=32)
        with self.assertRaises(IndexError):
            state.read_lane_reg(32, RegKind.VGPR, 0, count=1)
        with self.assertRaises(IndexError):
            state.write_lane_reg(-1, RegKind.VGPR, 0, count=1, value=0)


class TestMutationDetection(unittest.TestCase):
    """Mutation tests: verify that intentional sabotages/bugs in the conversion logic are detected."""

    def test_mutation_signed_unsigned_confusion(self) -> None:
        # Sabotage: treating negative signed int as large unsigned int in v_cvt_f64_i32
        def mutated_v_cvt_f64_i32_unsigned_bug(src: int) -> int:
            return f64_to_bits(float(src & MASK32))

        # Converting -5 must produce -5.0, not +4294967291.0
        with self.assertRaises(AssertionError):
            self.assertEqual(
                mutated_v_cvt_f64_i32_unsigned_bug(i32_to_u32(-5)),
                oracle_v_cvt_f64_i32(i32_to_u32(-5)),
            )

    def test_mutation_saturation_wrap_around_bug(self) -> None:
        # Sabotage: integer overflow wraps around instead of saturating to INT32_MAX
        def mutated_v_cvt_i32_f64_wrap_bug(src_bits: int) -> int:
            val = bits_to_f64(src_bits)
            return int(val) & MASK32

        # 3e9 exceeds INT32_MAX; must saturate to INT32_MAX (0x7FFFFFFF), not wrap to negative
        large_bits = f64_to_bits(3e9)
        with self.assertRaises(AssertionError):
            self.assertEqual(
                mutated_v_cvt_i32_f64_wrap_bug(large_bits),
                oracle_v_cvt_i32_f64(large_bits),
            )

    def test_mutation_nan_nonzero_bug(self) -> None:
        # Sabotage: NaN returns non-zero default
        def mutated_v_cvt_i32_f64_nan_bug(src_bits: int) -> int:
            if is_nan_f64(src_bits):
                return 0x7FFF_FFFF
            return oracle_v_cvt_i32_f64(src_bits)

        with self.assertRaises(AssertionError):
            self.assertEqual(
                mutated_v_cvt_i32_f64_nan_bug(CANONICAL_QNAN_64),
                oracle_v_cvt_i32_f64(CANONICAL_QNAN_64),
            )

    def test_mutation_negative_unsigned_clamp_bug(self) -> None:
        # Sabotage: negative float in u32 conversion casts to signed int bits instead of 0
        def mutated_v_cvt_u32_f64_neg_bug(src_bits: int) -> int:
            val = bits_to_f64(src_bits)
            return int(val) & MASK32  # e.g. -1 becomes 0xFFFFFFFF

        neg_bits = f64_to_bits(-1.0)
        with self.assertRaises(AssertionError):
            self.assertEqual(
                mutated_v_cvt_u32_f64_neg_bug(neg_bits),
                oracle_v_cvt_u32_f64(neg_bits),
            )

    def test_mutation_clamp_negative_zero_bug(self) -> None:
        # Sabotage: clamping -0.0 retains NEG_ZERO_64 instead of POS_ZERO_64
        def mutated_apply_f64_clamp_neg_zero(bits: int) -> int:
            val = bits_to_f64(bits)
            if val < 0.0:
                return POS_ZERO_64
            if val > 1.0:
                return POS_ONE_64
            return bits  # returns NEG_ZERO_64 unchanged

        with self.assertRaises(AssertionError):
            self.assertEqual(
                mutated_apply_f64_clamp_neg_zero(NEG_ZERO_64),
                apply_f64_clamp(NEG_ZERO_64),
            )

    def test_mutation_exec_mask_leak(self) -> None:
        # Sabotage: inactive lanes write to destination VGPR
        simd = SIMDInterpreter()
        state = SIMDState(num_lanes=32, exec_mask=0x0000_0000)  # all inactive
        canary = 0xABCD_EF01
        for lane in range(32):
            state.write_lane_reg(lane, RegKind.VGPR, 0, 1, canary)

        inst = VCvtInstruction(
            opcode=Opcode.V_CVT_I32_F64,
            dst=Register(RegKind.VGPR, 0, count=1),
            src0=VOP1Operand(Literal64.from_float(50.0)),
        )
        simd.step(inst, state)
        # Proper interpreter preserves canary in all lanes
        for lane in range(32):
            self.assertEqual(state.read_lane_reg(lane, RegKind.VGPR, 0, count=1), canary)

        # Mutated write unconditionally overwrites inactive lane
        state.write_lane_reg(0, RegKind.VGPR, 0, 1, 50)
        with self.assertRaises(AssertionError):
            self.assertEqual(state.read_lane_reg(0, RegKind.VGPR, 0, count=1), canary)


if __name__ == "__main__":
    unittest.main()
