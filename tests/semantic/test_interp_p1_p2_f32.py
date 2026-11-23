from __future__ import annotations

"""Semantic interpreter tests and reference mathematical oracle for RDNA2 v_interp_p1_f32, v_interp_p2_f32, and v_interp_mov_f32.

RDNA2 VINTRP instruction family for perspective parameter interpolation:
- v_interp_p1_f32: Computes D = P10 * S + P0
  Evaluates the first barycentric coordinate axis (i) with attribute difference P10 and base P0.
- v_interp_p2_f32: Computes D = P20 * S + D
  Accumulates the second barycentric coordinate axis (j) with attribute difference P20 onto the accumulated D from p1.
Combined perspective evaluation:
  D = P0 + i * (P1 - P0) + j * (P2 - P0) = (1 - i - j) * P0 + i * P1 + j * P2
- v_interp_mov_f32: Computes direct attribute parameter extraction (flat shading or direct parameter access):
  param_sel 0 (p0): P0
  param_sel 1 (p10): P10 = P1 - P0
  param_sel 2 (p20): P20 = P2 - P0

Perspective-correct Barycentrics:
  Given triangle vertex clip depths W0, W1, W2 > 0 and screen barycentrics (l0, l1, l2):
    inv_W = l0 / W0 + l1 / W1 + l2 / W2
    i_persp = (l1 / W1) / inv_W
    j_persp = (l2 / W2) / inv_W
"""

from dataclasses import dataclass, field
from enum import Enum, auto
import math
import struct
from typing import List, Optional, Tuple, Union
import unittest

# IEEE 754 binary32 architectural constants
SIGN_MASK_F32: int = 0x8000_0000
EXP_MASK_F32: int = 0x7F80_0000
FRAC_MASK_F32: int = 0x007F_FFFF
QNAN_BIT_F32: int = 0x0040_0000

CANONICAL_QNAN_F32: int = 0x7FC0_0000
POS_ZERO_F32: int = 0x0000_0000
NEG_ZERO_F32: int = 0x8000_0000
POS_INF_F32: int = 0x7F80_0000
NEG_INF_F32: int = 0xFF80_0000
ONE_F32: int = 0x3F80_0000

MASK32: int = 0xFFFF_FFFF


def f32_to_bits(val: float) -> int:
    """Convert a Python float to 32-bit unsigned IEEE 754 integer bits."""
    try:
        return struct.unpack("<I", struct.pack("<f", float(val)))[0]
    except OverflowError:
        return POS_INF_F32 if val > 0 else NEG_INF_F32


def bits_to_f32(bits: int) -> float:
    """Convert 32-bit integer bits to a Python float."""
    return struct.unpack("<f", struct.pack("<I", bits & MASK32))[0]


def f32_round(val: float) -> float:
    """Round a float to single-precision IEEE 754 binary32."""
    try:
        return struct.unpack("<f", struct.pack("<f", float(val)))[0]
    except OverflowError:
        return float("inf") if val > 0 else float("-inf")


def is_nan_f32(bits: int) -> bool:
    return ((bits & EXP_MASK_F32) == EXP_MASK_F32) and ((bits & FRAC_MASK_F32) != 0)


def is_inf_f32(bits: int) -> bool:
    return ((bits & EXP_MASK_F32) == EXP_MASK_F32) and ((bits & FRAC_MASK_F32) == 0)


def is_zero_f32(bits: int) -> bool:
    return (bits & ~SIGN_MASK_F32) == 0


def apply_f32_modifiers(bits: int, abs_mod: bool = False, neg_mod: bool = False) -> int:
    """Apply VOP3 source modifiers: abs clears sign bit; neg toggles sign bit."""
    res = bits & MASK32
    if abs_mod:
        res &= ~SIGN_MASK_F32
    if neg_mod:
        res ^= SIGN_MASK_F32
    return res


def apply_f32_clamp(val_bits: int) -> int:
    """Apply RDNA2 clamp [0.0, 1.0] modifier; NaNs clamp to +0.0."""
    if is_nan_f32(val_bits) or val_bits == NEG_ZERO_F32:
        return POS_ZERO_F32
    val = bits_to_f32(val_bits)
    if val < 0.0:
        return POS_ZERO_F32
    if val > 1.0:
        return ONE_F32
    return val_bits


def apply_f32_omod(val_bits: int, omod: int) -> int:
    """Apply RDNA2 output modifier: 0: none, 1: mul:2, 2: mul:4, 3: div:2."""
    if omod == 0:
        return val_bits
    if is_nan_f32(val_bits) or is_inf_f32(val_bits):
        return val_bits
    val = bits_to_f32(val_bits)
    if omod == 1:
        res = val * 2.0
    elif omod == 2:
        res = val * 4.0
    elif omod == 3:
        res = val * 0.5
    else:
        raise ValueError(f"Invalid omod {omod}")
    return f32_to_bits(res)


def raw_fma_f32(a: float, b: float, c: float) -> float:
    """Evaluate single-precision fused multiply-add (a * b) + c using binary64 intermediate."""
    if math.isnan(a) or math.isnan(b) or math.isnan(c):
        return float("nan")
    # Handling inf * 0
    if (math.isinf(a) and b == 0.0) or (a == 0.0 and math.isinf(b)):
        return float("nan")
    # Python 64-bit float has 53 mantissa bits; binary32 product has at most 48 mantissa bits.
    # Therefore a * b is exact with 0 rounding error before adding c.
    prod = a * b
    if math.isinf(prod) and math.isinf(c):
        if (prod > 0 and c < 0) or (prod < 0 and c > 0):
            return float("nan")
    res = prod + c
    return f32_round(res)


def oracle_v_interp_p1_f32(
    s_bits: int,
    p10_bits: int,
    p0_bits: int,
    abs_s: bool = False,
    neg_s: bool = False,
    clamp: bool = False,
    omod: int = 0,
) -> int:
    """Reference mathematical oracle for v_interp_p1_f32: D = P10 * S + P0."""
    s_mod = apply_f32_modifiers(s_bits, abs_mod=abs_s, neg_mod=neg_s)
    s_val = bits_to_f32(s_mod)
    p10_val = bits_to_f32(p10_bits)
    p0_val = bits_to_f32(p0_bits)

    res_f = raw_fma_f32(p10_val, s_val, p0_val)
    res_bits = f32_to_bits(res_f)

    if math.isnan(res_f):
        res_bits = CANONICAL_QNAN_F32

    if omod != 0:
        res_bits = apply_f32_omod(res_bits, omod)
    if clamp:
        res_bits = apply_f32_clamp(res_bits)
    return res_bits


def oracle_v_interp_p2_f32(
    s_bits: int,
    p20_bits: int,
    p1_accum_bits: int,
    abs_s: bool = False,
    neg_s: bool = False,
    clamp: bool = False,
    omod: int = 0,
) -> int:
    """Reference mathematical oracle for v_interp_p2_f32: D = P20 * S + D."""
    s_mod = apply_f32_modifiers(s_bits, abs_mod=abs_s, neg_mod=neg_s)
    s_val = bits_to_f32(s_mod)
    p20_val = bits_to_f32(p20_bits)
    d_val = bits_to_f32(p1_accum_bits)

    res_f = raw_fma_f32(p20_val, s_val, d_val)
    res_bits = f32_to_bits(res_f)

    if math.isnan(res_f):
        res_bits = CANONICAL_QNAN_F32

    if omod != 0:
        res_bits = apply_f32_omod(res_bits, omod)
    if clamp:
        res_bits = apply_f32_clamp(res_bits)
    return res_bits


