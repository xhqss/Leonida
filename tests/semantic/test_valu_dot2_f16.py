"""Semantic reference oracle and architectural model for RDNA2 v_dot2_f16 instructions.

Opcode targets:
  - v_dot2_f16_f16:    D.f16 = S0.f16[0] * S1.f16[0] + S0.f16[1] * S1.f16[1]
  - v_dot2acc_f16_f16: D.f16 = S0.f16[0] * S1.f16[0] + S0.f16[1] * S1.f16[1] + S2.f16

AMD RDNA2 VALU Instruction Set Architecture (GFX10):
  The v_dot2_f16_f16 instruction computes a 2-element half-precision floating-point dot
  product from packed 32-bit registers S0 and S1. Each 32-bit source holds two IEEE 754
  binary16 values: low halfword [15:0] and high halfword [31:16].
  The accumulating variant v_dot2acc_f16_f16 adds an accumulator S2 into the scalar result.
  Result is written to destination VGPR D[15:0] (upper bits zeroed).

Modifiers:
  - clamp: Saturates final result to [0.0, 1.0]. Negative values, -0.0, and NaNs clamp to +0.0;
    values >= 1.0 and +Infinity clamp to 1.0.
  - op_sel / op_sel_hi: Controls selection of low/high halfwords from S0 and S1 for the two terms.
  - neg / abs modifiers: Applied to source halfwords prior to arithmetic.

Standard library Python only: Python 3.9+ compatible, zero game data, zero external dependencies.
"""
from __future__ import annotations

import math
import struct
import unittest
from dataclasses import dataclass, field
from enum import Enum, auto
from fractions import Fraction
from typing import List, Optional, Tuple, Union

# ==============================================================================
# Architectural Bit Masks and IEEE 754 binary16 Encodings
# ==============================================================================

MASK16: int = 0xFFFF
MASK32: int = 0xFFFF_FFFF
MASK64: int = 0xFFFF_FFFF_FFFF_FFFF

SIGN_MASK: int = 0x8000
EXP_MASK: int = 0x7C00
FRAC_MASK: int = 0x03FF
QNAN_BIT: int = 0x0200

# Canonical IEEE 754 binary16 values
POS_ZERO: int = 0x0000
NEG_ZERO: int = 0x8000
POS_INF: int = 0x7C00
NEG_INF: int = 0xFC00
CANONICAL_QNAN: int = 0x7E00
CANONICAL_SNAN: int = 0x7D00

# Boundary limits
NORM_MAX: int = 0x7BFF       # +65504.0
NEG_NORM_MAX: int = 0xFBFF   # -65504.0
NORM_MIN: int = 0x0400       # +2^-14 (~6.10352e-5)
NEG_NORM_MIN: int = 0x8400   # -2^-14
SUB_MIN: int = 0x0001        # +2^-24 (~5.96046e-8)
NEG_SUB_MIN: int = 0x8001    # -2^-24
SUB_MAX: int = 0x03FF        # +1023 * 2^-24
NEG_SUB_MAX: int = 0x83FF    # -1023 * 2^-24

# Useful unit constants
ONE_F16: int = 0x3C00        # +1.0
NEG_ONE_F16: int = 0xBC00    # -1.0
HALF_F16: int = 0x3800       # +0.5
NEG_HALF_F16: int = 0xB800   # -0.5
TWO_F16: int = 0x4000        # +2.0
NEG_TWO_F16: int = 0xC000    # -2.0
THREE_F16: int = 0x4200      # +3.0
FOUR_F16: int = 0x4400       # +4.0
FIVE_F16: int = 0x4500       # +5.0


# ==============================================================================
# Helper Conversions, Bit Manipulation, and IEEE 754 Predicates
# ==============================================================================

def f16_to_bits(val: float) -> int:
    """Convert an IEEE 754 Python float to 16-bit binary16 integer bits."""
    if math.isnan(val):
        sign = 0x8000 if math.copysign(1.0, val) < 0 else 0x0000
        return sign | CANONICAL_QNAN
    if math.isinf(val):
        return POS_INF if val > 0 else NEG_INF
    if val >= 65520.0:
        return POS_INF
    if val <= -65520.0:
        return NEG_INF
    return struct.unpack("<H", struct.pack("<e", val))[0]


def bits_to_f16(bits: int) -> float:
    """Convert a 16-bit binary16 integer into a Python float."""
    return struct.unpack("<e", struct.pack("<H", bits & MASK16))[0]


def pack_f16x2(lo: int, hi: int) -> int:
    """Pack two 16-bit halfwords into a 32-bit dword (lo, hi)."""
    return ((hi & MASK16) << 16) | (lo & MASK16)


def unpack_f16x2(dword: int) -> Tuple[int, int]:
    """Unpack a 32-bit dword into two 16-bit halfwords (lo, hi)."""
    dword &= MASK32
    return dword & MASK16, (dword >> 16) & MASK16


def split_f16_bits(bits: int) -> Tuple[int, int, int]:
    """Split 16-bit binary16 into (sign, exponent, fraction) components."""
    sign = (bits & SIGN_MASK) >> 15
    exp = (bits & EXP_MASK) >> 10
    frac = bits & FRAC_MASK
    return sign, exp, frac


def is_nan(bits: int) -> bool:
    """Return True if 16-bit word represents an IEEE 754 NaN."""
    return ((bits & EXP_MASK) == EXP_MASK) and ((bits & FRAC_MASK) != 0)


def is_qnan(bits: int) -> bool:
    """Return True if 16-bit word represents a Quiet NaN."""
    return is_nan(bits) and bool(bits & QNAN_BIT)


def is_snan(bits: int) -> bool:
    """Return True if 16-bit word represents a Signaling NaN."""
    return is_nan(bits) and not bool(bits & QNAN_BIT)


def is_inf(bits: int) -> bool:
    """Return True if 16-bit word represents positive or negative infinity."""
    return ((bits & EXP_MASK) == EXP_MASK) and ((bits & FRAC_MASK) == 0)


def is_subnormal(bits: int) -> bool:
    """Return True if 16-bit word represents a subnormal (denormal) float."""
    return ((bits & EXP_MASK) == 0) and ((bits & FRAC_MASK) != 0)


def is_zero(bits: int) -> bool:
    """Return True if 16-bit word represents positive or negative zero."""
    return (bits & ~SIGN_MASK) == 0


def apply_f16_modifiers(bits: int, abs_mod: bool, neg_mod: bool) -> int:
    """Apply input modifiers: abs clears sign bit; neg toggles sign bit."""
    res = bits & MASK16
    if abs_mod:
        res &= ~SIGN_MASK
    if neg_mod:
        res ^= SIGN_MASK
    return res


def clamp_f16_bits(bits: int) -> int:
    """Apply RDNA2 VALU clamp modifier saturating to [0.0, 1.0].
    
    In RDNA2 hardware:
      - Any NaN clamps to +0.0
      - Values < 0.0 or -0.0 clamp to +0.0
      - Values > 1.0 or +Infinity clamp to 1.0 (ONE_F16)
    """
    if is_nan(bits):
        return POS_ZERO
    if is_zero(bits):
        return POS_ZERO
    if bool(bits & SIGN_MASK):
        return POS_ZERO
    if bits == POS_INF:
        return ONE_F16
    val = bits_to_f16(bits)
    if val > 1.0:
        return ONE_F16
    return bits


def mul_f16(a: int, b: int) -> int:
    """Multiply two 16-bit IEEE 754 half-precision numbers."""
    # 1. NaN propagation
    if is_nan(a) or is_nan(b):
        nan_bits = a if is_nan(a) else b
        return nan_bits | QNAN_BIT

    # 2. 0 * Inf is invalid -> Canonical Quiet NaN
    if (is_zero(a) and is_inf(b)) or (is_inf(a) and is_zero(b)):
        return CANONICAL_QNAN

    # 3. Infinity multiplication
    sign = (a ^ b) & SIGN_MASK
    if is_inf(a) or is_inf(b):
        return sign | POS_INF

    # 4. Zero multiplication
    if is_zero(a) or is_zero(b):
        return sign | POS_ZERO

    # 5. Finite arithmetic
    f_a = bits_to_f16(a)
    f_b = bits_to_f16(b)
    f_prod = f_a * f_b
    res = f16_to_bits(f_prod)
    if f_prod == 0.0:
        res = sign | POS_ZERO
    return res


def add_f16(a: int, b: int) -> int:
    """Add two 16-bit IEEE 754 half-precision numbers with round-to-nearest-even."""
    # 1. NaN propagation
    if is_nan(a) or is_nan(b):
        nan_bits = a if is_nan(a) else b
        return nan_bits | QNAN_BIT

    # 2. Infinity addition
    a_inf = is_inf(a)
    b_inf = is_inf(b)
    if a_inf and b_inf:
        if (a & SIGN_MASK) != (b & SIGN_MASK):
            # +inf + -inf -> Invalid operation -> Canonical Quiet NaN
            return CANONICAL_QNAN
        return a
    if a_inf:
        return a
    if b_inf:
        return b

    # 3. Signed zero addition
    if is_zero(a) and is_zero(b):
        if (a == NEG_ZERO) and (b == NEG_ZERO):
            return NEG_ZERO
        return POS_ZERO

    # 4. Finite addition
    f_a = bits_to_f16(a)
    f_b = bits_to_f16(b)
    f_res = f_a + f_b
    res = f16_to_bits(f_res)
    if f_res == 0.0 and not ((a == NEG_ZERO) and (b == NEG_ZERO)):
        res = POS_ZERO
    return res


# ==============================================================================
# Reference Mathematical Oracles
# ==============================================================================