def oracle_v_interp_mov_f32(
    param_sel: int,
    p0_bits: int,
    p10_bits: int,
    p20_bits: int,
    abs_val: bool = False,
    neg_val: bool = False,
    clamp: bool = False,
    omod: int = 0,
) -> int:
    """Reference mathematical oracle for v_interp_mov_f32."""
    if param_sel == 0:
        src_bits = p0_bits
    elif param_sel == 1:
        src_bits = p10_bits
    elif param_sel == 2:
        src_bits = p20_bits
    else:
        raise ValueError(f"Invalid param_sel {param_sel}, expected 0 (p0), 1 (p10), or 2 (p20)")

    res_bits = apply_f32_modifiers(src_bits, abs_mod=abs_val, neg_mod=neg_val)
    if omod != 0:
        res_bits = apply_f32_omod(res_bits, omod)
    if clamp:
        res_bits = apply_f32_clamp(res_bits)
    return res_bits


def oracle_interpolate_triangle(
    i_coord: float,
    j_coord: float,
    p0: float,
    p1: float,
    p2: float,
    clamp: bool = False,
) -> float:
    """End-to-end two-stage interpolation oracle: p1 then p2."""
    p10 = f32_round(p1 - p0)
    p20 = f32_round(p2 - p0)

    p1_bits = oracle_v_interp_p1_f32(f32_to_bits(i_coord), f32_to_bits(p10), f32_to_bits(p0))
    p2_bits = oracle_v_interp_p2_f32(f32_to_bits(j_coord), f32_to_bits(p20), p1_bits, clamp=clamp)
    return bits_to_f32(p2_bits)


def compute_perspective_coords(
    l0: float, l1: float, l2: float, w0: float, w1: float, w2: float
) -> Tuple[float, float]:
    """Calculate perspective-correct barycentric weights (i_persp, j_persp)."""
    inv_w0 = 1.0 / w0
    inv_w1 = 1.0 / w1
    inv_w2 = 1.0 / w2
    inv_w_total = l0 * inv_w0 + l1 * inv_w1 + l2 * inv_w2
    i_persp = (l1 * inv_w1) / inv_w_total
    j_persp = (l2 * inv_w2) / inv_w_total
    return f32_round(i_persp), f32_round(j_persp)


@dataclass
class AttributeChannel:
    """Architectural model for one channel of a shader attribute (P0, P10, P20)."""
    p0: float = 0.0
    p10: float = 0.0
    p20: float = 0.0

    @classmethod
    def from_vertices(cls, v0: float, v1: float, v2: float) -> AttributeChannel:
        return cls(
            p0=f32_round(v0),
            p10=f32_round(v1 - v0),
            p20=f32_round(v2 - v0),
        )

    def get_bits(self, param_sel: int) -> int:
        if param_sel == 0:
            return f32_to_bits(self.p0)
        elif param_sel == 1:
            return f32_to_bits(self.p10)
        elif param_sel == 2:
            return f32_to_bits(self.p20)
        raise ValueError(f"Unknown param_sel: {param_sel}")


@dataclass
class AttributeFile:
    """Architectural model of the RDNA2 SPI attribute parameter cache (32 attributes x 4 channels)."""
    attrs: List[List[AttributeChannel]] = field(
        default_factory=lambda: [[AttributeChannel() for _ in range(4)] for _ in range(32)]
    )

    def set_attribute(self, attr_idx: int, chan_idx: int, v0: float, v1: float, v2: float) -> None:
        self.attrs[attr_idx][chan_idx] = AttributeChannel.from_vertices(v0, v1, v2)

    def get_channel(self, attr_idx: int, chan_idx: int) -> AttributeChannel:
        return self.attrs[attr_idx][chan_idx]


@dataclass
class VGPRFile:
    """Architectural model of 256 32-bit Vector General Purpose Registers (VGPRs)."""
    regs: List[int] = field(default_factory=lambda: [0] * 256)

    def get(self, idx: int) -> int:
        return self.regs[idx & 0xFF]

    def set(self, idx: int, val: int) -> None:
        self.regs[idx & 0xFF] = val & MASK32

    def get_f32(self, idx: int) -> float:
        return bits_to_f32(self.get(idx))

    def set_f32(self, idx: int, val: float) -> None:
        self.set(idx, f32_to_bits(val))


class TestInterpMathematicalOracle(unittest.TestCase):
    """Test suite for reference mathematical oracle and architectural models."""

    def test_basic_interpolation_identities(self) -> None:
        p0, p1, p2 = 10.0, 20.0, 40.0
        # at vertex 0: (i=0, j=0) -> p0
        self.assertAlmostEqual(oracle_interpolate_triangle(0.0, 0.0, p0, p1, p2), 10.0)
        # at vertex 1: (i=1, j=0) -> p1
        self.assertAlmostEqual(oracle_interpolate_triangle(1.0, 0.0, p0, p1, p2), 20.0)
        # at vertex 2: (i=0, j=1) -> p2
        self.assertAlmostEqual(oracle_interpolate_triangle(0.0, 1.0, p0, p1, p2), 40.0)
        # midpoint of edge 0-1: (i=0.5, j=0) -> 15.0
        self.assertAlmostEqual(oracle_interpolate_triangle(0.5, 0.0, p0, p1, p2), 15.0)
        # centroid: (i=1/3, j=1/3) -> (10 + 20 + 40)/3 = 70/3 = 23.33333...
        self.assertAlmostEqual(oracle_interpolate_triangle(1.0 / 3.0, 1.0 / 3.0, p0, p1, p2), 70.0 / 3.0, places=5)

    def test_interp_mov_f32(self) -> None:
        p0 = f32_to_bits(12.5)
        p10 = f32_to_bits(5.0)
        p20 = f32_to_bits(-7.5)

        self.assertEqual(oracle_v_interp_mov_f32(0, p0, p10, p20), p0)
        self.assertEqual(oracle_v_interp_mov_f32(1, p0, p10, p20), p10)
        self.assertEqual(oracle_v_interp_mov_f32(2, p0, p10, p20), p20)

    def test_source_modifiers(self) -> None:
        p0 = f32_to_bits(10.0)
        p10 = f32_to_bits(2.0)
        s = f32_to_bits(-3.0)

        # with abs_s: s becomes +3.0 -> 2.0 * 3.0 + 10.0 = 16.0
        res_abs = oracle_v_interp_p1_f32(s, p10, p0, abs_s=True)
        self.assertEqual(bits_to_f32(res_abs), 16.0)

        # with neg_s: s becomes -(-3.0) = +3.0 -> 16.0
        res_neg = oracle_v_interp_p1_f32(s, p10, p0, neg_s=True)
        self.assertEqual(bits_to_f32(res_neg), 16.0)

        # with abs_s and neg_s: s becomes -|s| = -3.0 -> 2.0 * (-3.0) + 10.0 = 4.0
        res_both = oracle_v_interp_p1_f32(s, p10, p0, abs_s=True, neg_s=True)
        self.assertEqual(bits_to_f32(res_both), 4.0)

    def test_clamp_and_omod(self) -> None:
        p0 = f32_to_bits(0.5)
        p10 = f32_to_bits(0.75)
        s = f32_to_bits(1.0)
        # res = 0.75 * 1.0 + 0.5 = 1.25 -> clamped to 1.0
        res_clamped = oracle_v_interp_p1_f32(s, p10, p0, clamp=True)
        self.assertEqual(bits_to_f32(res_clamped), 1.0)

        # omod 1 (mul:2): (0.75 * 1.0 + 0.5) * 2 = 1.25 * 2 = 2.5
        res_omod = oracle_v_interp_p1_f32(s, p10, p0, omod=1)
        self.assertEqual(bits_to_f32(res_omod), 2.5)

    def test_perspective_correct_barycentrics(self) -> None:
        # Depth values: W0 = 1.0, W1 = 2.0, W2 = 4.0
        # Screen barycentrics: l0 = 0.2, l1 = 0.5, l2 = 0.3
        i_persp, j_persp = compute_perspective_coords(0.2, 0.5, 0.3, 1.0, 2.0, 4.0)
        self.assertAlmostEqual(i_persp, 10.0 / 21.0, places=5)
        self.assertAlmostEqual(j_persp, 3.0 / 21.0, places=5)

    def test_architectural_models(self) -> None:
        af = AttributeFile()
        af.set_attribute(3, 1, 100.0, 200.0, 300.0)
        chan = af.get_channel(3, 1)
        self.assertEqual(chan.p0, 100.0)
        self.assertEqual(chan.p10, 100.0)
        self.assertEqual(chan.p20, 200.0)

        vgprs = VGPRFile()
        vgprs.set_f32(5, 42.5)
        self.assertEqual(vgprs.get_f32(5), 42.5)


class InterpOpcode(Enum):
    V_INTERP_P1_F32 = "v_interp_p1_f32"
    V_INTERP_P2_F32 = "v_interp_p2_f32"
    V_INTERP_MOV_F32 = "v_interp_mov_f32"


class ParamSel(Enum):
    P0 = 0
    P10 = 1
    P20 = 2

    @classmethod
    def from_str(cls, s: str) -> ParamSel:
        s_low = s.strip().lower()
        if s_low in ("p0", "0"):
            return cls.P0
        if s_low in ("p10", "1"):
            return cls.P10
        if s_low in ("p20", "2"):
            return cls.P20
        raise ValueError(f"Unknown param selector: {s}")


class Channel(Enum):
    X = 0
    Y = 1
    Z = 2
    W = 3

    @classmethod
    def from_str(cls, s: str) -> Channel:
        s_low = s.strip().lower()
        mapping = {"x": cls.X, "y": cls.Y, "z": cls.Z, "w": cls.W}
        if s_low in mapping:
            return mapping[s_low]
        raise ValueError(f"Unknown channel: {s}")


class RegType(Enum):
    VGPR = "vgpr"
    SGPR = "sgpr"
    LITERAL = "literal"
    PARAM_SEL = "param_sel"


@dataclass(frozen=True)
class AttributeRef:
    attr_index: int
    channel: Channel

    def __post_init__(self) -> None:
        if not (0 <= self.attr_index <= 31):
            raise ValueError(f"Attribute index out of bounds [0, 31]: {self.attr_index}")

    @property
    def name(self) -> str:
        return f"attr{self.attr_index}.{self.channel.name.lower()}"

    @classmethod
    def parse(cls, s: str) -> AttributeRef:
        s = s.strip().lower()
        if not s.startswith("attr"):
            raise ValueError(f"Invalid attribute syntax: {s}")
        parts = s[4:].split(".")
        if len(parts) != 2:
            raise ValueError(f"Expected attr<N>.<chan>, got: {s}")
        idx = int(parts[0])
        chan = Channel.from_str(parts[1])
        return cls(attr_index=idx, channel=chan)


@dataclass(frozen=True)
class Operand:
    reg_type: RegType
    index_or_val: Union[int, float, ParamSel]
    abs_mod: bool = False
    neg_mod: bool = False

    def disasm(self) -> str:
        if self.reg_type == RegType.VGPR:
            base = f"v{self.index_or_val}"
        elif self.reg_type == RegType.SGPR:
            base = f"s{self.index_or_val}"
        elif self.reg_type == RegType.PARAM_SEL:
            if isinstance(self.index_or_val, ParamSel):
                return self.index_or_val.name.lower()
            return f"p{self.index_or_val}"
        else:
            base = f"{self.index_or_val}"
        if self.abs_mod:
            base = f"|{base}|"
        if self.neg_mod:
            base = f"-{base}"
        return base


@dataclass(frozen=True)
class InterpInstruction:
    opcode: InterpOpcode
    dst: int  # VGPR index
    src: Operand
    attr: AttributeRef
    clamp: bool = False
    omod: int = 0

    def disasm(self) -> str:
        parts = [f"{self.opcode.value} v{self.dst}, {self.src.disasm()}, {self.attr.name}"]
        if self.clamp:
            parts.append("clamp")
        if self.omod == 1:
            parts.append("mul:2")
        elif self.omod == 2:
            parts.append("mul:4")
        elif self.omod == 3:
            parts.append("div:2")
        return " ".join(parts)


@dataclass(frozen=True)
class SymbolicInterpExpr:
    opcode: InterpOpcode
    src_expr: str
    p0_sym: str
    p10_sym: str
    p20_sym: str
    accum_expr: Optional[str] = None

    def to_math_string(self) -> str:
        if self.opcode == InterpOpcode.V_INTERP_P1_F32:
            return f"({self.p10_sym} * {self.src_expr} + {self.p0_sym})"
        elif self.opcode == InterpOpcode.V_INTERP_P2_F32:
            accum = self.accum_expr if self.accum_expr else "D"
            return f"({self.p20_sym} * {self.src_expr} + {accum})"
        elif self.opcode == InterpOpcode.V_INTERP_MOV_F32:
            return f"mov({self.src_expr})"
        return "unknown"


class TestInterpSymbolicAST(unittest.TestCase):
    """Test suite for symbolic AST opcode definitions and typed expressions."""

    def test_attribute_ref_parsing_and_formatting(self) -> None:
        ref = AttributeRef(3, Channel.Y)
        self.assertEqual(ref.name, "attr3.y")
        self.assertEqual(ref.attr_index, 3)
        self.assertEqual(ref.channel, Channel.Y)

        parsed = AttributeRef.parse("attr15.w")
        self.assertEqual(parsed.attr_index, 15)
        self.assertEqual(parsed.channel, Channel.W)

    def test_instruction_disassembly(self) -> None:
        inst_p1 = InterpInstruction(
            opcode=InterpOpcode.V_INTERP_P1_F32,
            dst=2,
            src=Operand(RegType.VGPR, 0),
            attr=AttributeRef(0, Channel.X),
        )
        self.assertEqual(inst_p1.disasm(), "v_interp_p1_f32 v2, v0, attr0.x")

        inst_p2 = InterpInstruction(
            opcode=InterpOpcode.V_INTERP_P2_F32,
            dst=2,
            src=Operand(RegType.VGPR, 1, abs_mod=True, neg_mod=True),
            attr=AttributeRef(0, Channel.X),
            clamp=True,
            omod=1,
        )
        self.assertEqual(inst_p2.disasm(), "v_interp_p2_f32 v2, -|v1|, attr0.x clamp mul:2")

        inst_mov = InterpInstruction(
            opcode=InterpOpcode.V_INTERP_MOV_F32,
            dst=4,
            src=Operand(RegType.PARAM_SEL, ParamSel.P0),
            attr=AttributeRef(2, Channel.Z),
        )
        self.assertEqual(inst_mov.disasm(), "v_interp_mov_f32 v4, p0, attr2.z")

    def test_symbolic_expression_expansion(self) -> None:
        expr_p1 = SymbolicInterpExpr(
            opcode=InterpOpcode.V_INTERP_P1_F32,
            src_expr="i",
            p0_sym="P0",
            p10_sym="P10",
            p20_sym="P20",
        )
        self.assertEqual(expr_p1.to_math_string(), "(P10 * i + P0)")

        expr_p2 = SymbolicInterpExpr(
            opcode=InterpOpcode.V_INTERP_P2_F32,
            src_expr="j",
            p0_sym="P0",
            p10_sym="P10",
            p20_sym="P20",
            accum_expr=expr_p1.to_math_string(),
        )
        self.assertEqual(expr_p2.to_math_string(), "(P20 * j + (P10 * i + P0))")


@dataclass
class ScalarMachineState:
    """Complete architectural state for scalar single-thread RDNA2 execution."""
    vgpr: VGPRFile = field(default_factory=VGPRFile)
    sgpr: List[int] = field(default_factory=lambda: [0] * 106)
    attributes: AttributeFile = field(default_factory=AttributeFile)
    m0: int = 0
    pc: int = 0