def oracle_v_dot2_f16(
    src0_bits: int,
    src1_bits: int,
    abs0: Tuple[bool, bool] = (False, False),
    neg0: Tuple[bool, bool] = (False, False),
    abs1: Tuple[bool, bool] = (False, False),
    neg1: Tuple[bool, bool] = (False, False),
    op_sel: Tuple[int, int] = (0, 0),
    op_sel_hi: Tuple[int, int] = (1, 1),
    clamp: bool = False,
) -> int:
    """Reference mathematical oracle for RDNA2 v_dot2_f16_f16.

    Computes: D = S0[op_sel[0]] * S1[op_sel[1]] + S0[op_sel_hi[0]] * S1[op_sel_hi[1]]
    Returns 16-bit binary16 integer bits.
    """
    s0_lo, s0_hi = unpack_f16x2(src0_bits)
    s1_lo, s1_hi = unpack_f16x2(src1_bits)
    s0_halves = (s0_lo, s0_hi)
    s1_halves = (s1_lo, s1_hi)

    h0_0 = apply_f16_modifiers(s0_halves[op_sel[0] & 1], abs0[0], neg0[0])
    h1_0 = apply_f16_modifiers(s1_halves[op_sel[1] & 1], abs1[0], neg1[0])
    h0_1 = apply_f16_modifiers(s0_halves[op_sel_hi[0] & 1], abs0[1], neg0[1])
    h1_1 = apply_f16_modifiers(s1_halves[op_sel_hi[1] & 1], abs1[1], neg1[1])

    prod0 = mul_f16(h0_0, h1_0)
    prod1 = mul_f16(h0_1, h1_1)
    res = add_f16(prod0, prod1)

    if clamp:
        res = clamp_f16_bits(res)
    return res


def oracle_v_dot2acc_f16(
    src0_bits: int,
    src1_bits: int,
    src2_acc_bits: int,
    abs0: Tuple[bool, bool] = (False, False),
    neg0: Tuple[bool, bool] = (False, False),
    abs1: Tuple[bool, bool] = (False, False),
    neg1: Tuple[bool, bool] = (False, False),
    abs2: bool = False,
    neg2: bool = False,
    op_sel: Tuple[int, int] = (0, 0),
    op_sel_hi: Tuple[int, int] = (1, 1),
    clamp: bool = False,
) -> int:
    """Reference mathematical oracle for RDNA2 v_dot2acc_f16_f16.

    Computes: D = S0[op_sel[0]] * S1[op_sel[1]] + S0[op_sel_hi[0]] * S1[op_sel_hi[1]] + S2
    Returns 16-bit binary16 integer bits.
    """
    dot_val = oracle_v_dot2_f16(
        src0_bits=src0_bits,
        src1_bits=src1_bits,
        abs0=abs0,
        neg0=neg0,
        abs1=abs1,
        neg1=neg1,
        op_sel=op_sel,
        op_sel_hi=op_sel_hi,
        clamp=False,
    )
    acc = apply_f16_modifiers(src2_acc_bits & MASK16, abs2, neg2)
    res = add_f16(dot_val, acc)

    if clamp:
        res = clamp_f16_bits(res)
    return res


# ==============================================================================
# Symbolic AST Opcode Definitions and Typed Expressions
# ==============================================================================

class RegType(Enum):
    VGPR = auto()
    SGPR = auto()


@dataclass(frozen=True)
class Reg:
    """Architectural 32-bit register (VGPR or SGPR)."""
    index: int
    reg_type: RegType

    def __post_init__(self) -> None:
        if not (0 <= self.index <= 255):
            raise ValueError(f"Register index {self.index} out of valid range [0, 255]")

    @property
    def name(self) -> str:
        prefix = "v" if self.reg_type == RegType.VGPR else "s"
        return f"{prefix}{self.index}"


@dataclass(frozen=True)
class Literal32:
    """32-bit inline constant or literal packed halfword pair."""
    bits: int

    def __post_init__(self) -> None:
        if not (0 <= self.bits <= MASK32):
            raise ValueError(f"Literal32 bits 0x{self.bits:X} out of range [0, 0xFFFFFFFF]")

    @classmethod
    def from_f16x2(cls, lo: float, hi: float) -> Literal32:
        return cls(pack_f16x2(f16_to_bits(lo), f16_to_bits(hi)))

    @property
    def name(self) -> str:
        lo, hi = unpack_f16x2(self.bits)
        return f"pack_f16({bits_to_f16(lo):g}, {bits_to_f16(hi):g})"


@dataclass(frozen=True)
class VOP3Operand:
    """VOP3/VOP3P operand with sub-word modifiers (abs, neg per 16-bit half)."""
    source: Union[Reg, Literal32]
    abs_lo: bool = False
    abs_hi: bool = False
    neg_lo: bool = False
    neg_hi: bool = False

    def disassemble(self) -> str:
        s = self.source.name
        mods = []
        if self.abs_lo or self.abs_hi:
            mods.append("abs")
        if self.neg_lo or self.neg_hi:
            mods.append("neg")
        if mods:
            return f"{'/'.join(mods)}({s})"
        return s


@dataclass(frozen=True)
class VDot2F16Instruction:
    """Symbolic AST node representing AMD RDNA2 v_dot2_f16_f16 instruction."""
    dst: Reg
    src0: VOP3Operand
    src1: VOP3Operand
    clamp: bool = False
    op_sel: Tuple[int, int] = (0, 0)
    op_sel_hi: Tuple[int, int] = (1, 1)

    def __post_init__(self) -> None:
        if self.dst.reg_type != RegType.VGPR:
            raise TypeError(f"Destination of VALU instruction must be VGPR, got {self.dst.reg_type}")

    def disassemble(self) -> str:
        s = f"v_dot2_f16_f16 {self.dst.name}, {self.src0.disassemble()}, {self.src1.disassemble()}"
        if self.op_sel != (0, 0) or self.op_sel_hi != (1, 1):
            s += f" op_sel:[{self.op_sel[0]},{self.op_sel[1]},{self.op_sel_hi[0]},{self.op_sel_hi[1]}]"
        if self.clamp:
            s += " clamp"
        return s


@dataclass(frozen=True)
class VDot2AccF16Instruction:
    """Symbolic AST node representing AMD RDNA2 v_dot2acc_f16_f16 instruction."""
    dst: Reg
    src0: VOP3Operand
    src1: VOP3Operand
    src2: VOP3Operand
    clamp: bool = False
    op_sel: Tuple[int, int] = (0, 0)
    op_sel_hi: Tuple[int, int] = (1, 1)

    def __post_init__(self) -> None:
        if self.dst.reg_type != RegType.VGPR:
            raise TypeError(f"Destination of VALU instruction must be VGPR, got {self.dst.reg_type}")

    def disassemble(self) -> str:
        s = f"v_dot2acc_f16_f16 {self.dst.name}, {self.src0.disassemble()}, {self.src1.disassemble()}, {self.src2.disassemble()}"
        if self.op_sel != (0, 0) or self.op_sel_hi != (1, 1):
            s += f" op_sel:[{self.op_sel[0]},{self.op_sel[1]},{self.op_sel_hi[0]},{self.op_sel_hi[1]}]"
        if self.clamp:
            s += " clamp"
        return s


# ==============================================================================
# Deterministic Scalar Instruction Interpreter
# ==============================================================================

@dataclass
class ScalarMachineState:
    """Architectural register file for a single execution thread."""
    vgprs: List[int] = field(default_factory=lambda: [0] * 256)
    sgprs: List[int] = field(default_factory=lambda: [0] * 256)

    def get_vgpr(self, index: int) -> int:
        return self.vgprs[index] & MASK32

    def set_vgpr(self, index: int, val: int) -> None:
        self.vgprs[index] = val & MASK32

    def get_sgpr(self, index: int) -> int:
        return self.sgprs[index] & MASK32

    def set_sgpr(self, index: int, val: int) -> None:
        self.sgprs[index] = val & MASK32


class ScalarInterpreter:
    """Deterministic scalar instruction interpreter for v_dot2_f16 family."""

    def resolve_operand(self, op: VOP3Operand, state: ScalarMachineState) -> int:
        if isinstance(op.source, Reg):
            if op.source.reg_type == RegType.VGPR:
                return state.get_vgpr(op.source.index)
            elif op.source.reg_type == RegType.SGPR:
                return state.get_sgpr(op.source.index)
            else:
                raise ValueError(f"Unknown register type: {op.source.reg_type}")
        elif isinstance(op.source, Literal32):
            return op.source.bits
        else:
            raise TypeError(f"Unsupported operand source type: {type(op.source)}")

    def step(
        self,
        instr: Union[VDot2F16Instruction, VDot2AccF16Instruction],
        state: ScalarMachineState,
    ) -> int:
        # Atomic read phase
        s0_raw = self.resolve_operand(instr.src0, state)
        s1_raw = self.resolve_operand(instr.src1, state)

        # Computation phase
        if isinstance(instr, VDot2F16Instruction):
            res_bits = oracle_v_dot2_f16(
                src0_bits=s0_raw,
                src1_bits=s1_raw,
                abs0=(instr.src0.abs_lo, instr.src0.abs_hi),
                neg0=(instr.src0.neg_lo, instr.src0.neg_hi),
                abs1=(instr.src1.abs_lo, instr.src1.abs_hi),
                neg1=(instr.src1.neg_lo, instr.src1.neg_hi),
                op_sel=instr.op_sel,
                op_sel_hi=instr.op_sel_hi,
                clamp=instr.clamp,
            )
        elif isinstance(instr, VDot2AccF16Instruction):
            s2_raw = self.resolve_operand(instr.src2, state)
            res_bits = oracle_v_dot2acc_f16(
                src0_bits=s0_raw,
                src1_bits=s1_raw,
                src2_acc_bits=s2_raw,
                abs0=(instr.src0.abs_lo, instr.src0.abs_hi),
                neg0=(instr.src0.neg_lo, instr.src0.neg_hi),
                abs1=(instr.src1.abs_lo, instr.src1.abs_hi),
                neg1=(instr.src1.neg_lo, instr.src1.neg_hi),
                abs2=instr.src2.abs_lo,
                neg2=instr.src2.neg_lo,
                op_sel=instr.op_sel,
                op_sel_hi=instr.op_sel_hi,
                clamp=instr.clamp,
            )
        else:
            raise TypeError(f"Unsupported instruction type: {type(instr)}")

        # Write-back phase: write 16-bit scalar result into lower 16 bits of VGPR
        state.set_vgpr(instr.dst.index, res_bits)
        return res_bits


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

    def get_vgpr(self, lane: int, index: int) -> int:
        return self.vgprs[lane][index] & MASK32

    def set_vgpr(self, lane: int, index: int, val: int) -> None:
        self.vgprs[lane][index] = val & MASK32

    def get_sgpr(self, index: int) -> int:
        return self.sgprs[index] & MASK32

    def set_sgpr(self, index: int, val: int) -> None:
        self.sgprs[index] = val & MASK32


class SIMDInterpreter:
    """SIMD instruction interpreter handling multi-lane divergence and EXEC masking."""

    def resolve_lane_operand(self, op: VOP3Operand, state: SIMDState, lane: int) -> int:
        if isinstance(op.source, Reg):
            if op.source.reg_type == RegType.VGPR:
                return state.get_vgpr(lane, op.source.index)
            elif op.source.reg_type == RegType.SGPR:
                return state.get_sgpr(op.source.index)
            else:
                raise ValueError(f"Unknown register type: {op.source.reg_type}")
        elif isinstance(op.source, Literal32):
            return op.source.bits
        else:
            raise TypeError(f"Unsupported operand source type: {type(op.source)}")

    def step(
        self,
        instr: Union[VDot2F16Instruction, VDot2AccF16Instruction],
        state: SIMDState,
    ) -> List[Optional[int]]:
        results: List[Optional[int]] = [None] * state.num_lanes

        # Atomic read phase across all lanes
        s0_vals = [
            self.resolve_lane_operand(instr.src0, state, lane)
            for lane in range(state.num_lanes)
        ]
        s1_vals = [
            self.resolve_lane_operand(instr.src1, state, lane)
            for lane in range(state.num_lanes)
        ]
        s2_vals = None
        if isinstance(instr, VDot2AccF16Instruction):
            s2_vals = [
                self.resolve_lane_operand(instr.src2, state, lane)
                for lane in range(state.num_lanes)
            ]

        # Execution and masking phase
        for lane in range(state.num_lanes):
            if (state.exec_mask & (1 << lane)) != 0:
                if isinstance(instr, VDot2F16Instruction):
                    res = oracle_v_dot2_f16(
                        src0_bits=s0_vals[lane],
                        src1_bits=s1_vals[lane],
                        abs0=(instr.src0.abs_lo, instr.src0.abs_hi),
                        neg0=(instr.src0.neg_lo, instr.src0.neg_hi),
                        abs1=(instr.src1.abs_lo, instr.src1.abs_hi),
                        neg1=(instr.src1.neg_lo, instr.src1.neg_hi),
                        op_sel=instr.op_sel,
                        op_sel_hi=instr.op_sel_hi,
                        clamp=instr.clamp,
                    )
                elif isinstance(instr, VDot2AccF16Instruction) and s2_vals is not None:
                    res = oracle_v_dot2acc_f16(
                        src0_bits=s0_vals[lane],
                        src1_bits=s1_vals[lane],
                        src2_acc_bits=s2_vals[lane],
                        abs0=(instr.src0.abs_lo, instr.src0.abs_hi),
                        neg0=(instr.src0.neg_lo, instr.src0.neg_hi),
                        abs1=(instr.src1.abs_lo, instr.src1.abs_hi),
                        neg1=(instr.src1.neg_lo, instr.src1.neg_hi),
                        abs2=instr.src2.abs_lo,
                        neg2=instr.src2.neg_lo,
                        op_sel=instr.op_sel,
                        op_sel_hi=instr.op_sel_hi,
                        clamp=instr.clamp,
                    )
                else:
                    raise TypeError(f"Unsupported instruction type: {type(instr)}")

                state.set_vgpr(lane, instr.dst.index, res)
                results[lane] = res

        return results


# ==============================================================================
# Condition Codes, Status Flags, and Carry Generation
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


def evaluate_v_dot2_f16_flags(
    src0_bits: int,
    src1_bits: int,
    res_bits: int,
    abs0: Tuple[bool, bool] = (False, False),
    neg0: Tuple[bool, bool] = (False, False),
    abs1: Tuple[bool, bool] = (False, False),
    neg1: Tuple[bool, bool] = (False, False),
    op_sel: Tuple[int, int] = (0, 0),
    op_sel_hi: Tuple[int, int] = (1, 1),
    clamp: bool = False,
) -> FPStatusFlags:
    """Evaluate IEEE 754 architectural flags and condition codes for v_dot2_f16."""
    s0_lo, s0_hi = unpack_f16x2(src0_bits)
    s1_lo, s1_hi = unpack_f16x2(src1_bits)
    s0_halves = (s0_lo, s0_hi)
    s1_halves = (s1_lo, s1_hi)

    h0_0 = apply_f16_modifiers(s0_halves[op_sel[0] & 1], abs0[0], neg0[0])
    h1_0 = apply_f16_modifiers(s1_halves[op_sel[1] & 1], abs1[0], neg1[0])
    h0_1 = apply_f16_modifiers(s0_halves[op_sel_hi[0] & 1], abs0[1], neg0[1])
    h1_1 = apply_f16_modifiers(s1_halves[op_sel_hi[1] & 1], abs1[1], neg1[1])

    # Check for signaling NaNs
    has_snan = is_snan(h0_0) or is_snan(h1_0) or is_snan(h0_1) or is_snan(h1_1)

    # Check for 0 * inf
    has_zero_times_inf = (
        (is_zero(h0_0) and is_inf(h1_0)) or (is_inf(h0_0) and is_zero(h1_0)) or
        (is_zero(h0_1) and is_inf(h1_1)) or (is_inf(h0_1) and is_zero(h1_1))
    )

    # Check for +inf + -inf
    p0 = mul_f16(h0_0, h1_0)
    p1 = mul_f16(h0_1, h1_1)
    has_inf_cancellation = is_inf(p0) and is_inf(p1) and ((p0 & SIGN_MASK) != (p1 & SIGN_MASK))

    invalid = has_snan or has_zero_times_inf or has_inf_cancellation

    nan_flag = is_nan(res_bits)
    inf_flag = is_inf(res_bits)
    zero_flag = is_zero(res_bits)
    neg_flag = bool(res_bits & SIGN_MASK)
    denorm_flag = is_subnormal(res_bits)

    inputs_all_finite = not (is_inf(h0_0) or is_inf(h1_0) or is_inf(h0_1) or is_inf(h1_1) or
                             is_nan(h0_0) or is_nan(h1_0) or is_nan(h0_1) or is_nan(h1_1))
    overflow = inputs_all_finite and inf_flag

    underflow = False
    if denorm_flag:
        underflow = True

    inexact = False
    if overflow:
        inexact = True
    elif inputs_all_finite and not (nan_flag or inf_flag):
        try:
            f0_0 = Fraction(bits_to_f16(h0_0))
            f1_0 = Fraction(bits_to_f16(h1_0))
            f0_1 = Fraction(bits_to_f16(h0_1))
            f1_1 = Fraction(bits_to_f16(h1_1))
            exact_math = (f0_0 * f1_0) + (f0_1 * f1_1)
            res_frac = Fraction(bits_to_f16(res_bits))
            if not clamp:
                if exact_math != res_frac:
                    inexact = True
            else:
                clamped_f = min(1.0, max(0.0, float(exact_math)))
                if Fraction(clamped_f) != res_frac:
                    inexact = True
        except (ValueError, OverflowError):
            pass

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


def evaluate_v_dot2acc_f16_flags(
    src0_bits: int,
    src1_bits: int,
    src2_acc_bits: int,
    res_bits: int,
    abs0: Tuple[bool, bool] = (False, False),
    neg0: Tuple[bool, bool] = (False, False),
    abs1: Tuple[bool, bool] = (False, False),
    neg1: Tuple[bool, bool] = (False, False),
    abs2: bool = False,
    neg2: bool = False,
    op_sel: Tuple[int, int] = (0, 0),
    op_sel_hi: Tuple[int, int] = (1, 1),
    clamp: bool = False,
) -> FPStatusFlags:
    """Evaluate IEEE 754 architectural flags and condition codes for v_dot2acc_f16."""
    acc = apply_f16_modifiers(src2_acc_bits & MASK16, abs2, neg2)
    base_flags = evaluate_v_dot2_f16_flags(
        src0_bits=src0_bits,
        src1_bits=src1_bits,
        res_bits=res_bits,
        abs0=abs0,
        neg0=neg0,
        abs1=abs1,
        neg1=neg1,
        op_sel=op_sel,
        op_sel_hi=op_sel_hi,
        clamp=clamp,
    )

    invalid = base_flags.invalid or is_snan(acc)
    # Check if dot sum and acc produce +inf + -inf
    dot_val = oracle_v_dot2_f16(src0_bits, src1_bits, abs0, neg0, abs1, neg1, op_sel, op_sel_hi)
    if is_inf(dot_val) and is_inf(acc) and ((dot_val & SIGN_MASK) != (acc & SIGN_MASK)):
        invalid = True

    nan_flag = is_nan(res_bits)
    inf_flag = is_inf(res_bits)
    zero_flag = is_zero(res_bits)
    neg_flag = bool(res_bits & SIGN_MASK)
    denorm_flag = is_subnormal(res_bits)

    return FPStatusFlags(
        invalid=invalid,
        overflow=base_flags.overflow,
        underflow=base_flags.underflow,
        inexact=base_flags.inexact,
        zero=zero_flag,
        negative=neg_flag,
        denormal=denorm_flag,
        infinite=inf_flag,
        nan=nan_flag,
    )