class ScalarInterpInterpreter:
    """Deterministic scalar instruction interpreter for RDNA2 VINTRP instructions."""

    def resolve_src(self, operand: Operand, state: ScalarMachineState) -> int:
        if operand.reg_type == RegType.VGPR:
            return state.vgpr.get(int(operand.index_or_val))
        elif operand.reg_type == RegType.SGPR:
            return state.sgpr[int(operand.index_or_val) & 0x7F]
        elif operand.reg_type == RegType.LITERAL:
            if isinstance(operand.index_or_val, float):
                return f32_to_bits(operand.index_or_val)
            return int(operand.index_or_val) & MASK32
        elif operand.reg_type == RegType.PARAM_SEL:
            if isinstance(operand.index_or_val, ParamSel):
                return operand.index_or_val.value
            return int(operand.index_or_val)
        raise ValueError(f"Unknown operand type: {operand.reg_type}")

    def step(self, inst: InterpInstruction, state: ScalarMachineState) -> int:
        chan = state.attributes.get_channel(inst.attr.attr_index, inst.attr.channel.value)
        p0_bits = f32_to_bits(chan.p0)
        p10_bits = f32_to_bits(chan.p10)
        p20_bits = f32_to_bits(chan.p20)

        if inst.opcode == InterpOpcode.V_INTERP_P1_F32:
            s_bits = self.resolve_src(inst.src, state)
            res = oracle_v_interp_p1_f32(
                s_bits=s_bits,
                p10_bits=p10_bits,
                p0_bits=p0_bits,
                abs_s=inst.src.abs_mod,
                neg_s=inst.src.neg_mod,
                clamp=inst.clamp,
                omod=inst.omod,
            )
            state.vgpr.set(inst.dst, res)
            state.pc += 1
            return res

        elif inst.opcode == InterpOpcode.V_INTERP_P2_F32:
            s_bits = self.resolve_src(inst.src, state)
            accum_bits = state.vgpr.get(inst.dst)
            res = oracle_v_interp_p2_f32(
                s_bits=s_bits,
                p20_bits=p20_bits,
                p1_accum_bits=accum_bits,
                abs_s=inst.src.abs_mod,
                neg_s=inst.src.neg_mod,
                clamp=inst.clamp,
                omod=inst.omod,
            )
            state.vgpr.set(inst.dst, res)
            state.pc += 1
            return res

        elif inst.opcode == InterpOpcode.V_INTERP_MOV_F32:
            param_sel = self.resolve_src(inst.src, state)
            res = oracle_v_interp_mov_f32(
                param_sel=param_sel,
                p0_bits=p0_bits,
                p10_bits=p10_bits,
                p20_bits=p20_bits,
                abs_val=inst.src.abs_mod,
                neg_val=inst.src.neg_mod,
                clamp=inst.clamp,
                omod=inst.omod,
            )
            state.vgpr.set(inst.dst, res)
            state.pc += 1
            return res

        raise NotImplementedError(f"Unsupported opcode: {inst.opcode}")

    def run_program(self, instructions: List[InterpInstruction], state: ScalarMachineState) -> None:
        for inst in instructions:
            self.step(inst, state)


class TestScalarInterpInterpreter(unittest.TestCase):
    """Test suite for deterministic scalar instruction interpreter."""

    def setUp(self) -> None:
        self.interp = ScalarInterpInterpreter()
        self.state = ScalarMachineState()

    def test_scalar_p1_execution(self) -> None:
        # attr0.x: v0=1.0, v1=5.0, v2=10.0 -> p0=1.0, p10=4.0, p20=9.0
        self.state.attributes.set_attribute(0, 0, 1.0, 5.0, 10.0)
        # v0 = i = 0.5
        self.state.vgpr.set_f32(0, 0.5)

        inst = InterpInstruction(
            opcode=InterpOpcode.V_INTERP_P1_F32,
            dst=2,
            src=Operand(RegType.VGPR, 0),
            attr=AttributeRef(0, Channel.X),
        )
        res_bits = self.interp.step(inst, self.state)
        # 4.0 * 0.5 + 1.0 = 3.0
        self.assertEqual(bits_to_f32(res_bits), 3.0)
        self.assertEqual(self.state.vgpr.get_f32(2), 3.0)
        self.assertEqual(self.state.pc, 1)

    def test_scalar_p1_then_p2_pipeline(self) -> None:
        # attr0.y: v0=2.0, v1=6.0, v2=14.0 -> p0=2.0, p10=4.0, p20=12.0
        self.state.attributes.set_attribute(0, 1, 2.0, 6.0, 14.0)
        # i = 0.25 (v0), j = 0.5 (v1)
        self.state.vgpr.set_f32(0, 0.25)
        self.state.vgpr.set_f32(1, 0.5)

        program = [
            InterpInstruction(
                opcode=InterpOpcode.V_INTERP_P1_F32,
                dst=2,
                src=Operand(RegType.VGPR, 0),
                attr=AttributeRef(0, Channel.Y),
            ),
            InterpInstruction(
                opcode=InterpOpcode.V_INTERP_P2_F32,
                dst=2,
                src=Operand(RegType.VGPR, 1),
                attr=AttributeRef(0, Channel.Y),
            ),
        ]
        self.interp.run_program(program, self.state)
        # p1: 4.0 * 0.25 + 2.0 = 3.0
        # p2: 12.0 * 0.5 + 3.0 = 9.0
        self.assertEqual(self.state.vgpr.get_f32(2), 9.0)
        self.assertEqual(self.state.pc, 2)

    def test_scalar_mov_flat(self) -> None:
        # attr1.z: v0=7.0, v1=12.0, v2=25.0 -> p0=7.0, p10=5.0, p20=18.0
        self.state.attributes.set_attribute(1, 2, 7.0, 12.0, 25.0)

        inst_p0 = InterpInstruction(
            opcode=InterpOpcode.V_INTERP_MOV_F32,
            dst=3,
            src=Operand(RegType.PARAM_SEL, ParamSel.P0),
            attr=AttributeRef(1, Channel.Z),
        )
        self.interp.step(inst_p0, self.state)
        self.assertEqual(self.state.vgpr.get_f32(3), 7.0)

        inst_p10 = InterpInstruction(
            opcode=InterpOpcode.V_INTERP_MOV_F32,
            dst=4,
            src=Operand(RegType.PARAM_SEL, ParamSel.P10),
            attr=AttributeRef(1, Channel.Z),
        )
        self.interp.step(inst_p10, self.state)
        self.assertEqual(self.state.vgpr.get_f32(4), 5.0)

        inst_p20 = InterpInstruction(
            opcode=InterpOpcode.V_INTERP_MOV_F32,
            dst=5,
            src=Operand(RegType.PARAM_SEL, ParamSel.P20),
            attr=AttributeRef(1, Channel.Z),
        )
        self.interp.step(inst_p20, self.state)
        self.assertEqual(self.state.vgpr.get_f32(5), 18.0)

    def test_scalar_determinism(self) -> None:
        self.state.attributes.set_attribute(2, 0, 3.14159, 2.71828, 1.41421)
        self.state.vgpr.set_f32(10, 0.3333333)
        self.state.vgpr.set_f32(11, 0.6666667)

        prog = [
            InterpInstruction(InterpOpcode.V_INTERP_P1_F32, 12, Operand(RegType.VGPR, 10), AttributeRef(2, Channel.X)),
            InterpInstruction(InterpOpcode.V_INTERP_P2_F32, 12, Operand(RegType.VGPR, 11), AttributeRef(2, Channel.X)),
        ]

        self.interp.run_program(prog, self.state)
        result1 = self.state.vgpr.get(12)

        state2 = ScalarMachineState()
        state2.attributes.set_attribute(2, 0, 3.14159, 2.71828, 1.41421)
        state2.vgpr.set_f32(10, 0.3333333)
        state2.vgpr.set_f32(11, 0.6666667)
        self.interp.run_program(prog, state2)
        result2 = state2.vgpr.get(12)

        self.assertEqual(result1, result2)