def compute_f16_mantissa_carry(s0_bits: int, s1_bits: int) -> Tuple[int, int]:
    """Demonstrate IEEE 754 11-bit mantissa carry generation during addition."""
    _, exp0, frac0 = split_f16_bits(s0_bits)
    _, exp1, frac1 = split_f16_bits(s1_bits)
    m0 = (1 << 10) | frac0 if exp0 != 0 else frac0
    m1 = (1 << 10) | frac1 if exp1 != 0 else frac1
    if exp0 > exp1:
        m1 >>= (exp0 - exp1)
    elif exp1 > exp0:
        m0 >>= (exp1 - exp0)
    mant_sum = m0 + m1
    carry_out = 1 if mant_sum >= (1 << 11) else 0
    return mant_sum, carry_out


# ==============================================================================
# Unit Tests: Architectural Model and Reference Oracle
# ==============================================================================

class TestDot2F16ModelAndOracle(unittest.TestCase):
    """Unit tests for binary16 architectural model and reference mathematical oracle."""

    def test_f16_bit_roundtrip(self) -> None:
        values = [0.0, -0.0, 1.0, -1.0, 0.5, -2.5, 65504.0, -65504.0]
        for v in values:
            with self.subTest(v=v):
                bits = f16_to_bits(v)
                recovered = bits_to_f16(bits)
                self.assertEqual(v, recovered)
                self.assertEqual(math.copysign(1.0, v), math.copysign(1.0, recovered))

    def test_pack_unpack_f16x2(self) -> None:
        dword = pack_f16x2(ONE_F16, TWO_F16)
        lo, hi = unpack_f16x2(dword)
        self.assertEqual(lo, ONE_F16)
        self.assertEqual(hi, TWO_F16)
        self.assertEqual(dword, 0x4000_3C00)

    def test_classification_predicates(self) -> None:
        self.assertTrue(is_zero(POS_ZERO))
        self.assertTrue(is_zero(NEG_ZERO))
        self.assertFalse(is_zero(ONE_F16))

        self.assertTrue(is_inf(POS_INF))
        self.assertTrue(is_inf(NEG_INF))
        self.assertFalse(is_inf(POS_ZERO))

        self.assertTrue(is_nan(CANONICAL_QNAN))
        self.assertTrue(is_qnan(CANONICAL_QNAN))
        self.assertFalse(is_snan(CANONICAL_QNAN))

        self.assertTrue(is_nan(CANONICAL_SNAN))
        self.assertTrue(is_snan(CANONICAL_SNAN))
        self.assertFalse(is_qnan(CANONICAL_SNAN))

        self.assertTrue(is_subnormal(SUB_MIN))
        self.assertTrue(is_subnormal(SUB_MAX))
        self.assertFalse(is_subnormal(NORM_MIN))
        self.assertFalse(is_subnormal(POS_ZERO))

    def test_exact_finite_dot2(self) -> None:
        # [1.0, 2.0] . [3.0, 4.0] = 1*3 + 2*4 = 11.0
        s0 = pack_f16x2(ONE_F16, TWO_F16)
        s1 = pack_f16x2(THREE_F16, FOUR_F16)
        res = oracle_v_dot2_f16(s0, s1)
        self.assertEqual(bits_to_f16(res), 11.0)

        # [2.0, -1.0] . [1.5, 3.0] = 3.0 - 3.0 = 0.0
        s0_b = pack_f16x2(TWO_F16, NEG_ONE_F16)
        s1_b = pack_f16x2(f16_to_bits(1.5), THREE_F16)
        res_b = oracle_v_dot2_f16(s0_b, s1_b)
        self.assertEqual(res_b, POS_ZERO)

    def test_exact_finite_dot2acc(self) -> None:
        # [1.0, 2.0] . [3.0, 4.0] + 5.0 = 11.0 + 5.0 = 16.0
        s0 = pack_f16x2(ONE_F16, TWO_F16)
        s1 = pack_f16x2(THREE_F16, FOUR_F16)
        res = oracle_v_dot2acc_f16(s0, s1, FIVE_F16)
        self.assertEqual(bits_to_f16(res), 16.0)

    def test_signed_zero_rules(self) -> None:
        # (+0) * (+1) + (+0) * (+1) = +0
        s0 = pack_f16x2(POS_ZERO, POS_ZERO)
        s1 = pack_f16x2(ONE_F16, ONE_F16)
        self.assertEqual(oracle_v_dot2_f16(s0, s1), POS_ZERO)

        # (-0) * (+1) + (-0) * (+1) = -0
        s0_neg = pack_f16x2(NEG_ZERO, NEG_ZERO)
        self.assertEqual(oracle_v_dot2_f16(s0_neg, s1), NEG_ZERO)

    def test_input_modifiers(self) -> None:
        s0 = pack_f16x2(NEG_ONE_F16, NEG_TWO_F16)
        s1 = pack_f16x2(THREE_F16, FOUR_F16)
        # abs on s0: |-1|*3 + |-2|*4 = 3 + 8 = 11.0
        res = oracle_v_dot2_f16(s0, s1, abs0=(True, True))
        self.assertEqual(bits_to_f16(res), 11.0)

        # neg on s1 term0: -(-1*3) + (-2*4) = 3 - 8 = -5.0
        res2 = oracle_v_dot2_f16(s0, s1, neg0=(True, False))
        self.assertEqual(bits_to_f16(res2), -5.0)

    def test_op_sel_permutation(self) -> None:
        # s0: lo=1.0, hi=2.0; s1: lo=3.0, hi=4.0
        s0 = pack_f16x2(ONE_F16, TWO_F16)
        s1 = pack_f16x2(THREE_F16, FOUR_F16)
        # Swap terms: op_sel=(1, 0) -> s0[1]*s1[0] = 2*3 = 6; op_sel_hi=(0, 1) -> s0[0]*s1[1] = 1*4 = 4; total 10.0
        res = oracle_v_dot2_f16(s0, s1, op_sel=(1, 0), op_sel_hi=(0, 1))
        self.assertEqual(bits_to_f16(res), 10.0)

    def test_clamp_modifier(self) -> None:
        s0 = pack_f16x2(TWO_F16, TWO_F16)
        s1 = pack_f16x2(THREE_F16, FOUR_F16)
        # Result = 14.0, clamped -> 1.0
        res_high = oracle_v_dot2_f16(s0, s1, clamp=True)
        self.assertEqual(res_high, ONE_F16)

        # Result = -11.0, clamped -> +0.0
        s0_neg = pack_f16x2(NEG_ONE_F16, NEG_TWO_F16)
        res_low = oracle_v_dot2_f16(s0_neg, s1, clamp=True)
        self.assertEqual(res_low, POS_ZERO)

        # NaN clamped -> +0.0
        res_nan = oracle_v_dot2_f16(pack_f16x2(CANONICAL_QNAN, POS_ZERO), s1, clamp=True)
        self.assertEqual(res_nan, POS_ZERO)

    def test_nan_handling_and_propagation(self) -> None:
        s0 = pack_f16x2(CANONICAL_SNAN, ONE_F16)
        s1 = pack_f16x2(ONE_F16, ONE_F16)
        res = oracle_v_dot2_f16(s0, s1)
        self.assertTrue(is_nan(res))
        self.assertTrue(is_qnan(res))

    def test_infinity_handling(self) -> None:
        # +inf * 0 -> Invalid -> Quiet NaN
        s0 = pack_f16x2(POS_INF, ONE_F16)
        s1 = pack_f16x2(POS_ZERO, ONE_F16)
        res = oracle_v_dot2_f16(s0, s1)
        self.assertTrue(is_nan(res))