@dataclass
class SIMDState:
    """Architectural state for SIMD multi-lane Wave32 / Wave64 wavefront."""
    num_lanes: int = 32
    exec_mask: int = 0xFFFF_FFFF
    lanes_vgpr: List[VGPRFile] = field(default_factory=list)
    sgpr: List[int] = field(default_factory=lambda: [0] * 106)
    attributes: AttributeFile = field(default_factory=AttributeFile)

    def __post_init__(self) -> None:
        if not self.lanes_vgpr:
            self.lanes_vgpr = [VGPRFile() for _ in range(self.num_lanes)]

    def is_lane_active(self, lane: int) -> bool:
        if not (0 <= lane < self.num_lanes):
            return False
        return bool(self.exec_mask & (1 << lane))

    def get_vgpr(self, lane: int, reg_idx: int) -> int:
        return self.lanes_vgpr[lane].get(reg_idx)

    def set_vgpr(self, lane: int, reg_idx: int, val: int) -> None:
        self.lanes_vgpr[lane].set(reg_idx, val)

    def get_vgpr_f32(self, lane: int, reg_idx: int) -> float:
        return self.lanes_vgpr[lane].get_f32(reg_idx)

    def set_vgpr_f32(self, lane: int, reg_idx: int, val: float) -> None:
        self.lanes_vgpr[lane].set_f32(reg_idx, val)


class SIMDInterpInterpreter:
    """SIMD wavefront instruction interpreter supporting multi-lane divergence and EXEC masking."""

    def resolve_lane_src(self, operand: Operand, lane: int, state: SIMDState) -> int:
        if operand.reg_type == RegType.VGPR:
            return state.get_vgpr(lane, int(operand.index_or_val))
        elif operand.reg_type == RegType.SGPR:
            return state.sgpr[int(operand.index_or_val) & 0x7F]
        elif operand.reg_type == RegType.LITERAL:
            if isinstance(operand.index_or_val, float):
                return f32_to_bits(operand.index_or_val)
            return int(operand.index_or_val) & MASK32
        elif operand.reg_type == RegType.PARAM_SEL:
            if isinstance(operand.index_or_val, ParamSel):
                return operand.index_or_val.value
            return int(operand.index_or_val)
        raise ValueError(f"Unknown operand type: {operand.reg_type}")

    def step(self, inst: InterpInstruction, state: SIMDState) -> List[Optional[int]]:
        results: List[Optional[int]] = [None] * state.num_lanes
        chan = state.attributes.get_channel(inst.attr.attr_index, inst.attr.channel.value)
        p0_bits = f32_to_bits(chan.p0)
        p10_bits = f32_to_bits(chan.p10)
        p20_bits = f32_to_bits(chan.p20)

        for lane in range(state.num_lanes):
            if not state.is_lane_active(lane):
                continue

            if inst.opcode == InterpOpcode.V_INTERP_P1_F32:
                s_bits = self.resolve_lane_src(inst.src, lane, state)
                res = oracle_v_interp_p1_f32(
                    s_bits=s_bits,
                    p10_bits=p10_bits,
                    p0_bits=p0_bits,
                    abs_s=inst.src.abs_mod,
                    neg_s=inst.src.neg_mod,
                    clamp=inst.clamp,
                    omod=inst.omod,
                )
                state.set_vgpr(lane, inst.dst, res)
                results[lane] = res

            elif inst.opcode == InterpOpcode.V_INTERP_P2_F32:
                s_bits = self.resolve_lane_src(inst.src, lane, state)
                accum_bits = state.get_vgpr(lane, inst.dst)
                res = oracle_v_interp_p2_f32(
                    s_bits=s_bits,
                    p20_bits=p20_bits,
                    p1_accum_bits=accum_bits,
                    abs_s=inst.src.abs_mod,
                    neg_s=inst.src.neg_mod,
                    clamp=inst.clamp,
                    omod=inst.omod,
                )
                state.set_vgpr(lane, inst.dst, res)
                results[lane] = res

            elif inst.opcode == InterpOpcode.V_INTERP_MOV_F32:
                param_sel = self.resolve_lane_src(inst.src, lane, state)
                res = oracle_v_interp_mov_f32(
                    param_sel=param_sel,
                    p0_bits=p0_bits,
                    p10_bits=p10_bits,
                    p20_bits=p20_bits,
                    abs_val=inst.src.abs_mod,
                    neg_val=inst.src.neg_mod,
                    clamp=inst.clamp,
                    omod=inst.omod,
                )
                state.set_vgpr(lane, inst.dst, res)
                results[lane] = res

        return results

    def run_program(self, instructions: List[InterpInstruction], state: SIMDState) -> None:
        for inst in instructions:
            self.step(inst, state)


class TestSIMDMultiLaneDivergence(unittest.TestCase):
    """Test suite for SIMD multi-lane divergence and 32/64-bit EXEC masking."""

    def setUp(self) -> None:
        self.interp = SIMDInterpInterpreter()

    def test_wave32_divergent_coordinates(self) -> None:
        state = SIMDState(num_lanes=32, exec_mask=0xFFFF_FFFF)
        # Attribute: v0=0.0, v1=32.0, v2=64.0 -> p0=0.0, p10=32.0, p20=64.0
        state.attributes.set_attribute(0, 0, 0.0, 32.0, 64.0)

        # Each lane has divergent i coordinate = lane / 32.0, j coordinate = 0.0
        for lane in range(32):
            state.set_vgpr_f32(lane, 0, lane / 32.0)
            state.set_vgpr_f32(lane, 1, 0.0)

        program = [
            InterpInstruction(InterpOpcode.V_INTERP_P1_F32, 2, Operand(RegType.VGPR, 0), AttributeRef(0, Channel.X)),
            InterpInstruction(InterpOpcode.V_INTERP_P2_F32, 2, Operand(RegType.VGPR, 1), AttributeRef(0, Channel.X)),
        ]
        self.interp.run_program(program, state)

        # Expected result in lane k: 32.0 * (k / 32.0) + 0.0 = float(k)
        for lane in range(32):
            self.assertAlmostEqual(state.get_vgpr_f32(lane, 2), float(lane), places=5)

    def test_wave64_exec_masking(self) -> None:
        state = SIMDState(num_lanes=64, exec_mask=0xFFFF_FFFF_FFFF_FFFF)
        state.attributes.set_attribute(1, 0, 10.0, 20.0, 30.0)

        for lane in range(64):
            state.set_vgpr_f32(lane, 0, 0.5)
            state.set_vgpr_f32(lane, 1, 0.25)
            state.set_vgpr_f32(lane, 2, -999.0)  # Sentinel

        # Only upper 32 lanes active
        state.exec_mask = 0xFFFF_FFFF_0000_0000

        program = [
            InterpInstruction(InterpOpcode.V_INTERP_P1_F32, 2, Operand(RegType.VGPR, 0), AttributeRef(1, Channel.X)),
            InterpInstruction(InterpOpcode.V_INTERP_P2_F32, 2, Operand(RegType.VGPR, 1), AttributeRef(1, Channel.X)),
        ]
        self.interp.run_program(program, state)

        # Inactive lanes 0..31 must retain sentinel -999.0
        for lane in range(32):
            self.assertEqual(state.get_vgpr_f32(lane, 2), -999.0)

        # Active lanes 32..63: p0=10, p10=10, p20=20
        # p1: 10 * 0.5 + 10 = 15.0
        # p2: 20 * 0.25 + 15 = 20.0
        for lane in range(32, 64):
            self.assertAlmostEqual(state.get_vgpr_f32(lane, 2), 20.0, places=5)

    def test_alternating_lanes_exec_mask(self) -> None:
        state = SIMDState(num_lanes=32, exec_mask=0x5555_5555)  # Even lanes active
        state.attributes.set_attribute(0, 0, 1.0, 2.0, 3.0)

        for lane in range(32):
            state.set_vgpr_f32(lane, 0, 1.0)
            state.set_vgpr_f32(lane, 5, 777.0)

        inst = InterpInstruction(InterpOpcode.V_INTERP_P1_F32, 5, Operand(RegType.VGPR, 0), AttributeRef(0, Channel.X))
        self.interp.step(inst, state)

        for lane in range(32):
            if lane % 2 == 0:
                self.assertEqual(state.get_vgpr_f32(lane, 5), 2.0)
            else:
                self.assertEqual(state.get_vgpr_f32(lane, 5), 777.0)

    def test_zero_exec_mask(self) -> None:
        state = SIMDState(num_lanes=32, exec_mask=0x0000_0000)
        state.attributes.set_attribute(0, 0, 1.0, 2.0, 3.0)
        for lane in range(32):
            state.set_vgpr_f32(lane, 3, 123.0)

        inst = InterpInstruction(InterpOpcode.V_INTERP_P1_F32, 3, Operand(RegType.VGPR, 0), AttributeRef(0, Channel.X))
        results = self.interp.step(inst, state)

        self.assertTrue(all(r is None for r in results))
        for lane in range(32):
            self.assertEqual(state.get_vgpr_f32(lane, 3), 123.0)