class TestASTOpcodeDefinitions(unittest.TestCase):
    """Unit tests for symbolic AST opcode definitions and typed expressions."""

    def test_reg_attributes_and_bounds(self) -> None:
        v0 = Reg(0, RegType.VGPR)
        s5 = Reg(5, RegType.SGPR)
        self.assertEqual(v0.name, "v0")
        self.assertEqual(s5.name, "s5")
        with self.assertRaises(ValueError):
            Reg(256, RegType.VGPR)
        with self.assertRaises(ValueError):
            Reg(-1, RegType.SGPR)

    def test_literal32_representation(self) -> None:
        lit = Literal32.from_f16x2(1.0, 2.0)
        self.assertEqual(lit.bits, 0x4000_3C00)
        self.assertIn("1", lit.name)
        self.assertIn("2", lit.name)
        with self.assertRaises(ValueError):
            Literal32(0x1_0000_0000)

    def test_vop3_operand_disassembly(self) -> None:
        op_plain = VOP3Operand(Reg(2, RegType.VGPR))
        self.assertEqual(op_plain.disassemble(), "v2")
        op_neg = VOP3Operand(Reg(4, RegType.SGPR), neg_lo=True)
        self.assertEqual(op_neg.disassemble(), "neg(s4)")
        op_abs_neg = VOP3Operand(Reg(8, RegType.VGPR), abs_lo=True, neg_hi=True)
        self.assertEqual(op_abs_neg.disassemble(), "abs/neg(v8)")

    def test_v_dot2_f16_disassembly(self) -> None:
        instr = VDot2F16Instruction(
            dst=Reg(0, RegType.VGPR),
            src0=VOP3Operand(Reg(1, RegType.VGPR)),
            src1=VOP3Operand(Reg(2, RegType.SGPR), abs_lo=True),
            clamp=True,
            op_sel=(1, 0),
            op_sel_hi=(0, 1),
        )
        expected = "v_dot2_f16_f16 v0, v1, abs(s2) op_sel:[1,0,0,1] clamp"
        self.assertEqual(instr.disassemble(), expected)

    def test_v_dot2acc_f16_disassembly(self) -> None:
        instr = VDot2AccF16Instruction(
            dst=Reg(3, RegType.VGPR),
            src0=VOP3Operand(Reg(4, RegType.VGPR)),
            src1=VOP3Operand(Reg(5, RegType.VGPR)),
            src2=VOP3Operand(Reg(6, RegType.VGPR)),
        )
        self.assertEqual(instr.disassemble(), "v_dot2acc_f16_f16 v3, v4, v5, v6")

    def test_ast_structural_equality(self) -> None:
        i1 = VDot2F16Instruction(
            dst=Reg(0, RegType.VGPR),
            src0=VOP3Operand(Reg(1, RegType.VGPR)),
            src1=VOP3Operand(Literal32.from_f16x2(1.0, 1.0)),
        )
        i2 = VDot2F16Instruction(
            dst=Reg(0, RegType.VGPR),
            src0=VOP3Operand(Reg(1, RegType.VGPR)),
            src1=VOP3Operand(Literal32.from_f16x2(1.0, 1.0)),
        )
        self.assertEqual(i1, i2)

    def test_invalid_destination_rejection(self) -> None:
        with self.assertRaises(TypeError):
            VDot2F16Instruction(
                dst=Reg(0, RegType.SGPR),
                src0=VOP3Operand(Reg(1, RegType.VGPR)),
                src1=VOP3Operand(Reg(2, RegType.VGPR)),
            )
        with self.assertRaises(TypeError):
            VDot2AccF16Instruction(
                dst=Reg(0, RegType.SGPR),
                src0=VOP3Operand(Reg(1, RegType.VGPR)),
                src1=VOP3Operand(Reg(2, RegType.VGPR)),
                src2=VOP3Operand(Reg(3, RegType.VGPR)),
            )


class TestScalarInterpreter(unittest.TestCase):
    """Unit tests for the deterministic scalar interpreter."""

    def setUp(self) -> None:
        self.interp = ScalarInterpreter()
        self.state = ScalarMachineState()

    def test_scalar_vgpr_vgpr_dot2(self) -> None:
        self.state.set_vgpr(1, pack_f16x2(ONE_F16, TWO_F16))
        self.state.set_vgpr(2, pack_f16x2(THREE_F16, FOUR_F16))
        instr = VDot2F16Instruction(
            dst=Reg(0, RegType.VGPR),
            src0=VOP3Operand(Reg(1, RegType.VGPR)),
            src1=VOP3Operand(Reg(2, RegType.VGPR)),
        )
        res = self.interp.step(instr, self.state)
        self.assertEqual(bits_to_f16(res), 11.0)
        self.assertEqual(self.state.get_vgpr(0), res)

    def test_scalar_vgpr_sgpr_dot2(self) -> None:
        self.state.set_vgpr(4, pack_f16x2(TWO_F16, THREE_F16))
        self.state.set_sgpr(8, pack_f16x2(TWO_F16, HALF_F16))
        instr = VDot2F16Instruction(
            dst=Reg(5, RegType.VGPR),
            src0=VOP3Operand(Reg(4, RegType.VGPR)),
            src1=VOP3Operand(Reg(8, RegType.SGPR)),
        )
        # 2*2 + 3*0.5 = 4 + 1.5 = 5.5
        res = self.interp.step(instr, self.state)
        self.assertEqual(bits_to_f16(res), 5.5)
        self.assertEqual(self.state.get_vgpr(5), res)

    def test_scalar_vgpr_literal_dot2(self) -> None:
        self.state.set_vgpr(10, pack_f16x2(ONE_F16, ONE_F16))
        instr = VDot2F16Instruction(
            dst=Reg(11, RegType.VGPR),
            src0=VOP3Operand(Reg(10, RegType.VGPR)),
            src1=VOP3Operand(Literal32.from_f16x2(4.0, 5.0)),
        )
        # 1*4 + 1*5 = 9.0
        res = self.interp.step(instr, self.state)
        self.assertEqual(bits_to_f16(res), 9.0)
        self.assertEqual(self.state.get_vgpr(11), res)

    def test_scalar_dot2acc_with_accumulator(self) -> None:
        self.state.set_vgpr(1, pack_f16x2(ONE_F16, TWO_F16))
        self.state.set_vgpr(2, pack_f16x2(THREE_F16, FOUR_F16))
        self.state.set_vgpr(3, FIVE_F16)
        instr = VDot2AccF16Instruction(
            dst=Reg(0, RegType.VGPR),
            src0=VOP3Operand(Reg(1, RegType.VGPR)),
            src1=VOP3Operand(Reg(2, RegType.VGPR)),
            src2=VOP3Operand(Reg(3, RegType.VGPR)),
        )
        # 1*3 + 2*4 + 5 = 16.0
        res = self.interp.step(instr, self.state)
        self.assertEqual(bits_to_f16(res), 16.0)
        self.assertEqual(self.state.get_vgpr(0), res)

    def test_scalar_modifiers_and_clamp(self) -> None:
        self.state.set_vgpr(0, pack_f16x2(NEG_ONE_F16, NEG_TWO_F16))
        self.state.set_vgpr(1, pack_f16x2(THREE_F16, FOUR_F16))
        instr = VDot2F16Instruction(
            dst=Reg(2, RegType.VGPR),
            src0=VOP3Operand(Reg(0, RegType.VGPR), abs_lo=True, abs_hi=True),
            src1=VOP3Operand(Reg(1, RegType.VGPR)),
            clamp=True,
        )
        # |-1|*3 + |-2|*4 = 11.0, clamped -> 1.0
        res = self.interp.step(instr, self.state)
        self.assertEqual(res, ONE_F16)

    def test_scalar_op_sel_permutation(self) -> None:
        self.state.set_vgpr(0, pack_f16x2(ONE_F16, TWO_F16))
        self.state.set_vgpr(1, pack_f16x2(THREE_F16, FOUR_F16))
        instr = VDot2F16Instruction(
            dst=Reg(2, RegType.VGPR),
            src0=VOP3Operand(Reg(0, RegType.VGPR)),
            src1=VOP3Operand(Reg(1, RegType.VGPR)),
            op_sel=(1, 0),
            op_sel_hi=(0, 1),
        )
        # s0[1]*s1[0] + s0[0]*s1[1] = 2*3 + 1*4 = 10.0
        res = self.interp.step(instr, self.state)
        self.assertEqual(bits_to_f16(res), 10.0)

    def test_scalar_execution_determinism(self) -> None:
        runs = []
        for _ in range(50):
            st = ScalarMachineState()
            st.set_vgpr(0, pack_f16x2(f16_to_bits(1.234), f16_to_bits(5.678)))
            st.set_vgpr(1, pack_f16x2(f16_to_bits(2.345), f16_to_bits(6.789)))
            instr = VDot2F16Instruction(
                dst=Reg(2, RegType.VGPR),
                src0=VOP3Operand(Reg(0, RegType.VGPR)),
                src1=VOP3Operand(Reg(1, RegType.VGPR)),
            )
            runs.append(self.interp.step(instr, st))
        self.assertTrue(all(r == runs[0] for r in runs))


class TestSIMDMultiLaneDivergence(unittest.TestCase):
    """Unit tests for SIMD execution, lane divergence, and 32/64-bit EXEC masking."""

    def setUp(self) -> None:
        self.interp = SIMDInterpreter()

    def test_wave32_full_exec_uniform_and_divergent(self) -> None:
        state = SIMDState(num_lanes=32, exec_mask=0xFFFF_FFFF)
        for lane in range(32):
            state.set_vgpr(lane, 0, pack_f16x2(f16_to_bits(float(lane)), ONE_F16))
            state.set_vgpr(lane, 1, pack_f16x2(TWO_F16, THREE_F16))

        instr = VDot2F16Instruction(
            dst=Reg(2, RegType.VGPR),
            src0=VOP3Operand(Reg(0, RegType.VGPR)),
            src1=VOP3Operand(Reg(1, RegType.VGPR)),
        )
        results = self.interp.step(instr, state)
        for lane in range(32):
            self.assertIsNotNone(results[lane])
            # lane * 2.0 + 1.0 * 3.0 = 2 * lane + 3
            expected = float(lane * 2 + 3)
            self.assertEqual(bits_to_f16(state.get_vgpr(lane, 2)), expected)

    def test_wave32_divergent_alternating_mask(self) -> None:
        sentinel = 0xDEAD_BEEF
        state = SIMDState(num_lanes=32, exec_mask=0x5555_5555)
        for lane in range(32):
            state.set_vgpr(lane, 0, pack_f16x2(ONE_F16, ONE_F16))
            state.set_vgpr(lane, 1, pack_f16x2(TWO_F16, TWO_F16))
            state.set_vgpr(lane, 2, sentinel)

        instr = VDot2F16Instruction(
            dst=Reg(2, RegType.VGPR),
            src0=VOP3Operand(Reg(0, RegType.VGPR)),
            src1=VOP3Operand(Reg(1, RegType.VGPR)),
        )
        results = self.interp.step(instr, state)
        for lane in range(32):
            if lane % 2 == 0:
                self.assertIsNotNone(results[lane])
                # 1*2 + 1*2 = 4.0
                self.assertEqual(bits_to_f16(state.get_vgpr(lane, 2)), 4.0)
            else:
                self.assertIsNone(results[lane])
                self.assertEqual(state.get_vgpr(lane, 2), sentinel)

    def test_wave32_exec_zero_no_modification(self) -> None:
        sentinel = 0xCAFE_BABE
        state = SIMDState(num_lanes=32, exec_mask=0x0)
        for lane in range(32):
            state.set_vgpr(lane, 2, sentinel)

        instr = VDot2F16Instruction(
            dst=Reg(2, RegType.VGPR),
            src0=VOP3Operand(Literal32.from_f16x2(1.0, 1.0)),
            src1=VOP3Operand(Literal32.from_f16x2(2.0, 2.0)),
        )
        results = self.interp.step(instr, state)
        self.assertTrue(all(r is None for r in results))
        for lane in range(32):
            self.assertEqual(state.get_vgpr(lane, 2), sentinel)

    def test_wave64_full_exec_dot2acc(self) -> None:
        state = SIMDState(num_lanes=64, exec_mask=MASK64)
        for lane in range(64):
            state.set_vgpr(lane, 0, pack_f16x2(ONE_F16, TWO_F16))
            state.set_vgpr(lane, 1, pack_f16x2(THREE_F16, FOUR_F16))
            state.set_vgpr(lane, 2, f16_to_bits(float(lane)))

        instr = VDot2AccF16Instruction(
            dst=Reg(3, RegType.VGPR),
            src0=VOP3Operand(Reg(0, RegType.VGPR)),
            src1=VOP3Operand(Reg(1, RegType.VGPR)),
            src2=VOP3Operand(Reg(2, RegType.VGPR)),
        )
        results = self.interp.step(instr, state)
        for lane in range(64):
            self.assertIsNotNone(results[lane])
            # 1*3 + 2*4 + lane = 11.0 + lane
            self.assertEqual(bits_to_f16(state.get_vgpr(lane, 3)), 11.0 + float(lane))

    def test_wave64_half_exec_lo_and_hi(self) -> None:
        sentinel = 0x1234_5678
        state = SIMDState(num_lanes=64, exec_mask=0x0000_0000_FFFF_FFFF)
        for lane in range(64):
            state.set_vgpr(lane, 0, pack_f16x2(ONE_F16, ONE_F16))
            state.set_vgpr(lane, 1, pack_f16x2(TWO_F16, TWO_F16))
            state.set_vgpr(lane, 2, sentinel)

        instr = VDot2F16Instruction(
            dst=Reg(2, RegType.VGPR),
            src0=VOP3Operand(Reg(0, RegType.VGPR)),
            src1=VOP3Operand(Reg(1, RegType.VGPR)),
        )
        self.interp.step(instr, state)
        for lane in range(32):
            self.assertEqual(bits_to_f16(state.get_vgpr(lane, 2)), 4.0)
        for lane in range(32, 64):
            self.assertEqual(state.get_vgpr(lane, 2), sentinel)

    def test_wave32_uniform_sgpr_broadcast(self) -> None:
        state = SIMDState(num_lanes=32, exec_mask=0xFFFF_FFFF)
        state.set_sgpr(10, pack_f16x2(TWO_F16, THREE_F16))
        for lane in range(32):
            state.set_vgpr(lane, 0, pack_f16x2(f16_to_bits(float(lane)), ONE_F16))

        instr = VDot2F16Instruction(
            dst=Reg(2, RegType.VGPR),
            src0=VOP3Operand(Reg(0, RegType.VGPR)),
            src1=VOP3Operand(Reg(10, RegType.SGPR)),
        )
        self.interp.step(instr, state)
        for lane in range(32):
            # lane * 2.0 + 1.0 * 3.0 = 2*lane + 3
            self.assertEqual(bits_to_f16(state.get_vgpr(lane, 2)), float(lane * 2 + 3))

    def test_simd_lane_dependent_clamp(self) -> None:
        state = SIMDState(num_lanes=32, exec_mask=0xFFFF_FFFF)
        # Even lanes produce positive large values (> 1.0), odd lanes produce negative values
        for lane in range(32):
            if lane % 2 == 0:
                state.set_vgpr(lane, 0, pack_f16x2(TWO_F16, TWO_F16))
                state.set_vgpr(lane, 1, pack_f16x2(TWO_F16, TWO_F16))
            else:
                state.set_vgpr(lane, 0, pack_f16x2(NEG_TWO_F16, NEG_TWO_F16))
                state.set_vgpr(lane, 1, pack_f16x2(ONE_F16, ONE_F16))

        instr = VDot2F16Instruction(
            dst=Reg(2, RegType.VGPR),
            src0=VOP3Operand(Reg(0, RegType.VGPR)),
            src1=VOP3Operand(Reg(1, RegType.VGPR)),
            clamp=True,
        )
        self.interp.step(instr, state)
        for lane in range(32):
            if lane % 2 == 0:
                self.assertEqual(state.get_vgpr(lane, 2), ONE_F16)
            else:
                self.assertEqual(state.get_vgpr(lane, 2), POS_ZERO)


class TestRegisterAliasing(unittest.TestCase):
    """Unit tests for register aliasing and in-place operand preservation."""

    def setUp(self) -> None:
        self.scalar_interp = ScalarInterpreter()
        self.simd_interp = SIMDInterpreter()

    def test_dst_alias_src0(self) -> None:
        state = ScalarMachineState()
        state.set_vgpr(0, pack_f16x2(ONE_F16, TWO_F16))
        state.set_vgpr(1, pack_f16x2(THREE_F16, FOUR_F16))
        instr = VDot2F16Instruction(
            dst=Reg(0, RegType.VGPR),
            src0=VOP3Operand(Reg(0, RegType.VGPR)),
            src1=VOP3Operand(Reg(1, RegType.VGPR)),
        )
        res = self.scalar_interp.step(instr, state)
        self.assertEqual(bits_to_f16(res), 11.0)
        self.assertEqual(state.get_vgpr(0), res)
        # src1 must be preserved intact
        self.assertEqual(state.get_vgpr(1), pack_f16x2(THREE_F16, FOUR_F16))

    def test_dst_alias_src1(self) -> None:
        state = ScalarMachineState()
        state.set_vgpr(0, pack_f16x2(ONE_F16, TWO_F16))
        state.set_vgpr(1, pack_f16x2(THREE_F16, FOUR_F16))
        instr = VDot2F16Instruction(
            dst=Reg(1, RegType.VGPR),
            src0=VOP3Operand(Reg(0, RegType.VGPR)),
            src1=VOP3Operand(Reg(1, RegType.VGPR)),
        )
        res = self.scalar_interp.step(instr, state)
        self.assertEqual(bits_to_f16(res), 11.0)
        self.assertEqual(state.get_vgpr(1), res)
        # src0 must be preserved intact
        self.assertEqual(state.get_vgpr(0), pack_f16x2(ONE_F16, TWO_F16))

    def test_dst_alias_src2_accumulator(self) -> None:
        state = ScalarMachineState()
        state.set_vgpr(0, pack_f16x2(ONE_F16, TWO_F16))
        state.set_vgpr(1, pack_f16x2(TWO_F16, ONE_F16))
        state.set_vgpr(2, FIVE_F16)
        instr = VDot2AccF16Instruction(
            dst=Reg(2, RegType.VGPR),
            src0=VOP3Operand(Reg(0, RegType.VGPR)),
            src1=VOP3Operand(Reg(1, RegType.VGPR)),
            src2=VOP3Operand(Reg(2, RegType.VGPR)),
        )
        # 1*2 + 2*1 + 5 = 9.0
        res = self.scalar_interp.step(instr, state)
        self.assertEqual(bits_to_f16(res), 9.0)
        self.assertEqual(state.get_vgpr(2), res)
        self.assertEqual(state.get_vgpr(0), pack_f16x2(ONE_F16, TWO_F16))
        self.assertEqual(state.get_vgpr(1), pack_f16x2(TWO_F16, ONE_F16))

    def test_dst_alias_all_sources(self) -> None:
        state = ScalarMachineState()
        # [1.0, 1.0] -> 1*1 + 1*1 + 1 = 3.0 (with low 16 bits = 1.0 treated as acc)
        state.set_vgpr(0, pack_f16x2(ONE_F16, ONE_F16))
        instr = VDot2AccF16Instruction(
            dst=Reg(0, RegType.VGPR),
            src0=VOP3Operand(Reg(0, RegType.VGPR)),
            src1=VOP3Operand(Reg(0, RegType.VGPR)),
            src2=VOP3Operand(Reg(0, RegType.VGPR)),
        )
        res = self.scalar_interp.step(instr, state)
        self.assertEqual(bits_to_f16(res), 3.0)
        self.assertEqual(state.get_vgpr(0), res)

    def test_inplace_iterative_dot2acc_accumulation(self) -> None:
        state = ScalarMachineState()
        state.set_vgpr(0, pack_f16x2(HALF_F16, HALF_F16))
        state.set_vgpr(1, pack_f16x2(ONE_F16, ONE_F16))
        state.set_vgpr(2, POS_ZERO)
        # each step adds 0.5*1.0 + 0.5*1.0 = 1.0 into v2
        instr = VDot2AccF16Instruction(
            dst=Reg(2, RegType.VGPR),
            src0=VOP3Operand(Reg(0, RegType.VGPR)),
            src1=VOP3Operand(Reg(1, RegType.VGPR)),
            src2=VOP3Operand(Reg(2, RegType.VGPR)),
        )
        for _ in range(50):
            self.scalar_interp.step(instr, state)
        self.assertEqual(bits_to_f16(state.get_vgpr(2)), 50.0)

    def test_simd_multi_lane_register_aliasing(self) -> None:
        state = SIMDState(num_lanes=32, exec_mask=0xFFFF_FFFF)
        for lane in range(32):
            state.set_vgpr(lane, 0, pack_f16x2(ONE_F16, ONE_F16))
            state.set_vgpr(lane, 1, pack_f16x2(TWO_F16, TWO_F16))
            state.set_vgpr(lane, 2, f16_to_bits(float(lane)))

        instr = VDot2AccF16Instruction(
            dst=Reg(2, RegType.VGPR),
            src0=VOP3Operand(Reg(0, RegType.VGPR)),
            src1=VOP3Operand(Reg(1, RegType.VGPR)),
            src2=VOP3Operand(Reg(2, RegType.VGPR)),
        )
        self.simd_interp.step(instr, state)
        for lane in range(32):
            # 1*2 + 1*2 + lane = 4.0 + lane
            self.assertEqual(bits_to_f16(state.get_vgpr(lane, 2)), 4.0 + float(lane))

    def test_independent_register_preservation(self) -> None:
        state = ScalarMachineState()
        for i in range(256):
            state.set_vgpr(i, 0xA5A5_0000 | i)
            state.set_sgpr(i, 0x5A5A_0000 | i)

        state.set_vgpr(10, pack_f16x2(ONE_F16, TWO_F16))
        state.set_vgpr(11, pack_f16x2(THREE_F16, FOUR_F16))
        instr = VDot2F16Instruction(
            dst=Reg(12, RegType.VGPR),
            src0=VOP3Operand(Reg(10, RegType.VGPR)),
            src1=VOP3Operand(Reg(11, RegType.VGPR)),
        )
        self.scalar_interp.step(instr, state)

        for i in range(256):
            if i not in (10, 11, 12):
                self.assertEqual(state.get_vgpr(i), 0xA5A5_0000 | i)
            self.assertEqual(state.get_sgpr(i), 0x5A5A_0000 | i)