class TestRegisterAliasingAndPreservation(unittest.TestCase):
    """Test suite for register aliasing and in-place operand preservation."""

    def setUp(self) -> None:
        self.scalar_interp = ScalarInterpInterpreter()
        self.simd_interp = SIMDInterpInterpreter()

    def test_inplace_p1_aliased_src_dst(self) -> None:
        # v_interp_p1_f32 v0, v0, attr0.x
        # Reading coordinate from v0 and writing result to v0
        state = ScalarMachineState()
        state.attributes.set_attribute(0, 0, 10.0, 5.0, 20.0)  # p0=10.0, p10=-5.0, p20=10.0
        state.vgpr.set_f32(0, 2.0)  # s = 2.0

        inst = InterpInstruction(
            opcode=InterpOpcode.V_INTERP_P1_F32,
            dst=0,
            src=Operand(RegType.VGPR, 0),
            attr=AttributeRef(0, Channel.X),
        )
        res = self.scalar_interp.step(inst, state)
        # -5.0 * 2.0 + 10.0 = 0.0
        self.assertEqual(bits_to_f32(res), 0.0)
        self.assertEqual(state.vgpr.get_f32(0), 0.0)

    def test_inplace_p2_aliased_src_dst(self) -> None:
        # v_interp_p2_f32 v0, v0, attr0.y
        # Where v0 acts as both coordinate S and accumulator D!
        # D_new = P20 * v0_old + v0_old
        state = ScalarMachineState()
        state.attributes.set_attribute(0, 1, 0.0, 1.0, 4.0)  # p20 = 4.0
        state.vgpr.set_f32(0, 3.0)  # v0 = 3.0

        inst = InterpInstruction(
            opcode=InterpOpcode.V_INTERP_P2_F32,
            dst=0,
            src=Operand(RegType.VGPR, 0),
            attr=AttributeRef(0, Channel.Y),
        )
        res = self.scalar_interp.step(inst, state)
        # 4.0 * 3.0 + 3.0 = 15.0
        self.assertEqual(bits_to_f32(res), 15.0)
        self.assertEqual(state.vgpr.get_f32(0), 15.0)

    def test_adjacent_register_preservation(self) -> None:
        state = ScalarMachineState()
        state.attributes.set_attribute(1, 0, 1.0, 2.0, 3.0)
        state.vgpr.set_f32(0, 111.0)
        state.vgpr.set_f32(1, 0.5)
        state.vgpr.set_f32(2, 222.0)
        state.vgpr.set_f32(3, 333.0)

        # Write to v1 using v1 as coordinate
        inst = InterpInstruction(
            opcode=InterpOpcode.V_INTERP_P1_F32,
            dst=1,
            src=Operand(RegType.VGPR, 1),
            attr=AttributeRef(1, Channel.X),
        )
        self.scalar_interp.step(inst, state)

        # v0, v2, v3 must remain exactly preserved
        self.assertEqual(state.vgpr.get_f32(0), 111.0)
        self.assertEqual(state.vgpr.get_f32(2), 222.0)
        self.assertEqual(state.vgpr.get_f32(3), 333.0)
        # v1 was updated: p10 * 0.5 + p0 = 1.0 * 0.5 + 1.0 = 1.5
        self.assertEqual(state.vgpr.get_f32(1), 1.5)

    def test_pipelined_accumulator_chain(self) -> None:
        state = ScalarMachineState()
        # attr0.z: p0=5.0, p10=2.0, p20=8.0
        state.attributes.set_attribute(0, 2, 5.0, 7.0, 13.0)
        # v0 = i = 0.5, v1 = j = 0.25
        state.vgpr.set_f32(0, 0.5)
        state.vgpr.set_f32(1, 0.25)
        state.vgpr.set_f32(2, 999.0)  # Initial uninitialized value in destination

        # Step 1: p1 writes to v2
        p1 = InterpInstruction(InterpOpcode.V_INTERP_P1_F32, 2, Operand(RegType.VGPR, 0), AttributeRef(0, Channel.Z))
        self.scalar_interp.step(p1, state)
        # v2 = 2.0 * 0.5 + 5.0 = 6.0
        self.assertEqual(state.vgpr.get_f32(2), 6.0)

        # Step 2: p2 reads v2 and accumulates into v2
        p2 = InterpInstruction(InterpOpcode.V_INTERP_P2_F32, 2, Operand(RegType.VGPR, 1), AttributeRef(0, Channel.Z))
        self.scalar_interp.step(p2, state)
        # v2 = 8.0 * 0.25 + 6.0 = 8.0
        self.assertEqual(state.vgpr.get_f32(2), 8.0)

    def test_simd_multi_lane_aliased_inplace(self) -> None:
        state = SIMDState(num_lanes=32, exec_mask=0xFFFF_FFFF)
        # attr1.w: p0=0.0, p10=1.0, p20=2.0
        state.attributes.set_attribute(1, 3, 0.0, 1.0, 2.0)

        for lane in range(32):
            # v0 = lane float value
            state.set_vgpr_f32(lane, 0, float(lane))

        # Each lane executes in-place v_interp_p1_f32 v0, v0, attr1.w
        inst = InterpInstruction(InterpOpcode.V_INTERP_P1_F32, 0, Operand(RegType.VGPR, 0), AttributeRef(1, Channel.W))
        self.simd_interp.step(inst, state)

        for lane in range(32):
            # p10 * lane + p0 = 1.0 * lane + 0.0 = float(lane)
            self.assertEqual(state.get_vgpr_f32(lane, 0), float(lane))


@dataclass(frozen=True)
class FPStatusFlags:
    """Architectural floating-point status flags per IEEE 754 and RDNA2 shader core."""
    invalid: bool = False
    div_by_zero: bool = False
    overflow: bool = False
    underflow: bool = False
    inexact: bool = False
    negative: bool = False
    zero: bool = False
    denormal: bool = False
    infinite: bool = False
    nan: bool = False


def is_subnormal_f32(bits: int) -> bool:
    return ((bits & EXP_MASK_F32) == 0) and ((bits & FRAC_MASK_F32) != 0)