class TestConditionCodesAndStatusFlags(unittest.TestCase):
    """Unit tests for status flags, condition codes, and carry generation."""

    def test_exact_finite_flags(self) -> None:
        s0 = pack_f16x2(ONE_F16, TWO_F16)
        s1 = pack_f16x2(THREE_F16, FOUR_F16)
        res = oracle_v_dot2_f16(s0, s1)
        flags = evaluate_v_dot2_f16_flags(s0, s1, res)
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
        # 1.0 * 1.0 + 1.0 * (1/3 approx) -> inexact
        s0 = pack_f16x2(ONE_F16, ONE_F16)
        s1 = pack_f16x2(ONE_F16, f16_to_bits(0.3333333))
        res = oracle_v_dot2_f16(s0, s1)
        flags = evaluate_v_dot2_f16_flags(s0, s1, res)
        self.assertTrue(flags.inexact)
        self.assertFalse(flags.overflow)

    def test_overflow_flag(self) -> None:
        s0 = pack_f16x2(NORM_MAX, NORM_MAX)
        s1 = pack_f16x2(TWO_F16, TWO_F16)
        res = oracle_v_dot2_f16(s0, s1)
        self.assertEqual(res, POS_INF)
        flags = evaluate_v_dot2_f16_flags(s0, s1, res)
        self.assertTrue(flags.overflow)
        self.assertTrue(flags.infinite)
        self.assertTrue(flags.inexact)

    def test_underflow_and_denorm_flags(self) -> None:
        s0 = pack_f16x2(SUB_MIN, POS_ZERO)
        s1 = pack_f16x2(ONE_F16, POS_ZERO)
        res = oracle_v_dot2_f16(s0, s1)
        flags = evaluate_v_dot2_f16_flags(s0, s1, res)
        self.assertTrue(flags.denormal)
        self.assertTrue(flags.underflow)

    def test_invalid_operation_flag_snan_and_inf_zero(self) -> None:
        # SNaN input triggers invalid
        s0 = pack_f16x2(CANONICAL_SNAN, ONE_F16)
        s1 = pack_f16x2(ONE_F16, ONE_F16)
        res = oracle_v_dot2_f16(s0, s1)
        flags = evaluate_v_dot2_f16_flags(s0, s1, res)
        self.assertTrue(flags.invalid)
        self.assertTrue(flags.nan)

        # 0 * inf triggers invalid
        s0_inf = pack_f16x2(POS_INF, ONE_F16)
        s1_zero = pack_f16x2(POS_ZERO, ONE_F16)
        res_inf = oracle_v_dot2_f16(s0_inf, s1_zero)
        flags_inf = evaluate_v_dot2_f16_flags(s0_inf, s1_zero, res_inf)
        self.assertTrue(flags_inf.invalid)
        self.assertTrue(flags_inf.nan)

    def test_invalid_operation_flag_inf_cancellation(self) -> None:
        # (+inf) + (-inf) product sum triggers invalid
        s0 = pack_f16x2(POS_INF, NEG_INF)
        s1 = pack_f16x2(ONE_F16, ONE_F16)
        res = oracle_v_dot2_f16(s0, s1)
        flags = evaluate_v_dot2_f16_flags(s0, s1, res)
        self.assertTrue(flags.invalid)
        self.assertTrue(flags.nan)

    def test_zero_and_negative_flags(self) -> None:
        s0 = pack_f16x2(POS_ZERO, POS_ZERO)
        s1 = pack_f16x2(ONE_F16, ONE_F16)
        res = oracle_v_dot2_f16(s0, s1)
        flags = evaluate_v_dot2_f16_flags(s0, s1, res)
        self.assertTrue(flags.zero)
        self.assertFalse(flags.negative)

        s0_neg = pack_f16x2(NEG_ONE_F16, NEG_TWO_F16)
        s1_pos = pack_f16x2(TWO_F16, THREE_F16)
        res_neg = oracle_v_dot2_f16(s0_neg, s1_pos)
        flags_neg = evaluate_v_dot2_f16_flags(s0_neg, s1_pos, res_neg)
        self.assertFalse(flags_neg.zero)
        self.assertTrue(flags_neg.negative)

    def test_dot2acc_flags_with_accumulator(self) -> None:
        s0 = pack_f16x2(ONE_F16, TWO_F16)
        s1 = pack_f16x2(THREE_F16, FOUR_F16)
        res = oracle_v_dot2acc_f16(s0, s1, FIVE_F16)
        flags = evaluate_v_dot2acc_f16_flags(s0, s1, FIVE_F16, res)
        self.assertFalse(flags.invalid)
        self.assertEqual(bits_to_f16(res), 16.0)

    def test_mantissa_carry_generation(self) -> None:
        # 1.0 + 1.0 in f16: mantissas 1.0000000000 + 1.0000000000 = 2.0000000000 -> carry_out = 1
        _, carry1 = compute_f16_mantissa_carry(ONE_F16, ONE_F16)
        self.assertEqual(carry1, 1)

        # 1.0 + 0.25 in f16: mantissa sum < 2.0 -> carry_out = 0
        _, carry0 = compute_f16_mantissa_carry(ONE_F16, f16_to_bits(0.25))
        self.assertEqual(carry0, 0)