def evaluate_f32_interp_flags(
    s_bits: int,
    p_param_bits: int,
    addend_bits: int,
    res_bits: int,
) -> FPStatusFlags:
    """Evaluate architectural status flags for fused multiply-add (p_param * s + addend)."""
    s_nan = is_nan_f32(s_bits)
    p_nan = is_nan_f32(p_param_bits)
    a_nan = is_nan_f32(addend_bits)
    s_inf = is_inf_f32(s_bits)
    p_inf = is_inf_f32(p_param_bits)
    a_inf = is_inf_f32(addend_bits)

    s_zero = is_zero_f32(s_bits)
    p_zero = is_zero_f32(p_param_bits)

    # 1. Invalid operation
    invalid = False
    if s_nan or p_nan or a_nan:
        invalid = True
    elif (s_inf and p_zero) or (p_inf and s_zero):
        invalid = True
    elif (s_inf or p_inf) and a_inf:
        prod_sign = ((s_bits ^ p_param_bits) & SIGN_MASK_F32)
        add_sign = (addend_bits & SIGN_MASK_F32)
        if prod_sign != add_sign:
            invalid = True

    # 2. Infinite & NaN
    res_nan = is_nan_f32(res_bits)
    res_inf = is_inf_f32(res_bits)

    # 3. Overflow
    overflow = False
    if res_inf and not (s_inf or p_inf or a_inf):
        overflow = True

    # 4. Denormal & Underflow
    denormal = (
        is_subnormal_f32(s_bits)
        or is_subnormal_f32(p_param_bits)
        or is_subnormal_f32(addend_bits)
        or is_subnormal_f32(res_bits)
    )
    underflow = is_subnormal_f32(res_bits)

    # 5. Zero & Negative
    zero = is_zero_f32(res_bits)
    negative = bool(res_bits & SIGN_MASK_F32) and not res_nan

    # 6. Inexact (approximate check: if overflow, underflow, or fractional rounding occurred)
    inexact = overflow or underflow
    if not (invalid or res_nan or res_inf):
        # Check against high-precision arithmetic
        s_val = bits_to_f32(s_bits)
        p_val = bits_to_f32(p_param_bits)
        a_val = bits_to_f32(addend_bits)
        exact = p_val * s_val + a_val
        inexact = inexact or (exact != bits_to_f32(res_bits))

    return FPStatusFlags(
        invalid=invalid,
        overflow=overflow,
        underflow=underflow,
        inexact=inexact,
        negative=negative,
        zero=zero,
        denormal=denormal,
        infinite=res_inf,
        nan=res_nan,
    )


def compute_f32_mantissa_carry(s0_bits: int, s1_bits: int) -> Tuple[int, int]:
    """Calculate internal mantissa summation and carry-out for FP normalization."""
    # Extract mantissas with implicit leading 1 for normalized floats
    exp0 = (s0_bits & EXP_MASK_F32) >> 23
    exp1 = (s1_bits & EXP_MASK_F32) >> 23

    m0 = (s0_bits & FRAC_MASK_F32) | (0x0080_0000 if exp0 != 0 else 0)
    m1 = (s1_bits & FRAC_MASK_F32) | (0x0080_0000 if exp1 != 0 else 0)

    # Align mantissas if exponents differ
    shift = exp0 - exp1
    if shift > 0:
        m1 >>= shift
    elif shift < 0:
        m0 >>= (-shift)

    sum_m = m0 + m1
    carry_out = 1 if (sum_m >= 0x0100_0000) else 0
    return sum_m, carry_out


class TestConditionCodesAndStatusFlags(unittest.TestCase):
    """Test suite for floating-point status flags, condition codes, and carry generation."""

    def test_exact_finite_flags(self) -> None:
        s = f32_to_bits(2.0)
        p = f32_to_bits(3.0)
        a = f32_to_bits(4.0)
        res = oracle_v_interp_p1_f32(s, p, a)
        flags = evaluate_f32_interp_flags(s, p, a, res)

        self.assertFalse(flags.invalid)
        self.assertFalse(flags.overflow)
        self.assertFalse(flags.underflow)
        self.assertFalse(flags.inexact)
        self.assertFalse(flags.negative)
        self.assertFalse(flags.zero)
        self.assertFalse(flags.denormal)
        self.assertFalse(flags.infinite)
        self.assertFalse(flags.nan)

    def test_inexact_flag(self) -> None:
        s = f32_to_bits(1.0)
        p = f32_to_bits(1e-7)
        a = f32_to_bits(1.0)
        res = oracle_v_interp_p1_f32(s, p, a)
        flags = evaluate_f32_interp_flags(s, p, a, res)

        self.assertTrue(flags.inexact)
        self.assertFalse(flags.overflow)
        self.assertFalse(flags.underflow)

    def test_overflow_to_infinity_flag(self) -> None:
        flt_max = 0x7F7F_FFFF  # ~3.4028235e+38
        s = f32_to_bits(2.0)
        p = flt_max
        a = f32_to_bits(0.0)
        res = oracle_v_interp_p1_f32(s, p, a)
        flags = evaluate_f32_interp_flags(s, p, a, res)

        self.assertTrue(flags.overflow)
        self.assertTrue(flags.infinite)
        self.assertTrue(flags.inexact)
        self.assertFalse(flags.invalid)

    def test_underflow_and_denormal_flags(self) -> None:
        subnorm = 0x0000_0001
        s = f32_to_bits(1.0)
        p = subnorm
        a = f32_to_bits(0.0)
        res = oracle_v_interp_p1_f32(s, p, a)
        flags = evaluate_f32_interp_flags(s, p, a, res)

        self.assertTrue(flags.denormal)
        self.assertTrue(flags.underflow)
        self.assertFalse(flags.overflow)

    def test_invalid_operation_flag(self) -> None:
        # 0.0 * inf is invalid
        s = f32_to_bits(0.0)
        p = POS_INF_F32
        a = f32_to_bits(1.0)
        res = oracle_v_interp_p1_f32(s, p, a)
        flags = evaluate_f32_interp_flags(s, p, a, res)

        self.assertTrue(flags.invalid)
        self.assertTrue(flags.nan)

    def test_zero_and_negative_flags(self) -> None:
        s = f32_to_bits(0.0)
        p = f32_to_bits(0.0)
        a = f32_to_bits(0.0)
        res = oracle_v_interp_p1_f32(s, p, a)
        flags = evaluate_f32_interp_flags(s, p, a, res)
        self.assertTrue(flags.zero)
        self.assertFalse(flags.negative)

        # negative result
        s_neg = f32_to_bits(-5.0)
        p_neg = f32_to_bits(2.0)
        a_neg = f32_to_bits(0.0)
        res_neg = oracle_v_interp_p1_f32(s_neg, p_neg, a_neg)
        flags_neg = evaluate_f32_interp_flags(s_neg, p_neg, a_neg, res_neg)
        self.assertFalse(flags_neg.zero)
        self.assertTrue(flags_neg.negative)

    def test_mantissa_carry_generation(self) -> None:
        # 1.0 + 1.0 -> mantissas are 1.0 + 1.0 = 2.0 -> carry out = 1
        _, carry1 = compute_f32_mantissa_carry(f32_to_bits(1.0), f32_to_bits(1.0))
        self.assertEqual(carry1, 1)

        # 1.0 + 0.25 -> mantissas sum is 1.25 < 2.0 -> carry out = 0
        _, carry0 = compute_f32_mantissa_carry(f32_to_bits(1.0), f32_to_bits(0.25))
        self.assertEqual(carry0, 0)


class TestIEEE754BoundaryExtremes(unittest.TestCase):
    """Test suite for IEEE 754 boundary extremes, subnormals, and NaN handling."""

    def test_nan_clamping_to_positive_zero(self) -> None:
        # In RDNA2, when clamp modifier is active, any NaN result must clamp to +0.0
        qnan = CANONICAL_QNAN_F32
        p0 = f32_to_bits(1.0)
        p10 = f32_to_bits(2.0)

        # NaN coordinate without clamp produces NaN
        res_no_clamp = oracle_v_interp_p1_f32(qnan, p10, p0, clamp=False)
        self.assertTrue(is_nan_f32(res_no_clamp))

        # NaN coordinate WITH clamp produces exact POS_ZERO (+0.0)
        res_clamped = oracle_v_interp_p1_f32(qnan, p10, p0, clamp=True)
        self.assertEqual(res_clamped, POS_ZERO_F32)

        # NaN in p2 with clamp produces exact POS_ZERO
        res_p2_clamped = oracle_v_interp_p2_f32(qnan, p10, p0, clamp=True)
        self.assertEqual(res_p2_clamped, POS_ZERO_F32)

        # NaN in mov with clamp produces POS_ZERO
        res_mov_clamped = oracle_v_interp_mov_f32(0, qnan, p10, p0, clamp=True)
        self.assertEqual(res_mov_clamped, POS_ZERO_F32)

    def test_infinity_clamping(self) -> None:
        # +inf clamped produces 1.0
        # -inf clamped produces +0.0
        p0 = f32_to_bits(0.0)
        p10 = f32_to_bits(1.0)

        res_pos_inf = oracle_v_interp_p1_f32(POS_INF_F32, p10, p0, clamp=True)
        self.assertEqual(res_pos_inf, ONE_F32)

        res_neg_inf = oracle_v_interp_p1_f32(NEG_INF_F32, p10, p0, clamp=True)
        self.assertEqual(res_neg_inf, POS_ZERO_F32)

    def test_signed_zero_rules(self) -> None:
        # -0.0 * 2.0 + (-0.0) -> -0.0
        p0_neg = NEG_ZERO_F32
        p10 = f32_to_bits(2.0)
        s_neg_zero = NEG_ZERO_F32

        res = oracle_v_interp_p1_f32(s_neg_zero, p10, p0_neg)
        self.assertEqual(res, NEG_ZERO_F32)

        # -0.0 + (+0.0) -> +0.0 per IEEE 754 RNE
        res_pos = oracle_v_interp_p1_f32(s_neg_zero, p10, POS_ZERO_F32)
        self.assertEqual(res_pos, POS_ZERO_F32)

        # Clamping -0.0 produces +0.0
        res_clamp = oracle_v_interp_p1_f32(s_neg_zero, p10, p0_neg, clamp=True)
        self.assertEqual(res_clamp, POS_ZERO_F32)

    def test_subnormal_precision(self) -> None:
        # Smallest subnormal: 0x00000001 = 2**-149
        subnorm = 0x0000_0001
        s = f32_to_bits(2.0)
        p0 = POS_ZERO_F32

        # 2.0 * 2**-149 = 2**-148 (0x00000002)
        res = oracle_v_interp_p1_f32(s, subnorm, p0)
        self.assertEqual(res, 0x0000_0002)

    def test_signaling_nan_handling(self) -> None:
        # Signaling NaN: exponent all 1s, non-zero fraction with MSB fraction = 0
        snan = 0x7F80_0001
        p0 = f32_to_bits(1.0)
        p10 = f32_to_bits(2.0)

        res = oracle_v_interp_p1_f32(snan, p10, p0)
        self.assertTrue(is_nan_f32(res))

    def test_flt_max_overflow(self) -> None:
        flt_max = 0x7F7F_FFFF  # ~3.4028235e+38
        s = f32_to_bits(2.0)
        p0 = POS_ZERO_F32

        # Overflows to +inf
        res_unclamped = oracle_v_interp_p1_f32(s, flt_max, p0)
        self.assertEqual(res_unclamped, POS_INF_F32)

        # Clamped overflows saturate to 1.0
        res_clamped = oracle_v_interp_p1_f32(s, flt_max, p0, clamp=True)
        self.assertEqual(res_clamped, ONE_F32)

    def test_invalid_operation_nan_generation(self) -> None:
        # 0.0 * inf is invalid, resulting in canonical NaN
        s_zero = POS_ZERO_F32
        p_inf = POS_INF_F32
        a_zero = POS_ZERO_F32

        res = oracle_v_interp_p1_f32(s_zero, p_inf, a_zero)
        self.assertEqual(res, CANONICAL_QNAN_F32)


class TestNegativeControlsAndSyntaxRejection(unittest.TestCase):
    """Negative controls, bounds enforcement, and syntax rejection tests."""

    def test_out_of_bounds_attribute_indices(self) -> None:
        with self.assertRaises(ValueError):
            AttributeRef(32, Channel.X)
        with self.assertRaises(ValueError):
            AttributeRef(-1, Channel.Y)

    def test_invalid_attribute_syntax_parsing(self) -> None:
        invalid_syntaxes = [
            "attr",
            "v0.x",
            "attr0",
            "attr0.",
            "attr0.q",
            "attr32.x",
            "attribute0.x",
        ]
        for s in invalid_syntaxes:
            with self.subTest(syntax=s):
                with self.assertRaises(ValueError):
                    AttributeRef.parse(s)

    def test_invalid_param_selector(self) -> None:
        with self.assertRaises(ValueError):
            ParamSel.from_str("p3")
        with self.assertRaises(ValueError):
            ParamSel.from_str("invalid")
        with self.assertRaises(ValueError):
            oracle_v_interp_mov_f32(99, 0, 0, 0)

    def test_invalid_omod(self) -> None:
        with self.assertRaises(ValueError):
            apply_f32_omod(ONE_F32, 4)
        with self.assertRaises(ValueError):
            apply_f32_omod(ONE_F32, -1)


class TestMutationDetection(unittest.TestCase):
    """Mutation testing suite asserting that semantic mutations are strictly detected."""

    def test_detect_addition_mutant(self) -> None:
        # Reference: P10 * S + P0
        # Mutant: (P10 + S) + P0
        s = f32_to_bits(2.0)
        p10 = f32_to_bits(3.0)
        p0 = f32_to_bits(4.0)

        correct = oracle_v_interp_p1_f32(s, p10, p0)  # 3*2 + 4 = 10.0
        mutant = f32_to_bits((3.0 + 2.0) + 4.0)       # 9.0
        self.assertNotEqual(correct, mutant)

    def test_detect_missing_p0_base_mutant(self) -> None:
        # Reference: P10 * S + P0
        # Mutant: P10 * S
        s = f32_to_bits(2.0)
        p10 = f32_to_bits(3.0)
        p0 = f32_to_bits(5.0)

        correct = oracle_v_interp_p1_f32(s, p10, p0)  # 3*2 + 5 = 11.0
        mutant = f32_to_bits(3.0 * 2.0)              # 6.0
        self.assertNotEqual(correct, mutant)

    def test_detect_accumulator_swap_mutant(self) -> None:
        # Reference p2: P20 * S + D
        # Mutant p2: P10 * S + D
        s = f32_to_bits(0.5)
        p20 = f32_to_bits(8.0)
        p10 = f32_to_bits(2.0)
        d = f32_to_bits(10.0)

        correct = oracle_v_interp_p2_f32(s, p20, d)   # 8*0.5 + 10 = 14.0
        mutant = oracle_v_interp_p2_f32(s, p10, d)    # 2*0.5 + 10 = 11.0
        self.assertNotEqual(correct, mutant)

    def test_detect_sign_inversion_mutant(self) -> None:
        # Reference: P20 * S + D
        # Mutant: -P20 * S + D
        s = f32_to_bits(0.5)
        p20 = f32_to_bits(4.0)
        d = f32_to_bits(10.0)

        correct = oracle_v_interp_p2_f32(s, p20, d)   # 4*0.5 + 10 = 12.0
        mutant = oracle_v_interp_p2_f32(s, p20, d, neg_s=True)  # 4*(-0.5) + 10 = 8.0
        self.assertNotEqual(correct, mutant)

    def test_detect_nan_clamp_omission_mutant(self) -> None:
        # In RDNA2, clamping NaN must yield POS_ZERO (0x00000000).
        # A mutant that leaves NaN unchanged must be detected.
        qnan = CANONICAL_QNAN_F32
        p0 = f32_to_bits(1.0)
        p10 = f32_to_bits(2.0)

        correct = oracle_v_interp_p1_f32(qnan, p10, p0, clamp=True)
        self.assertEqual(correct, POS_ZERO_F32)

        mutant_no_clamp = oracle_v_interp_p1_f32(qnan, p10, p0, clamp=False)
        self.assertNotEqual(correct, mutant_no_clamp)


if __name__ == "__main__":
    unittest.main()