class TestIEEE754BoundariesAndNaN(unittest.TestCase):
    """Unit tests for IEEE 754 boundary extremes, subnormals, and NaN behavior."""

    def test_canonical_nan_quietening_and_propagation(self) -> None:
        # SNaN in s0 should be quietened
        s0_snan = pack_f16x2(CANONICAL_SNAN, ONE_F16)
        s1 = pack_f16x2(ONE_F16, ONE_F16)
        res = oracle_v_dot2_f16(s0_snan, s1)
        self.assertTrue(is_nan(res))
        self.assertTrue(is_qnan(res))

        # QNaN in s1 should propagate
        s1_qnan = pack_f16x2(ONE_F16, CANONICAL_QNAN)
        res2 = oracle_v_dot2_f16(s1, s1_qnan)
        self.assertTrue(is_nan(res2))
        self.assertTrue(is_qnan(res2))

    def test_nan_clamping_to_positive_zero(self) -> None:
        # RDNA2 VALU clamp guarantees NaN is clamped to +0.0
        s0_nan = pack_f16x2(CANONICAL_QNAN, ONE_F16)
        s1 = pack_f16x2(ONE_F16, ONE_F16)
        res_clamped = oracle_v_dot2_f16(s0_nan, s1, clamp=True)
        self.assertEqual(res_clamped, POS_ZERO)

        # Clamping NaN with accumulator
        res_acc_clamped = oracle_v_dot2acc_f16(s1, s1, CANONICAL_QNAN, clamp=True)
        self.assertEqual(res_acc_clamped, POS_ZERO)

    def test_infinity_times_zero_produces_qnan(self) -> None:
        s0 = pack_f16x2(POS_INF, ONE_F16)
        s1 = pack_f16x2(POS_ZERO, ONE_F16)
        res = oracle_v_dot2_f16(s0, s1)
        self.assertTrue(is_nan(res))
        self.assertTrue(is_qnan(res))

    def test_infinity_addition_and_cancellation(self) -> None:
        # (+inf) + (+inf) -> +inf
        s0_inf = pack_f16x2(POS_INF, POS_INF)
        s1_one = pack_f16x2(ONE_F16, ONE_F16)
        res_inf = oracle_v_dot2_f16(s0_inf, s1_one)
        self.assertEqual(res_inf, POS_INF)

        # (+inf) + (-inf) -> Canonical QNaN
        s0_canc = pack_f16x2(POS_INF, NEG_INF)
        res_canc = oracle_v_dot2_f16(s0_canc, s1_one)
        self.assertTrue(is_nan(res_canc))
        self.assertTrue(is_qnan(res_canc))

    def test_clamping_infinity_boundaries(self) -> None:
        s0_pos_inf = pack_f16x2(POS_INF, ONE_F16)
        s1_one = pack_f16x2(ONE_F16, ONE_F16)
        # +inf clamped -> 1.0
        res_clamped_pos = oracle_v_dot2_f16(s0_pos_inf, s1_one, clamp=True)
        self.assertEqual(res_clamped_pos, ONE_F16)

        # -inf clamped -> +0.0
        s0_neg_inf = pack_f16x2(NEG_INF, ONE_F16)
        res_clamped_neg = oracle_v_dot2_f16(s0_neg_inf, s1_one, clamp=True)
        self.assertEqual(res_clamped_neg, POS_ZERO)

    def test_subnormal_operands_and_gradual_underflow(self) -> None:
        # Subnormal inputs
        s0 = pack_f16x2(SUB_MIN, SUB_MAX)
        s1 = pack_f16x2(ONE_F16, POS_ZERO)
        # SUB_MIN * 1.0 + SUB_MAX * 0.0 = SUB_MIN
        res = oracle_v_dot2_f16(s0, s1)
        self.assertEqual(res, SUB_MIN)
        self.assertTrue(is_subnormal(res))

    def test_subnormal_precision_preservation(self) -> None:
        s0 = pack_f16x2(SUB_MAX, POS_ZERO)
        s1 = pack_f16x2(ONE_F16, POS_ZERO)
        res = oracle_v_dot2_f16(s0, s1)
        self.assertEqual(res, SUB_MAX)

        s0_neg = pack_f16x2(NEG_SUB_MIN, POS_ZERO)
        res_neg = oracle_v_dot2_f16(s0_neg, s1)
        self.assertEqual(res_neg, NEG_SUB_MIN)

    def test_largest_normal_overflow_boundaries(self) -> None:
        # 65504.0 * 2.0 = 131008.0 -> overflows binary16 to +inf
        s0 = pack_f16x2(NORM_MAX, POS_ZERO)
        s1 = pack_f16x2(TWO_F16, POS_ZERO)
        res = oracle_v_dot2_f16(s0, s1)
        self.assertEqual(res, POS_INF)

        # NORM_MAX + NORM_MAX -> +inf
        s0_both = pack_f16x2(NORM_MAX, NORM_MAX)
        s1_ones = pack_f16x2(ONE_F16, ONE_F16)
        res_both = oracle_v_dot2_f16(s0_both, s1_ones)
        self.assertEqual(res_both, POS_INF)

    def test_signed_zero_multiplication_signs(self) -> None:
        # (+0) * (-1) = -0; (+0) + (-0) = +0
        s0 = pack_f16x2(POS_ZERO, POS_ZERO)
        s1 = pack_f16x2(NEG_ONE_F16, ONE_F16)
        res = oracle_v_dot2_f16(s0, s1)
        self.assertEqual(res, POS_ZERO)

        # (-0) * (+1) + (-0) * (+1) = -0 + -0 = -0
        s0_neg = pack_f16x2(NEG_ZERO, NEG_ZERO)
        s1_pos = pack_f16x2(ONE_F16, ONE_F16)
        res_neg = oracle_v_dot2_f16(s0_neg, s1_pos)
        self.assertEqual(res_neg, NEG_ZERO)

    def test_subnormal_clamp_behavior(self) -> None:
        # Positive subnormal is in (0, 1) -> remains positive subnormal
        s0 = pack_f16x2(SUB_MAX, POS_ZERO)
        s1 = pack_f16x2(ONE_F16, POS_ZERO)
        res_sub = oracle_v_dot2_f16(s0, s1, clamp=True)
        self.assertEqual(res_sub, SUB_MAX)

        # Negative subnormal is < 0 -> clamps to +0.0
        s0_neg = pack_f16x2(NEG_SUB_MAX, POS_ZERO)
        res_neg = oracle_v_dot2_f16(s0_neg, s1, clamp=True)
        self.assertEqual(res_neg, POS_ZERO)


class TestNegativeControlsAndMutationDetection(unittest.TestCase):
    """Negative controls, syntax rejection, and mutation detection tests."""

    def test_invalid_register_indices_out_of_range(self) -> None:
        with self.assertRaises(ValueError):
            Reg(-1, RegType.VGPR)
        with self.assertRaises(ValueError):
            Reg(256, RegType.VGPR)
        with self.assertRaises(ValueError):
            Reg(999, RegType.SGPR)

    def test_invalid_destination_type_sgpr_rejected(self) -> None:
        with self.assertRaises(TypeError):
            VDot2F16Instruction(
                dst=Reg(0, RegType.SGPR),
                src0=VOP3Operand(Reg(1, RegType.VGPR)),
                src1=VOP3Operand(Reg(2, RegType.VGPR)),
            )

    def test_mutation_fmul_tampered_to_fadd(self) -> None:
        """Sabotaged implementation using addition instead of multiplication is detected."""
        s0 = pack_f16x2(TWO_F16, THREE_F16)
        s1 = pack_f16x2(FOUR_F16, TWO_F16)
        # Expected: 2*4 + 3*2 = 8 + 6 = 14.0
        golden = oracle_v_dot2_f16(s0, s1)
        self.assertEqual(bits_to_f16(golden), 14.0)

        # Mutant: (2+4) + (3+2) = 6 + 5 = 11.0
        mutant_val = add_f16(add_f16(TWO_F16, FOUR_F16), add_f16(THREE_F16, TWO_F16))
        self.assertNotEqual(golden, mutant_val)

    def test_mutation_fadd_tampered_to_fsub(self) -> None:
        """Sabotaged implementation using subtraction instead of addition is detected."""
        s0 = pack_f16x2(THREE_F16, TWO_F16)
        s1 = pack_f16x2(TWO_F16, ONE_F16)
        # Expected: 3*2 + 2*1 = 6 + 2 = 8.0
        golden = oracle_v_dot2_f16(s0, s1)
        self.assertEqual(bits_to_f16(golden), 8.0)

        # Mutant: 3*2 - 2*1 = 6 - 2 = 4.0
        p0 = mul_f16(THREE_F16, TWO_F16)
        p1 = mul_f16(TWO_F16, ONE_F16)
        mutant_val = add_f16(p0, apply_f16_modifiers(p1, False, True))
        self.assertNotEqual(golden, mutant_val)

    def test_mutation_accumulator_drop_detected(self) -> None:
        """Sabotaged implementation dropping the accumulator is caught by oracle."""
        s0 = pack_f16x2(ONE_F16, ONE_F16)
        s1 = pack_f16x2(ONE_F16, ONE_F16)
        acc = FIVE_F16
        # Expected: 1*1 + 1*1 + 5 = 7.0
        golden = oracle_v_dot2acc_f16(s0, s1, acc)
        self.assertEqual(bits_to_f16(golden), 7.0)

        # Mutant: 1*1 + 1*1 = 2.0 (accumulator omitted)
        mutant_val = oracle_v_dot2_f16(s0, s1)
        self.assertNotEqual(golden, mutant_val)

    def test_mutation_clamp_removal_detected(self) -> None:
        """Sabotaged implementation removing clamp is caught on saturated outputs."""
        s0 = pack_f16x2(TWO_F16, TWO_F16)
        s1 = pack_f16x2(TWO_F16, TWO_F16)
        # Expected clamped: 2*2 + 2*2 = 8.0 clamped -> 1.0
        golden = oracle_v_dot2_f16(s0, s1, clamp=True)
        self.assertEqual(golden, ONE_F16)

        # Mutant: unclamped 8.0
        mutant_unclamped = oracle_v_dot2_f16(s0, s1, clamp=False)
        self.assertNotEqual(golden, mutant_unclamped)

    def test_mutation_op_sel_misrouting_detected(self) -> None:
        """Sabotaged implementation reading wrong halfword is caught."""
        # s0: lo=1.0, hi=4.0; s1: lo=2.0, hi=3.0
        s0 = pack_f16x2(ONE_F16, FOUR_F16)
        s1 = pack_f16x2(TWO_F16, THREE_F16)
        # Normal op_sel (0,0), (1,1): 1*2 + 4*3 = 14.0
        golden = oracle_v_dot2_f16(s0, s1)
        self.assertEqual(bits_to_f16(golden), 14.0)

        # Mutant: swapped to lo*lo + lo*hi: 1*2 + 1*3 = 5.0
        mutant_misrouted = oracle_v_dot2_f16(s0, s1, op_sel=(0, 0), op_sel_hi=(0, 1))
        self.assertNotEqual(golden, mutant_misrouted)

    def test_mutation_neg_lo_inversion_detected(self) -> None:
        """Sabotaged implementation ignoring negation modifier is caught."""
        s0 = pack_f16x2(TWO_F16, THREE_F16)
        s1 = pack_f16x2(TWO_F16, ONE_F16)
        # neg on s0 lo: (-2)*2 + 3*1 = -4 + 3 = -1.0
        golden = oracle_v_dot2_f16(s0, s1, neg0=(True, False))
        self.assertEqual(bits_to_f16(golden), -1.0)

        # Mutant: negation ignored -> 2*2 + 3*1 = 7.0
        mutant_ignored_neg = oracle_v_dot2_f16(s0, s1, neg0=(False, False))
        self.assertNotEqual(golden, mutant_ignored_neg)


if __name__ == "__main__":
    unittest.main()
