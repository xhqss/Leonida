"""Semantic interpreter tests and reference mathematical oracle for RDNA2 v_cmp_class.

Models IEEE 754-2008 / IEEE 754-2019 floating-point classification and AMD RDNA2 / GFX10
Vector Comparison (VOPC / VOP3) architectural semantics:
  - v_cmp_class_f32: D.mask[lane] = (class(src0.f32) & src1.u32) != 0
  - v_cmp_class_f64: D.mask[lane] = (class(src0.f64) & src1.u32) != 0

RDNA2 Floating-Point Classification Bit Layout (10-bit bitmask):
  - Bit 0 (0x001): Signaling NaN (sNaN)
  - Bit 1 (0x002): Quiet NaN (qNaN)
  - Bit 2 (0x004): Negative Infinity (-INF)
  - Bit 3 (0x008): Negative Normal
  - Bit 4 (0x010): Negative Subnormal / Denormal
  - Bit 5 (0x020): Negative Zero (-0.0)
  - Bit 6 (0x040): Positive Zero (+0.0)
  - Bit 7 (0x080): Positive Subnormal / Denormal
  - Bit 8 (0x100): Positive Normal
  - Bit 9 (0x200): Positive Infinity (+INF)

Standard library Python only; zero third-party dependencies; zero game data.
Python 3.9+ compatible (from __future__ import annotations).
"""
from __future__ import annotations

import dataclasses
import enum
import math
import struct
import unittest

MASK32: int = 0xFFFFFFFF
MASK64: int = 0xFFFFFFFFFFFFFFFF

# Float32 masks and constants
SIGN_MASK_F32: int = 0x80000000
EXP_MASK_F32: int = 0x7F800000
MANT_MASK_F32: int = 0x007FFFFF
QUIET_NAN_BIT_F32: int = 0x00400000

POS_ZERO_F32: int = 0x00000000
NEG_ZERO_F32: int = 0x80000000
POS_ONE_F32: int = 0x3F800000
NEG_ONE_F32: int = 0xBF800000
POS_INF_F32: int = 0x7F800000
NEG_INF_F32: int = 0xFF800000
QNAN_F32: int = 0x7FC00000
SNAN_F32: int = 0x7F800001
DENORM_MIN_F32: int = 0x00000001
DENORM_MAX_F32: int = 0x007FFFFF
NEG_DENORM_MIN_F32: int = 0x80000001
NEG_DENORM_MAX_F32: int = 0x807FFFFF
FLT_MIN_F32: int = 0x00800000
FLT_MAX_F32: int = 0x7F7FFFFF
NEG_FLT_MIN_F32: int = 0x80800000
NEG_FLT_MAX_F32: int = 0xFF7FFFFF

# Float64 masks and constants
SIGN_MASK_F64: int = 0x8000000000000000
EXP_MASK_F64: int = 0x7FF0000000000000
MANT_MASK_F64: int = 0x000FFFFFFFFFFFFF
QUIET_NAN_BIT_F64: int = 0x0008000000000000

POS_ZERO_F64: int = 0x0000000000000000
NEG_ZERO_F64: int = 0x8000000000000000
POS_ONE_F64: int = 0x3FF0000000000000
NEG_ONE_F64: int = 0xBFF0000000000000
POS_INF_F64: int = 0x7FF0000000000000
NEG_INF_F64: int = 0xFFF0000000000000
QNAN_F64: int = 0x7FF8000000000000
SNAN_F64: int = 0x7FF0000000000001
DENORM_MIN_F64: int = 0x0000000000000001
DENORM_MAX_F64: int = 0x000FFFFFFFFFFFFF
NEG_DENORM_MIN_F64: int = 0x8000000000000001
NEG_DENORM_MAX_F64: int = 0x800FFFFFFFFFFFFF
DBL_MIN_F64: int = 0x0010000000000000
DBL_MAX_F64: int = 0x7FEFFFFFFFFFFFFF
NEG_DBL_MIN_F64: int = 0x8010000000000000
NEG_DBL_MAX_F64: int = 0xFFEFFFFFFFFFFFFF

# 10 IEEE 754 Floating-Point Class Bit Flags
CLASS_SNAN: int = 1 << 0          # 0x001
CLASS_QNAN: int = 1 << 1          # 0x002
CLASS_NEG_INF: int = 1 << 2       # 0x004
CLASS_NEG_NORMAL: int = 1 << 3    # 0x008
CLASS_NEG_DENORMAL: int = 1 << 4  # 0x010
CLASS_NEG_ZERO: int = 1 << 5      # 0x020
CLASS_POS_ZERO: int = 1 << 6      # 0x040
CLASS_POS_DENORMAL: int = 1 << 7  # 0x080
CLASS_POS_NORMAL: int = 1 << 8    # 0x100
CLASS_POS_INF: int = 1 << 9       # 0x200

# Canonical composite class masks
CLASS_MASK_NAN: int = CLASS_SNAN | CLASS_QNAN
CLASS_MASK_INF: int = CLASS_NEG_INF | CLASS_POS_INF
CLASS_MASK_ZERO: int = CLASS_NEG_ZERO | CLASS_POS_ZERO
CLASS_MASK_DENORMAL: int = CLASS_NEG_DENORMAL | CLASS_POS_DENORMAL
CLASS_MASK_NORMAL: int = CLASS_NEG_NORMAL | CLASS_POS_NORMAL
CLASS_MASK_FINITE: int = CLASS_MASK_ZERO | CLASS_MASK_DENORMAL | CLASS_MASK_NORMAL
CLASS_MASK_ALL: int = (1 << 10) - 1  # 0x3FF


def apply_f32_modifiers(bits: int, neg: bool = False, abs_val: bool = False) -> int:
    """Applies VOP3 input modifiers: abs (|x|) first, then neg (-x)."""
    bits &= MASK32
    if abs_val:
        bits &= ~SIGN_MASK_F32
    if neg:
        bits ^= SIGN_MASK_F32
    return bits & MASK32


def apply_f64_modifiers(bits: int, neg: bool = False, abs_val: bool = False) -> int:
    """Applies VOP3 input modifiers: abs (|x|) first, then neg (-x)."""
    bits &= MASK64
    if abs_val:
        bits &= ~SIGN_MASK_F64
    if neg:
        bits ^= SIGN_MASK_F64
    return bits & MASK64


def classify_f32(bits: int) -> int:
    """Classifies an IEEE 754 binary32 float and returns a single class bit."""
    u = bits & MASK32
    sign = (u & SIGN_MASK_F32) != 0
    exp_val = (u & EXP_MASK_F32) >> 23
    mant = u & MANT_MASK_F32

    if exp_val == 0xFF:
        if mant == 0:
            return CLASS_NEG_INF if sign else CLASS_POS_INF
        is_quiet = (mant & QUIET_NAN_BIT_F32) != 0
        return CLASS_QNAN if is_quiet else CLASS_SNAN

    if exp_val == 0:
        if mant == 0:
            return CLASS_NEG_ZERO if sign else CLASS_POS_ZERO
        return CLASS_NEG_DENORMAL if sign else CLASS_POS_DENORMAL

    return CLASS_NEG_NORMAL if sign else CLASS_POS_NORMAL


def classify_f64(bits: int) -> int:
    """Classifies an IEEE 754 binary64 double and returns a single class bit."""
    u = bits & MASK64
    sign = (u & SIGN_MASK_F64) != 0
    exp_val = (u & EXP_MASK_F64) >> 52
    mant = u & MANT_MASK_F64

    if exp_val == 0x7FF:
        if mant == 0:
            return CLASS_NEG_INF if sign else CLASS_POS_INF
        is_quiet = (mant & QUIET_NAN_BIT_F64) != 0
        return CLASS_QNAN if is_quiet else CLASS_SNAN

    if exp_val == 0:
        if mant == 0:
            return CLASS_NEG_ZERO if sign else CLASS_POS_ZERO
        return CLASS_NEG_DENORMAL if sign else CLASS_POS_DENORMAL

    return CLASS_NEG_NORMAL if sign else CLASS_POS_NORMAL


def ref_cmp_class_f32(src0_bits: int, mask: int, neg: bool = False, abs_val: bool = False) -> bool:
    """Reference oracle for v_cmp_class_f32."""
    val = apply_f32_modifiers(src0_bits, neg=neg, abs_val=abs_val)
    cls = classify_f32(val)
    return bool(cls & (mask & MASK32))


def ref_cmp_class_f64(src0_bits: int, mask: int, neg: bool = False, abs_val: bool = False) -> bool:
    """Reference oracle for v_cmp_class_f64."""
    val = apply_f64_modifiers(src0_bits, neg=neg, abs_val=abs_val)
    cls = classify_f64(val)
    return bool(cls & (mask & MASK32))


class TestValuCmpClassOracle(unittest.TestCase):
    """Unit tests for the reference mathematical oracle and architectural model."""

    def test_classify_all_f32_classes(self) -> None:
        expected = [
            (SNAN_F32, CLASS_SNAN),
            (QNAN_F32, CLASS_QNAN),
            (NEG_INF_F32, CLASS_NEG_INF),
            (NEG_ONE_F32, CLASS_NEG_NORMAL),
            (NEG_DENORM_MIN_F32, CLASS_NEG_DENORMAL),
            (NEG_ZERO_F32, CLASS_NEG_ZERO),
            (POS_ZERO_F32, CLASS_POS_ZERO),
            (DENORM_MIN_F32, CLASS_POS_DENORMAL),
            (POS_ONE_F32, CLASS_POS_NORMAL),
            (POS_INF_F32, CLASS_POS_INF),
        ]
        for bits, cls in expected:
            with self.subTest(bits=f"{bits:#010x}", expected=f"{cls:#05x}"):
                self.assertEqual(classify_f32(bits), cls)

    def test_classify_all_f64_classes(self) -> None:
        expected = [
            (SNAN_F64, CLASS_SNAN),
            (QNAN_F64, CLASS_QNAN),
            (NEG_INF_F64, CLASS_NEG_INF),
            (NEG_ONE_F64, CLASS_NEG_NORMAL),
            (NEG_DENORM_MIN_F64, CLASS_NEG_DENORMAL),
            (NEG_ZERO_F64, CLASS_NEG_ZERO),
            (POS_ZERO_F64, CLASS_POS_ZERO),
            (DENORM_MIN_F64, CLASS_POS_DENORMAL),
            (POS_ONE_F64, CLASS_POS_NORMAL),
            (POS_INF_F64, CLASS_POS_INF),
        ]
        for bits, cls in expected:
            with self.subTest(bits=f"{bits:#018x}", expected=f"{cls:#05x}"):
                self.assertEqual(classify_f64(bits), cls)

    def test_ref_cmp_class_f32_composite_masks(self) -> None:
        self.assertTrue(ref_cmp_class_f32(POS_ZERO_F32, CLASS_MASK_ZERO))
        self.assertTrue(ref_cmp_class_f32(NEG_ZERO_F32, CLASS_MASK_ZERO))
        self.assertFalse(ref_cmp_class_f32(POS_ONE_F32, CLASS_MASK_ZERO))

        self.assertTrue(ref_cmp_class_f32(SNAN_F32, CLASS_MASK_NAN))
        self.assertTrue(ref_cmp_class_f32(QNAN_F32, CLASS_MASK_NAN))
        self.assertFalse(ref_cmp_class_f32(POS_INF_F32, CLASS_MASK_NAN))

        self.assertTrue(ref_cmp_class_f32(POS_INF_F32, CLASS_MASK_INF))
        self.assertTrue(ref_cmp_class_f32(NEG_INF_F32, CLASS_MASK_INF))
        self.assertFalse(ref_cmp_class_f32(QNAN_F32, CLASS_MASK_INF))

    def test_ref_cmp_class_f64_composite_masks(self) -> None:
        self.assertTrue(ref_cmp_class_f64(POS_ZERO_F64, CLASS_MASK_ZERO))
        self.assertTrue(ref_cmp_class_f64(NEG_ZERO_F64, CLASS_MASK_ZERO))
        self.assertFalse(ref_cmp_class_f64(POS_ONE_F64, CLASS_MASK_ZERO))

        self.assertTrue(ref_cmp_class_f64(SNAN_F64, CLASS_MASK_NAN))
        self.assertTrue(ref_cmp_class_f64(QNAN_F64, CLASS_MASK_NAN))
        self.assertFalse(ref_cmp_class_f64(POS_INF_F64, CLASS_MASK_NAN))

    def test_input_modifiers_f32(self) -> None:
        # abs(-1.0) -> +1.0 (matches POS_NORMAL)
        self.assertTrue(ref_cmp_class_f32(NEG_ONE_F32, CLASS_POS_NORMAL, abs_val=True))
        self.assertFalse(ref_cmp_class_f32(NEG_ONE_F32, CLASS_NEG_NORMAL, abs_val=True))

        # neg(+1.0) -> -1.0 (matches NEG_NORMAL)
        self.assertTrue(ref_cmp_class_f32(POS_ONE_F32, CLASS_NEG_NORMAL, neg=True))
        self.assertFalse(ref_cmp_class_f32(POS_ONE_F32, CLASS_POS_NORMAL, neg=True))

        # -|-0.0| -> -0.0 (matches NEG_ZERO)
        self.assertTrue(ref_cmp_class_f32(NEG_ZERO_F32, CLASS_NEG_ZERO, neg=True, abs_val=True))
        self.assertFalse(ref_cmp_class_f32(NEG_ZERO_F32, CLASS_POS_ZERO, neg=True, abs_val=True))

    def test_input_modifiers_f64(self) -> None:
        # abs(-INF) -> +INF
        self.assertTrue(ref_cmp_class_f64(NEG_INF_F64, CLASS_POS_INF, abs_val=True))
        self.assertFalse(ref_cmp_class_f64(NEG_INF_F64, CLASS_NEG_INF, abs_val=True))

        # neg(+INF) -> -INF
        self.assertTrue(ref_cmp_class_f64(POS_INF_F64, CLASS_NEG_INF, neg=True))
        self.assertFalse(ref_cmp_class_f64(POS_INF_F64, CLASS_POS_INF, neg=True))


# ---------------------------------------------------------------------------
# Symbolic AST Opcode Definitions and Typed Expressions
# ---------------------------------------------------------------------------

class RegKind(enum.Enum):
    """Register file domain."""
    VGPR = "v"
    SGPR = "s"
    SPECIAL = "special"


@dataclasses.dataclass(frozen=True)
class Register:
    """Symbolic register operand representing single 32-bit dwords, pairs, or special VCC."""
    kind: RegKind
    index: int
    count: int = 1

    def __post_init__(self) -> None:
        if self.kind == RegKind.VGPR:
            if self.index < 0 or self.index >= 256:
                raise ValueError(f"VGPR index out of range [0, 255]: {self.index}")
            if self.count not in (1, 2):
                raise ValueError(f"Unsupported VGPR count (must be 1 or 2): {self.count}")
            if self.count == 2 and self.index + 1 >= 256:
                raise ValueError(f"Register pair exceeds VGPR file boundary: {self.index}")
        elif self.kind == RegKind.SGPR:
            if self.index < 0 or self.index >= 106:
                raise ValueError(f"SGPR index out of range [0, 105]: {self.index}")
            if self.count not in (1, 2):
                raise ValueError(f"Unsupported SGPR count (must be 1 or 2): {self.count}")
            if self.count == 2 and self.index + 1 >= 106:
                raise ValueError(f"Register pair exceeds SGPR file boundary: {self.index}")
        elif self.kind == RegKind.SPECIAL:
            # Special registers like VCC (idx=0)
            if self.count not in (1, 2):
                raise ValueError(f"Unsupported special register count: {self.count}")

    @property
    def is_vgpr(self) -> bool:
        return self.kind == RegKind.VGPR

    @property
    def is_sgpr(self) -> bool:
        return self.kind == RegKind.SGPR

    @property
    def is_vcc(self) -> bool:
        return self.kind == RegKind.SPECIAL and self.index == 0

    def format_asm(self) -> str:
        if self.is_vcc:
            return "vcc"
        if self.count == 2:
            return f"{self.kind.value}[{self.index}:{self.index + 1}]"
        return f"{self.kind.value}{self.index}"


@dataclasses.dataclass(frozen=True)
class Immediate:
    """Symbolic immediate operand (32-bit mask or float pattern)."""
    bits: int

    def __init__(self, val: int | float) -> None:
        if isinstance(val, float):
            b = struct.unpack("<I", struct.pack("<f", val))[0]
        else:
            b = int(val) & MASK64
        object.__setattr__(self, "bits", b)

    def format_asm(self) -> str:
        if self.bits > MASK32:
            return f"{self.bits:#018x}"
        return f"{self.bits:#010x}"


@dataclasses.dataclass(frozen=True)
class OperandModifiers:
    """Input modifiers for VALU instructions (abs and neg)."""
    neg: bool = False
    abs_val: bool = False

    def format_asm(self, base_str: str) -> str:
        res = base_str
        if self.abs_val:
            res = f"|{res}|"
        if self.neg:
            res = f"-{res}"
        return res


class Opcode(enum.Enum):
    """VALU float classification opcodes."""
    V_CMP_CLASS_F32 = "v_cmp_class_f32"
    V_CMP_CLASS_F64 = "v_cmp_class_f64"


class Encoding(enum.Enum):
    """Instruction encoding format."""
    VOPC = "e32"
    VOP3 = "e64"


@dataclasses.dataclass(frozen=True)
class Instruction:
    """Symbolic AST node representing a v_cmp_class instruction."""
    opcode: Opcode
    dst: Register
    src0: Register | Immediate
    src1: Register | Immediate
    src0_mods: OperandModifiers = dataclasses.field(default_factory=OperandModifiers)
    encoding: Encoding = Encoding.VOPC

    def __post_init__(self) -> None:
        # VOPC encoding restrictions
        if self.encoding == Encoding.VOPC:
            if not self.dst.is_vcc:
                raise ValueError(f"VOPC encoding comparison destination must be VCC: {self.dst}")
            if self.src0_mods.neg or self.src0_mods.abs_val:
                raise ValueError("VOPC encoding does not support input modifiers (requires VOP3/e64)")

        # Float type register size validation
        if isinstance(self.src0, Register):
            if self.opcode == Opcode.V_CMP_CLASS_F32 and self.src0.count != 1:
                raise ValueError(f"v_cmp_class_f32 src0 must be 32-bit (count=1): {self.src0}")
            if self.opcode == Opcode.V_CMP_CLASS_F64 and self.src0.count != 2:
                raise ValueError(f"v_cmp_class_f64 src0 must be 64-bit pair (count=2): {self.src0}")

        # Mask operand validation (src1 is always 32-bit mask)
        if isinstance(self.src1, Register) and self.src1.count != 1:
            raise ValueError(f"v_cmp_class src1 mask must be 32-bit (count=1): {self.src1}")

    def to_assembly(self) -> str:
        s0_str = self.src0.format_asm()
        s1_str = self.src1.format_asm()
        s0_fmt = self.src0_mods.format_asm(s0_str)
        dst_str = self.dst.format_asm()
        enc_suffix = f"_{self.encoding.value}" if self.encoding == Encoding.VOP3 else ""
        return f"{self.opcode.value}{enc_suffix} {dst_str}, {s0_fmt}, {s1_str}"


@dataclasses.dataclass
class Program:
    """Symbolic sequence of VALU comparison instructions."""
    instructions: list[Instruction] = dataclasses.field(default_factory=list)

    def append(self, inst: Instruction) -> None:
        self.instructions.append(inst)

    def to_assembly(self) -> str:
        return "\n".join(inst.to_assembly() for inst in self.instructions)


class TestValuCmpClassAST(unittest.TestCase):
    """Unit tests for symbolic AST opcode definitions and typed expressions."""

    def test_register_formatting(self) -> None:
        v0 = Register(RegKind.VGPR, 0, count=1)
        v_pair = Register(RegKind.VGPR, 2, count=2)
        s4 = Register(RegKind.SGPR, 4, count=1)
        s_pair = Register(RegKind.SGPR, 6, count=2)
        vcc = Register(RegKind.SPECIAL, 0, count=2)

        self.assertEqual(v0.format_asm(), "v0")
        self.assertEqual(v_pair.format_asm(), "v[2:3]")
        self.assertEqual(s4.format_asm(), "s4")
        self.assertEqual(s_pair.format_asm(), "s[6:7]")
        self.assertEqual(vcc.format_asm(), "vcc")

    def test_instruction_formatting_vopc(self) -> None:
        inst = Instruction(
            opcode=Opcode.V_CMP_CLASS_F32,
            dst=Register(RegKind.SPECIAL, 0, count=2),
            src0=Register(RegKind.VGPR, 0, count=1),
            src1=Register(RegKind.SGPR, 1, count=1),
            encoding=Encoding.VOPC,
        )
        self.assertEqual(inst.to_assembly(), "v_cmp_class_f32 vcc, v0, s1")

    def test_instruction_formatting_vop3(self) -> None:
        inst = Instruction(
            opcode=Opcode.V_CMP_CLASS_F64,
            dst=Register(RegKind.SGPR, 0, count=2),
            src0=Register(RegKind.VGPR, 2, count=2),
            src1=Immediate(CLASS_MASK_ZERO),
            src0_mods=OperandModifiers(neg=True, abs_val=True),
            encoding=Encoding.VOP3,
        )
        self.assertEqual(inst.to_assembly(), f"v_cmp_class_f64_e64 s[0:1], -|v[2:3]|, {CLASS_MASK_ZERO:#010x}")

    def test_ast_validation_errors(self) -> None:
        # VOPC with non-vcc destination
        with self.assertRaises(ValueError):
            Instruction(
                opcode=Opcode.V_CMP_CLASS_F32,
                dst=Register(RegKind.SGPR, 0, count=1),
                src0=Register(RegKind.VGPR, 0, count=1),
                src1=Register(RegKind.SGPR, 1, count=1),
                encoding=Encoding.VOPC,
            )

        # VOPC with modifiers
        with self.assertRaises(ValueError):
            Instruction(
                opcode=Opcode.V_CMP_CLASS_F32,
                dst=Register(RegKind.SPECIAL, 0, count=2),
                src0=Register(RegKind.VGPR, 0, count=1),
                src1=Register(RegKind.SGPR, 1, count=1),
                src0_mods=OperandModifiers(abs_val=True),
                encoding=Encoding.VOPC,
            )

        # v_cmp_class_f64 with single VGPR
        with self.assertRaises(ValueError):
            Instruction(
                opcode=Opcode.V_CMP_CLASS_F64,
                dst=Register(RegKind.SPECIAL, 0, count=2),
                src0=Register(RegKind.VGPR, 0, count=1),
                src1=Register(RegKind.SGPR, 1, count=1),
            )

    def test_program_assembly(self) -> None:
        prog = Program()
        prog.append(Instruction(
            opcode=Opcode.V_CMP_CLASS_F32,
            dst=Register(RegKind.SPECIAL, 0, count=2),
            src0=Register(RegKind.VGPR, 0, count=1),
            src1=Register(RegKind.SGPR, 2, count=1),
        ))
        prog.append(Instruction(
            opcode=Opcode.V_CMP_CLASS_F64,
            dst=Register(RegKind.SGPR, 4, count=2),
            src0=Register(RegKind.VGPR, 6, count=2),
            src1=Immediate(CLASS_MASK_NAN),
            encoding=Encoding.VOP3,
        ))
        expected = (
            "v_cmp_class_f32 vcc, v0, s2\n"
            f"v_cmp_class_f64_e64 s[4:5], v[6:7], {CLASS_MASK_NAN:#010x}"
        )
        self.assertEqual(prog.to_assembly(), expected)


# ---------------------------------------------------------------------------
# Deterministic Scalar Instruction Interpreter
# ---------------------------------------------------------------------------

class ScalarVALUInterpreter:
    """Deterministic scalar interpreter for GFX10/RDNA2 v_cmp_class instructions."""

    def __init__(self) -> None:
        self.vgprs: list[int] = [0] * 256
        self.sgprs: list[int] = [0] * 106
        self.vcc: int = 0
        self.scc: int = 0
        self.trace: list[str] = []

    @property
    def vccz(self) -> bool:
        """Vector condition code zero flag: True if all bits of VCC are zero."""
        return (self.vcc & MASK64) == 0

    def read_reg_u32(self, kind: RegKind, idx: int) -> int:
        if kind == RegKind.VGPR:
            if idx < 0 or idx >= 256:
                raise IndexError(f"VGPR index out of bounds: {idx}")
            return self.vgprs[idx] & MASK32
        elif kind == RegKind.SGPR:
            if idx < 0 or idx >= 106:
                raise IndexError(f"SGPR index out of bounds: {idx}")
            return self.sgprs[idx] & MASK32
        elif kind == RegKind.SPECIAL and idx == 0:
            return self.vcc & MASK32
        raise IndexError(f"Unsupported register domain: {kind.value}{idx}")

    def write_reg_u32(self, kind: RegKind, idx: int, val: int) -> None:
        val32 = val & MASK32
        if kind == RegKind.VGPR:
            if idx < 0 or idx >= 256:
                raise IndexError(f"VGPR index out of bounds: {idx}")
            self.vgprs[idx] = val32
        elif kind == RegKind.SGPR:
            if idx < 0 or idx >= 106:
                raise IndexError(f"SGPR index out of bounds: {idx}")
            self.sgprs[idx] = val32
        elif kind == RegKind.SPECIAL and idx == 0:
            self.vcc = (self.vcc & ~MASK32) | val32
        else:
            raise IndexError(f"Unsupported register domain: {kind.value}{idx}")

    def read_reg_u64(self, kind: RegKind, idx: int) -> int:
        if kind == RegKind.SPECIAL and idx == 0:
            return self.vcc & MASK64
        lo = self.read_reg_u32(kind, idx)
        hi = self.read_reg_u32(kind, idx + 1)
        return ((hi & MASK32) << 32) | (lo & MASK32)

    def write_reg_u64(self, kind: RegKind, idx: int, val: int) -> None:
        val64 = val & MASK64
        if kind == RegKind.SPECIAL and idx == 0:
            self.vcc = val64
            return
        lo = val64 & MASK32
        hi = (val64 >> 32) & MASK32
        self.write_reg_u32(kind, idx, lo)
        self.write_reg_u32(kind, idx + 1, hi)

    def read_operand_f32(self, op: Register | Immediate, mods: OperandModifiers) -> int:
        if isinstance(op, Immediate):
            raw = op.bits & MASK32
        elif isinstance(op, Register):
            raw = self.read_reg_u32(op.kind, op.index)
        else:
            raise TypeError(f"Unsupported operand type: {type(op)}")
        return apply_f32_modifiers(raw, neg=mods.neg, abs_val=mods.abs_val)

    def read_operand_f64(self, op: Register | Immediate, mods: OperandModifiers) -> int:
        if isinstance(op, Immediate):
            raw = op.bits & MASK64
        elif isinstance(op, Register):
            raw = self.read_reg_u64(op.kind, op.index)
        else:
            raise TypeError(f"Unsupported operand type: {type(op)}")
        return apply_f64_modifiers(raw, neg=mods.neg, abs_val=mods.abs_val)

    def read_operand_u32(self, op: Register | Immediate) -> int:
        if isinstance(op, Immediate):
            return op.bits & MASK32
        elif isinstance(op, Register):
            return self.read_reg_u32(op.kind, op.index)
        raise TypeError(f"Unsupported operand type: {type(op)}")

    def step(self, inst: Instruction) -> bool:
        """Executes a single v_cmp_class instruction deterministically."""
        # Read-before-write semantics
        if inst.opcode == Opcode.V_CMP_CLASS_F32:
            s0_val = self.read_operand_f32(inst.src0, inst.src0_mods)
            s1_val = self.read_operand_u32(inst.src1)
            matched = ref_cmp_class_f32(s0_val, s1_val)
        elif inst.opcode == Opcode.V_CMP_CLASS_F64:
            s0_val = self.read_operand_f64(inst.src0, inst.src0_mods)
            s1_val = self.read_operand_u32(inst.src1)
            matched = ref_cmp_class_f64(s0_val, s1_val)
        else:
            raise NotImplementedError(f"Unsupported opcode: {inst.opcode}")

        # In scalar thread lane 0, boolean output is written to bit 0 of destination mask
        bit_val = 1 if matched else 0
        if inst.dst.is_vcc:
            self.vcc = bit_val
        elif inst.dst.is_sgpr:
            if inst.dst.count == 2:
                self.write_reg_u64(RegKind.SGPR, inst.dst.index, bit_val)
            else:
                self.write_reg_u32(RegKind.SGPR, inst.dst.index, bit_val)
        else:
            raise ValueError(f"Invalid comparison destination: {inst.dst}")

        self.trace.append(f"{inst.to_assembly()} => {matched} (bit={bit_val})")
        return matched

    def run_program(self, prog: Program) -> list[bool]:
        """Executes all instructions in a program sequentially."""
        return [self.step(inst) for inst in prog.instructions]


class TestValuCmpClassScalarInterpreter(unittest.TestCase):
    """Unit tests for the deterministic scalar instruction interpreter."""

    def test_scalar_f32_zero_check(self) -> None:
        interp = ScalarVALUInterpreter()
        interp.write_reg_u32(RegKind.VGPR, 0, POS_ZERO_F32)
        interp.write_reg_u32(RegKind.SGPR, 4, CLASS_MASK_ZERO)

        inst = Instruction(
            opcode=Opcode.V_CMP_CLASS_F32,
            dst=Register(RegKind.SPECIAL, 0, count=2),
            src0=Register(RegKind.VGPR, 0, count=1),
            src1=Register(RegKind.SGPR, 4, count=1),
        )
        res = interp.step(inst)
        self.assertTrue(res)
        self.assertEqual(interp.vcc, 1)

    def test_scalar_f32_normal_against_inf(self) -> None:
        interp = ScalarVALUInterpreter()
        interp.write_reg_u32(RegKind.VGPR, 0, POS_ONE_F32)

        inst = Instruction(
            opcode=Opcode.V_CMP_CLASS_F32,
            dst=Register(RegKind.SPECIAL, 0, count=2),
            src0=Register(RegKind.VGPR, 0, count=1),
            src1=Immediate(CLASS_MASK_INF),
        )
        res = interp.step(inst)
        self.assertFalse(res)
        self.assertEqual(interp.vcc, 0)

    def test_scalar_f64_nan_check(self) -> None:
        interp = ScalarVALUInterpreter()
        interp.write_reg_u64(RegKind.VGPR, 2, QNAN_F64)

        inst = Instruction(
            opcode=Opcode.V_CMP_CLASS_F64,
            dst=Register(RegKind.SGPR, 0, count=2),
            src0=Register(RegKind.VGPR, 2, count=2),
            src1=Immediate(CLASS_MASK_NAN),
            encoding=Encoding.VOP3,
        )
        res = interp.step(inst)
        self.assertTrue(res)
        self.assertEqual(interp.read_reg_u64(RegKind.SGPR, 0), 1)

    def test_scalar_program_chain(self) -> None:
        interp = ScalarVALUInterpreter()
        interp.write_reg_u32(RegKind.VGPR, 0, POS_INF_F32)
        interp.write_reg_u64(RegKind.VGPR, 2, NEG_DENORM_MIN_F64)

        prog = Program()
        prog.append(Instruction(
            opcode=Opcode.V_CMP_CLASS_F32,
            dst=Register(RegKind.SPECIAL, 0, count=2),
            src0=Register(RegKind.VGPR, 0, count=1),
            src1=Immediate(CLASS_POS_INF),
        ))
        prog.append(Instruction(
            opcode=Opcode.V_CMP_CLASS_F64,
            dst=Register(RegKind.SGPR, 4, count=2),
            src0=Register(RegKind.VGPR, 2, count=2),
            src1=Immediate(CLASS_NEG_DENORMAL),
            encoding=Encoding.VOP3,
        ))
        results = interp.run_program(prog)
        self.assertEqual(results, [True, True])
        self.assertEqual(len(interp.trace), 2)


# ---------------------------------------------------------------------------
# SIMD Wavefront Interpreter (Multi-Lane Divergence & 32/64-Bit EXEC Masking)
# ---------------------------------------------------------------------------

class SIMDWavefrontInterpreter:
    """Multi-lane SIMD interpreter modeling Wave32 and Wave64 execution on RDNA2."""

    def __init__(self, wave_size: int = 64) -> None:
        if wave_size not in (32, 64):
            raise ValueError(f"Invalid wave_size: {wave_size} (must be 32 or 64)")
        self.wave_size: int = wave_size
        self.exec_mask: int = (1 << wave_size) - 1
        self.vgprs: list[list[int]] = [[0] * 256 for _ in range(wave_size)]
        self.sgprs: list[int] = [0] * 106
        self.vcc: int = 0
        self.scc: int = 0

    @property
    def vccz(self) -> bool:
        """Vector condition code zero flag: True if all active wave bits of VCC are zero."""
        valid_mask = (1 << self.wave_size) - 1
        return (self.vcc & valid_mask) == 0

    def set_exec_mask(self, mask: int) -> None:
        valid_mask = (1 << self.wave_size) - 1
        self.exec_mask = mask & valid_mask

    def read_lane_reg_u32(self, lane: int, kind: RegKind, idx: int) -> int:
        if kind == RegKind.VGPR:
            if idx < 0 or idx >= 256:
                raise IndexError(f"VGPR index out of bounds: {idx}")
            return self.vgprs[lane][idx] & MASK32
        elif kind == RegKind.SGPR:
            if idx < 0 or idx >= 106:
                raise IndexError(f"SGPR index out of bounds: {idx}")
            return self.sgprs[idx] & MASK32
        elif kind == RegKind.SPECIAL and idx == 0:
            return self.vcc & MASK32
        raise IndexError(f"Unsupported register domain: {kind.value}{idx}")

    def write_lane_reg_u32(self, lane: int, kind: RegKind, idx: int, val: int) -> None:
        val32 = val & MASK32
        if kind == RegKind.VGPR:
            if idx < 0 or idx >= 256:
                raise IndexError(f"VGPR index out of bounds: {idx}")
            self.vgprs[lane][idx] = val32
        elif kind == RegKind.SGPR:
            if idx < 0 or idx >= 106:
                raise IndexError(f"SGPR index out of bounds: {idx}")
            self.sgprs[idx] = val32
        elif kind == RegKind.SPECIAL and idx == 0:
            self.vcc = (self.vcc & ~MASK32) | val32
        else:
            raise IndexError(f"Unsupported register domain: {kind.value}{idx}")

    def read_lane_reg_u64(self, lane: int, kind: RegKind, idx: int) -> int:
        if kind == RegKind.SPECIAL and idx == 0:
            return self.vcc & MASK64
        lo = self.read_lane_reg_u32(lane, kind, idx)
        hi = self.read_lane_reg_u32(lane, kind, idx + 1)
        return ((hi & MASK32) << 32) | (lo & MASK32)

    def write_lane_reg_u64(self, lane: int, kind: RegKind, idx: int, val: int) -> None:
        val64 = val & MASK64
        if kind == RegKind.SPECIAL and idx == 0:
            self.vcc = val64
            return
        lo = val64 & MASK32
        hi = (val64 >> 32) & MASK32
        self.write_lane_reg_u32(lane, kind, idx, lo)
        self.write_lane_reg_u32(lane, kind, idx + 1, hi)

    def read_lane_operand_f32(self, lane: int, op: Register | Immediate, mods: OperandModifiers) -> int:
        if isinstance(op, Immediate):
            raw = op.bits & MASK32
        elif isinstance(op, Register):
            raw = self.read_lane_reg_u32(lane, op.kind, op.index)
        else:
            raise TypeError(f"Unsupported operand type: {type(op)}")
        return apply_f32_modifiers(raw, neg=mods.neg, abs_val=mods.abs_val)

    def read_lane_operand_f64(self, lane: int, op: Register | Immediate, mods: OperandModifiers) -> int:
        if isinstance(op, Immediate):
            raw = op.bits & MASK64
        elif isinstance(op, Register):
            raw = self.read_lane_reg_u64(lane, op.kind, op.index)
        else:
            raise TypeError(f"Unsupported operand type: {type(op)}")
        return apply_f64_modifiers(raw, neg=mods.neg, abs_val=mods.abs_val)

    def read_lane_operand_u32(self, lane: int, op: Register | Immediate) -> int:
        if isinstance(op, Immediate):
            return op.bits & MASK32
        elif isinstance(op, Register):
            return self.read_lane_reg_u32(lane, op.kind, op.index)
        raise TypeError(f"Unsupported operand type: {type(op)}")

    def step(self, inst: Instruction) -> int:
        """Executes a v_cmp_class instruction across all wavefront lanes governed by EXEC mask."""
        # 1. Latch inputs for all lanes (read-before-write semantics)
        lane_inputs: list[tuple[int, int]] = []
        for lane in range(self.wave_size):
            if inst.opcode == Opcode.V_CMP_CLASS_F32:
                s0 = self.read_lane_operand_f32(lane, inst.src0, inst.src0_mods)
            else:
                s0 = self.read_lane_operand_f64(lane, inst.src0, inst.src0_mods)
            s1 = self.read_lane_operand_u32(lane, inst.src1)
            lane_inputs.append((s0, s1))

        # 2. Compute comparison bitmask. Inactive lanes strictly yield 0 in comparison mask.
        res_mask = 0
        for lane in range(self.wave_size):
            is_active = bool((self.exec_mask >> lane) & 1)
            if not is_active:
                continue
            s0, s1 = lane_inputs[lane]
            if inst.opcode == Opcode.V_CMP_CLASS_F32:
                matched = ref_cmp_class_f32(s0, s1)
            else:
                matched = ref_cmp_class_f64(s0, s1)
            if matched:
                res_mask |= (1 << lane)

        # 3. Write mask to destination register
        if inst.dst.is_vcc:
            self.vcc = res_mask
        elif inst.dst.is_sgpr:
            if inst.dst.count == 2:
                self.write_lane_reg_u64(0, RegKind.SGPR, inst.dst.index, res_mask)
            else:
                self.write_lane_reg_u32(0, RegKind.SGPR, inst.dst.index, res_mask & MASK32)
        else:
            raise ValueError(f"Invalid comparison destination: {inst.dst}")

        return res_mask


class TestValuCmpClassSIMD(unittest.TestCase):
    """Unit tests for SIMD multi-lane divergence and 32/64-bit EXEC masking."""

    def test_wave32_uniform_execution(self) -> None:
        simd = SIMDWavefrontInterpreter(wave_size=32)
        for lane in range(32):
            simd.write_lane_reg_u32(lane, RegKind.VGPR, 0, POS_ZERO_F32)

        inst = Instruction(
            opcode=Opcode.V_CMP_CLASS_F32,
            dst=Register(RegKind.SPECIAL, 0, count=2),
            src0=Register(RegKind.VGPR, 0, count=1),
            src1=Immediate(CLASS_MASK_ZERO),
        )
        res_mask = simd.step(inst)
        self.assertEqual(res_mask, 0xFFFFFFFF)
        self.assertEqual(simd.vcc, 0xFFFFFFFF)

    def test_wave64_multi_lane_divergence(self) -> None:
        simd = SIMDWavefrontInterpreter(wave_size=64)
        # Even lanes: +0.0 (matches CLASS_POS_ZERO)
        # Odd lanes: +1.0 (does not match CLASS_POS_ZERO)
        expected_mask = 0
        for lane in range(64):
            if lane % 2 == 0:
                simd.write_lane_reg_u64(lane, RegKind.VGPR, 2, POS_ZERO_F64)
                expected_mask |= (1 << lane)
            else:
                simd.write_lane_reg_u64(lane, RegKind.VGPR, 2, POS_ONE_F64)

        inst = Instruction(
            opcode=Opcode.V_CMP_CLASS_F64,
            dst=Register(RegKind.SPECIAL, 0, count=2),
            src0=Register(RegKind.VGPR, 2, count=2),
            src1=Immediate(CLASS_POS_ZERO),
        )
        res_mask = simd.step(inst)
        self.assertEqual(res_mask, expected_mask)
        self.assertEqual(simd.vcc, expected_mask)

    def test_exec_masking_partial(self) -> None:
        simd = SIMDWavefrontInterpreter(wave_size=32)
        # Only low 16 lanes active
        exec_mask = 0x0000FFFF
        simd.set_exec_mask(exec_mask)

        # All lanes hold NaN
        for lane in range(32):
            simd.write_lane_reg_u32(lane, RegKind.VGPR, 0, QNAN_F32)

        inst = Instruction(
            opcode=Opcode.V_CMP_CLASS_F32,
            dst=Register(RegKind.SPECIAL, 0, count=2),
            src0=Register(RegKind.VGPR, 0, count=1),
            src1=Immediate(CLASS_MASK_NAN),
        )
        res_mask = simd.step(inst)
        # Inactive high 16 lanes must be 0
        self.assertEqual(res_mask, 0x0000FFFF)

    def test_exec_masking_all_inactive(self) -> None:
        simd = SIMDWavefrontInterpreter(wave_size=64)
        simd.set_exec_mask(0)

        for lane in range(64):
            simd.write_lane_reg_u64(lane, RegKind.VGPR, 0, POS_INF_F64)

        inst = Instruction(
            opcode=Opcode.V_CMP_CLASS_F64,
            dst=Register(RegKind.SPECIAL, 0, count=2),
            src0=Register(RegKind.VGPR, 0, count=2),
            src1=Immediate(CLASS_MASK_INF),
        )
        res_mask = simd.step(inst)
        self.assertEqual(res_mask, 0)
        self.assertEqual(simd.vcc, 0)

    def test_scalar_sgpr_mask_broadcast(self) -> None:
        simd = SIMDWavefrontInterpreter(wave_size=32)
        # s4 holds mask for CLASS_POS_NORMAL
        simd.write_lane_reg_u32(0, RegKind.SGPR, 4, CLASS_POS_NORMAL)

        # Lanes 0..9 hold 1.0 (POS_NORMAL), others hold 0.0
        for lane in range(32):
            val = POS_ONE_F32 if lane < 10 else POS_ZERO_F32
            simd.write_lane_reg_u32(lane, RegKind.VGPR, 0, val)

        inst = Instruction(
            opcode=Opcode.V_CMP_CLASS_F32,
            dst=Register(RegKind.SPECIAL, 0, count=2),
            src0=Register(RegKind.VGPR, 0, count=1),
            src1=Register(RegKind.SGPR, 4, count=1),
        )
        res_mask = simd.step(inst)
        self.assertEqual(res_mask, 0x3FF)


class TestValuCmpClassRegisterAliasing(unittest.TestCase):
    """Unit tests for register aliasing and in-place operand preservation."""

    def test_dst_equals_src1_aliasing_scalar(self) -> None:
        # Test dst == src1: s4 is both the mask input and the destination register
        interp = ScalarVALUInterpreter()
        interp.write_reg_u32(RegKind.VGPR, 0, POS_ZERO_F32)
        interp.write_reg_u32(RegKind.SGPR, 4, CLASS_POS_ZERO)  # mask = 0x040

        inst = Instruction(
            opcode=Opcode.V_CMP_CLASS_F32,
            dst=Register(RegKind.SGPR, 4, count=1),
            src0=Register(RegKind.VGPR, 0, count=1),
            src1=Register(RegKind.SGPR, 4, count=1),
            encoding=Encoding.VOP3,
        )
        res = interp.step(inst)
        self.assertTrue(res)
        # Destination s4 now holds the result mask 1 (latching src1 occurred before writeback)
        self.assertEqual(interp.read_reg_u32(RegKind.SGPR, 4), 1)

    def test_dst_pair_overlaps_src1_scalar(self) -> None:
        # Test 64-bit dst s[4:5] where src1 is s5 (high word of dst)
        interp = ScalarVALUInterpreter()
        interp.write_reg_u64(RegKind.VGPR, 2, QNAN_F64)
        interp.write_reg_u32(RegKind.SGPR, 5, CLASS_MASK_NAN)

        inst = Instruction(
            opcode=Opcode.V_CMP_CLASS_F64,
            dst=Register(RegKind.SGPR, 4, count=2),
            src0=Register(RegKind.VGPR, 2, count=2),
            src1=Register(RegKind.SGPR, 5, count=1),
            encoding=Encoding.VOP3,
        )
        res = interp.step(inst)
        self.assertTrue(res)
        # s[4:5] holds 64-bit result mask 1 (low=1, hi=0)
        self.assertEqual(interp.read_reg_u64(RegKind.SGPR, 4), 1)

    def test_src0_vgpr_immutability(self) -> None:
        interp = ScalarVALUInterpreter()
        # Prepopulate VGPR pair with specific double pattern
        canary = 0xDEADBEEFCAFEBAAD
        interp.write_reg_u64(RegKind.VGPR, 10, canary)

        inst = Instruction(
            opcode=Opcode.V_CMP_CLASS_F64,
            dst=Register(RegKind.SPECIAL, 0, count=2),
            src0=Register(RegKind.VGPR, 10, count=2),
            src1=Immediate(CLASS_MASK_ALL),
        )
        interp.step(inst)
        # Source VGPR must never be modified by comparison
        self.assertEqual(interp.read_reg_u64(RegKind.VGPR, 10), canary)

    def test_unrelated_registers_preserved(self) -> None:
        interp = ScalarVALUInterpreter()
        for i in range(20, 30):
            interp.write_reg_u32(RegKind.VGPR, i, 0x12340000 + i)
            interp.write_reg_u32(RegKind.SGPR, i, 0x56780000 + i)

        inst = Instruction(
            opcode=Opcode.V_CMP_CLASS_F32,
            dst=Register(RegKind.SPECIAL, 0, count=2),
            src0=Immediate(0.0),
            src1=Immediate(CLASS_MASK_ZERO),
        )
        interp.step(inst)

        for i in range(20, 30):
            self.assertEqual(interp.read_reg_u32(RegKind.VGPR, i), 0x12340000 + i)
            self.assertEqual(interp.read_reg_u32(RegKind.SGPR, i), 0x56780000 + i)

    def test_simd_aliasing_dst_sgpr_pair_equals_src1(self) -> None:
        simd = SIMDWavefrontInterpreter(wave_size=64)
        # s4 holds mask for CLASS_MASK_INF
        simd.write_lane_reg_u32(0, RegKind.SGPR, 4, CLASS_MASK_INF)

        # All lanes hold +INF
        for lane in range(64):
            simd.write_lane_reg_u64(lane, RegKind.VGPR, 2, POS_INF_F64)

        # dst is s[4:5], src1 is s4
        inst = Instruction(
            opcode=Opcode.V_CMP_CLASS_F64,
            dst=Register(RegKind.SGPR, 4, count=2),
            src0=Register(RegKind.VGPR, 2, count=2),
            src1=Register(RegKind.SGPR, 4, count=1),
            encoding=Encoding.VOP3,
        )
        res_mask = simd.step(inst)
        self.assertEqual(res_mask, MASK64)
        self.assertEqual(simd.read_lane_reg_u64(0, RegKind.SGPR, 4), MASK64)


class TestValuCmpClassConditionCodesAndFlags(unittest.TestCase):
    """Unit tests for condition codes, status flags, and carry/borrow isolation."""

    def test_scc_immutability_scalar(self) -> None:
        interp = ScalarVALUInterpreter()
        interp.scc = 1  # Preset SCC to 1

        inst = Instruction(
            opcode=Opcode.V_CMP_CLASS_F32,
            dst=Register(RegKind.SPECIAL, 0, count=2),
            src0=Immediate(1.0),
            src1=Immediate(CLASS_MASK_NORMAL),
        )
        interp.step(inst)
        # Vector comparison instructions never modify the scalar condition code (SCC)
        self.assertEqual(interp.scc, 1)

    def test_scc_immutability_simd(self) -> None:
        simd = SIMDWavefrontInterpreter(wave_size=64)
        simd.scc = 1

        inst = Instruction(
            opcode=Opcode.V_CMP_CLASS_F64,
            dst=Register(RegKind.SPECIAL, 0, count=2),
            src0=Immediate(0.0),
            src1=Immediate(CLASS_MASK_ZERO),
        )
        simd.step(inst)
        self.assertEqual(simd.scc, 1)

    def test_vccz_flag_evaluation_scalar(self) -> None:
        interp = ScalarVALUInterpreter()
        # Non-matching check: 1.0 against ZERO mask -> match=False, VCC=0, vccz=True
        inst_nomatch = Instruction(
            opcode=Opcode.V_CMP_CLASS_F32,
            dst=Register(RegKind.SPECIAL, 0, count=2),
            src0=Immediate(1.0),
            src1=Immediate(CLASS_MASK_ZERO),
        )
        interp.step(inst_nomatch)
        self.assertEqual(interp.vcc, 0)
        self.assertTrue(interp.vccz)

        # Matching check: 0.0 against ZERO mask -> match=True, VCC=1, vccz=False
        inst_match = Instruction(
            opcode=Opcode.V_CMP_CLASS_F32,
            dst=Register(RegKind.SPECIAL, 0, count=2),
            src0=Immediate(0.0),
            src1=Immediate(CLASS_MASK_ZERO),
        )
        interp.step(inst_match)
        self.assertEqual(interp.vcc, 1)
        self.assertFalse(interp.vccz)

    def test_vccz_flag_evaluation_simd(self) -> None:
        simd = SIMDWavefrontInterpreter(wave_size=32)
        # All lanes hold 1.0; compare against ZERO -> all false -> vcc=0 -> vccz=True
        for lane in range(32):
            simd.write_lane_reg_u32(lane, RegKind.VGPR, 0, POS_ONE_F32)

        inst = Instruction(
            opcode=Opcode.V_CMP_CLASS_F32,
            dst=Register(RegKind.SPECIAL, 0, count=2),
            src0=Register(RegKind.VGPR, 0, count=1),
            src1=Immediate(CLASS_MASK_ZERO),
        )
        simd.step(inst)
        self.assertEqual(simd.vcc, 0)
        self.assertTrue(simd.vccz)

        # One lane holds 0.0 -> vcc != 0 -> vccz=False
        simd.write_lane_reg_u32(5, RegKind.VGPR, 0, POS_ZERO_F32)
        simd.step(inst)
        self.assertEqual(simd.vcc, (1 << 5))
        self.assertFalse(simd.vccz)

    def test_snan_quiet_classification_without_exception(self) -> None:
        # Per IEEE 754-2008 / 2019 clause 5.7.2, classification is a non-arithmetic predicate
        # that inspects bit patterns quietly without signaling Invalid Operation on sNaN.
        self.assertTrue(ref_cmp_class_f32(SNAN_F32, CLASS_SNAN))
        self.assertFalse(ref_cmp_class_f32(SNAN_F32, CLASS_QNAN))
        self.assertTrue(ref_cmp_class_f64(SNAN_F64, CLASS_SNAN))
        self.assertFalse(ref_cmp_class_f64(SNAN_F64, CLASS_QNAN))


class TestValuCmpClassIEEE754Boundaries(unittest.TestCase):
    """Exhaustive unit tests for IEEE 754 boundary extremes, subnormals, and NaN handling."""

    def test_signed_zeros_distinction(self) -> None:
        # +0.0
        self.assertTrue(ref_cmp_class_f32(POS_ZERO_F32, CLASS_POS_ZERO))
        self.assertFalse(ref_cmp_class_f32(POS_ZERO_F32, CLASS_NEG_ZERO))
        self.assertTrue(ref_cmp_class_f64(POS_ZERO_F64, CLASS_POS_ZERO))
        self.assertFalse(ref_cmp_class_f64(POS_ZERO_F64, CLASS_NEG_ZERO))

        # -0.0
        self.assertTrue(ref_cmp_class_f32(NEG_ZERO_F32, CLASS_NEG_ZERO))
        self.assertFalse(ref_cmp_class_f32(NEG_ZERO_F32, CLASS_POS_ZERO))
        self.assertTrue(ref_cmp_class_f64(NEG_ZERO_F64, CLASS_NEG_ZERO))
        self.assertFalse(ref_cmp_class_f64(NEG_ZERO_F64, CLASS_POS_ZERO))

    def test_subnormal_boundaries(self) -> None:
        # F32 subnormals
        self.assertTrue(ref_cmp_class_f32(DENORM_MIN_F32, CLASS_POS_DENORMAL))
        self.assertTrue(ref_cmp_class_f32(DENORM_MAX_F32, CLASS_POS_DENORMAL))
        self.assertTrue(ref_cmp_class_f32(NEG_DENORM_MIN_F32, CLASS_NEG_DENORMAL))
        self.assertTrue(ref_cmp_class_f32(NEG_DENORM_MAX_F32, CLASS_NEG_DENORMAL))
        # Ensure subnormal does not match zero or normal
        self.assertFalse(ref_cmp_class_f32(DENORM_MIN_F32, CLASS_MASK_ZERO))
        self.assertFalse(ref_cmp_class_f32(DENORM_MAX_F32, CLASS_MASK_NORMAL))

        # F64 subnormals
        self.assertTrue(ref_cmp_class_f64(DENORM_MIN_F64, CLASS_POS_DENORMAL))
        self.assertTrue(ref_cmp_class_f64(DENORM_MAX_F64, CLASS_POS_DENORMAL))
        self.assertTrue(ref_cmp_class_f64(NEG_DENORM_MIN_F64, CLASS_NEG_DENORMAL))
        self.assertTrue(ref_cmp_class_f64(NEG_DENORM_MAX_F64, CLASS_NEG_DENORMAL))
        self.assertFalse(ref_cmp_class_f64(DENORM_MIN_F64, CLASS_MASK_ZERO))
        self.assertFalse(ref_cmp_class_f64(DENORM_MAX_F64, CLASS_MASK_NORMAL))

    def test_normal_boundaries(self) -> None:
        # F32 normals
        self.assertTrue(ref_cmp_class_f32(FLT_MIN_F32, CLASS_POS_NORMAL))
        self.assertTrue(ref_cmp_class_f32(FLT_MAX_F32, CLASS_POS_NORMAL))
        self.assertTrue(ref_cmp_class_f32(NEG_FLT_MIN_F32, CLASS_NEG_NORMAL))
        self.assertTrue(ref_cmp_class_f32(NEG_FLT_MAX_F32, CLASS_NEG_NORMAL))

        # F64 normals
        self.assertTrue(ref_cmp_class_f64(DBL_MIN_F64, CLASS_POS_NORMAL))
        self.assertTrue(ref_cmp_class_f64(DBL_MAX_F64, CLASS_POS_NORMAL))
        self.assertTrue(ref_cmp_class_f64(NEG_DBL_MIN_F64, CLASS_NEG_NORMAL))
        self.assertTrue(ref_cmp_class_f64(NEG_DBL_MAX_F64, CLASS_NEG_NORMAL))

    def test_infinities_boundaries(self) -> None:
        # F32 infinities
        self.assertTrue(ref_cmp_class_f32(POS_INF_F32, CLASS_POS_INF))
        self.assertFalse(ref_cmp_class_f32(POS_INF_F32, CLASS_NEG_INF))
        self.assertTrue(ref_cmp_class_f32(NEG_INF_F32, CLASS_NEG_INF))
        self.assertFalse(ref_cmp_class_f32(NEG_INF_F32, CLASS_POS_INF))

        # F64 infinities
        self.assertTrue(ref_cmp_class_f64(POS_INF_F64, CLASS_POS_INF))
        self.assertFalse(ref_cmp_class_f64(POS_INF_F64, CLASS_NEG_INF))
        self.assertTrue(ref_cmp_class_f64(NEG_INF_F64, CLASS_NEG_INF))
        self.assertFalse(ref_cmp_class_f64(NEG_INF_F64, CLASS_POS_INF))

    def test_nan_discrimination_snan_vs_qnan(self) -> None:
        # Various sNaN payloads
        f32_snans = [SNAN_F32, 0x7F800002, 0x7F8000FF, 0x7FBFFFFF, 0xFF800001, 0xFFBFFFFF]
        for snan in f32_snans:
            with self.subTest(snan=f"{snan:#010x}"):
                self.assertTrue(ref_cmp_class_f32(snan, CLASS_SNAN))
                self.assertFalse(ref_cmp_class_f32(snan, CLASS_QNAN))

        # Various qNaN payloads
        f32_qnans = [QNAN_F32, 0x7FC00001, 0x7FFFFFFF, 0xFFC00000, 0xFFFFFFFF]
        for qnan in f32_qnans:
            with self.subTest(qnan=f"{qnan:#010x}"):
                self.assertTrue(ref_cmp_class_f32(qnan, CLASS_QNAN))
                self.assertFalse(ref_cmp_class_f32(qnan, CLASS_SNAN))

        # F64 sNaNs and qNaNs
        f64_snans = [SNAN_F64, 0x7FF0000000000002, 0x7FF7FFFFFFFFFFFF, 0xFFF0000000000001]
        for snan in f64_snans:
            with self.subTest(snan=f"{snan:#018x}"):
                self.assertTrue(ref_cmp_class_f64(snan, CLASS_SNAN))
                self.assertFalse(ref_cmp_class_f64(snan, CLASS_QNAN))

        f64_qnans = [QNAN_F64, 0x7FF8000000000001, 0x7FFFFFFFFFFFFFFF, 0xFFF8000000000000]
        for qnan in f64_qnans:
            with self.subTest(qnan=f"{qnan:#018x}"):
                self.assertTrue(ref_cmp_class_f64(qnan, CLASS_QNAN))
                self.assertFalse(ref_cmp_class_f64(qnan, CLASS_SNAN))

    def test_partition_theorem_exactly_one_class_bit_set(self) -> None:
        # Every floating point pattern must match exactly one class in [0..9]
        samples_f32 = [
            POS_ZERO_F32, NEG_ZERO_F32, POS_ONE_F32, NEG_ONE_F32,
            DENORM_MIN_F32, NEG_DENORM_MIN_F32, FLT_MIN_F32, FLT_MAX_F32,
            POS_INF_F32, NEG_INF_F32, QNAN_F32, SNAN_F32
        ]
        for val in samples_f32:
            cls = classify_f32(val)
            self.assertEqual(bin(cls).count("1"), 1)
            self.assertTrue(0 <= int(math.log2(cls)) <= 9)

        samples_f64 = [
            POS_ZERO_F64, NEG_ZERO_F64, POS_ONE_F64, NEG_ONE_F64,
            DENORM_MIN_F64, NEG_DENORM_MIN_F64, DBL_MIN_F64, DBL_MAX_F64,
            POS_INF_F64, NEG_INF_F64, QNAN_F64, SNAN_F64
        ]
        for val in samples_f64:
            cls = classify_f64(val)
            self.assertEqual(bin(cls).count("1"), 1)
            self.assertTrue(0 <= int(math.log2(cls)) <= 9)

    def test_modifiers_on_boundary_extremes(self) -> None:
        # abs(-0.0) -> +0.0
        self.assertTrue(ref_cmp_class_f32(NEG_ZERO_F32, CLASS_POS_ZERO, abs_val=True))
        # neg(+0.0) -> -0.0
        self.assertTrue(ref_cmp_class_f32(POS_ZERO_F32, CLASS_NEG_ZERO, neg=True))
        # neg(-0.0) -> +0.0
        self.assertTrue(ref_cmp_class_f32(NEG_ZERO_F32, CLASS_POS_ZERO, neg=True))
        # abs(-denorm) -> +denorm
        self.assertTrue(ref_cmp_class_f64(NEG_DENORM_MIN_F64, CLASS_POS_DENORMAL, abs_val=True))
        # neg(+denorm) -> -denorm
        self.assertTrue(ref_cmp_class_f64(DENORM_MIN_F64, CLASS_NEG_DENORMAL, neg=True))
        # abs(sNaN) preserves sNaN
        self.assertTrue(ref_cmp_class_f64(SNAN_F64, CLASS_SNAN, abs_val=True))
        # neg(qNaN) preserves qNaN
        self.assertTrue(ref_cmp_class_f64(QNAN_F64, CLASS_QNAN, neg=True))


class TestValuCmpClassNegativeControls(unittest.TestCase):
    """Unit tests for negative controls, syntax rejection, and invalid AST inputs."""

    def test_invalid_register_indices(self) -> None:
        with self.assertRaises(ValueError):
            Register(RegKind.VGPR, -1, count=1)
        with self.assertRaises(ValueError):
            Register(RegKind.VGPR, 256, count=1)
        with self.assertRaises(ValueError):
            Register(RegKind.VGPR, 255, count=2)  # Pair spills past 255
        with self.assertRaises(ValueError):
            Register(RegKind.SGPR, -1, count=1)
        with self.assertRaises(ValueError):
            Register(RegKind.SGPR, 106, count=1)
        with self.assertRaises(ValueError):
            Register(RegKind.SGPR, 105, count=2)  # Pair spills past 105

    def test_invalid_register_counts(self) -> None:
        with self.assertRaises(ValueError):
            Register(RegKind.VGPR, 0, count=0)
        with self.assertRaises(ValueError):
            Register(RegKind.VGPR, 0, count=3)

    def test_invalid_operand_types_and_sizes(self) -> None:
        # v_cmp_class_f32 with 64-bit pair src0
        with self.assertRaises(ValueError):
            Instruction(
                opcode=Opcode.V_CMP_CLASS_F32,
                dst=Register(RegKind.SPECIAL, 0, count=2),
                src0=Register(RegKind.VGPR, 0, count=2),
                src1=Immediate(CLASS_MASK_ZERO),
            )

        # v_cmp_class_f64 with 32-bit single register src0
        with self.assertRaises(ValueError):
            Instruction(
                opcode=Opcode.V_CMP_CLASS_F64,
                dst=Register(RegKind.SPECIAL, 0, count=2),
                src0=Register(RegKind.VGPR, 0, count=1),
                src1=Immediate(CLASS_MASK_ZERO),
            )

        # src1 mask cannot be 64-bit pair
        with self.assertRaises(ValueError):
            Instruction(
                opcode=Opcode.V_CMP_CLASS_F32,
                dst=Register(RegKind.SPECIAL, 0, count=2),
                src0=Register(RegKind.VGPR, 0, count=1),
                src1=Register(RegKind.SGPR, 2, count=2),
            )

    def test_interpreter_bounds_and_type_rejection(self) -> None:
        interp = ScalarVALUInterpreter()
        with self.assertRaises(IndexError):
            interp.read_reg_u32(RegKind.VGPR, 256)
        with self.assertRaises(IndexError):
            interp.write_reg_u32(RegKind.SGPR, 106, 0)
        with self.assertRaises(TypeError):
            interp.read_operand_f32("not_an_operand", OperandModifiers())  # type: ignore

    def test_simd_wave_size_rejection(self) -> None:
        with self.assertRaises(ValueError):
            SIMDWavefrontInterpreter(wave_size=16)
        with self.assertRaises(ValueError):
            SIMDWavefrontInterpreter(wave_size=128)


class TestValuCmpClassMutationDetection(unittest.TestCase):
    """Mutation tests: verifies that intentional sabotages/mutations of classification are caught."""

    def test_mutation_zero_sign_blindness(self) -> None:
        # Sabotage: treating -0.0 as +0.0
        def mutated_classify_f32(bits: int) -> int:
            u = bits & MASK32
            if (u & ~SIGN_MASK_F32) == 0:
                return CLASS_POS_ZERO  # Always return +0.0
            return classify_f32(bits)

        # Standard check: NEG_ZERO must match CLASS_NEG_ZERO, not CLASS_POS_ZERO
        with self.assertRaises(AssertionError):
            self.assertEqual(mutated_classify_f32(NEG_ZERO_F32), CLASS_NEG_ZERO)

    def test_mutation_snan_qnan_conflation(self) -> None:
        # Sabotage: treating all NaNs as quiet NaNs
        def mutated_classify_f32(bits: int) -> int:
            cls = classify_f32(bits)
            if cls == CLASS_SNAN:
                return CLASS_QNAN
            return cls

        with self.assertRaises(AssertionError):
            self.assertEqual(mutated_classify_f32(SNAN_F32), CLASS_SNAN)

    def test_mutation_subnormal_flush(self) -> None:
        # Sabotage: flushing denormals to zero
        def mutated_classify_f64(bits: int) -> int:
            cls = classify_f64(bits)
            if cls in (CLASS_POS_DENORMAL, CLASS_NEG_DENORMAL):
                return CLASS_POS_ZERO if cls == CLASS_POS_DENORMAL else CLASS_NEG_ZERO
            return cls

        with self.assertRaises(AssertionError):
            self.assertEqual(mutated_classify_f64(DENORM_MIN_F64), CLASS_POS_DENORMAL)

    def test_mutation_infinity_normal_overflow(self) -> None:
        # Sabotage: treating +INF as +NORMAL
        def mutated_classify_f32(bits: int) -> int:
            cls = classify_f32(bits)
            if cls == CLASS_POS_INF:
                return CLASS_POS_NORMAL
            return cls

        with self.assertRaises(AssertionError):
            self.assertEqual(mutated_classify_f32(POS_INF_F32), CLASS_POS_INF)

    def test_mutation_exec_mask_leakage(self) -> None:
        # Sabotage: inactive lanes write 1 to comparison mask
        simd = SIMDWavefrontInterpreter(wave_size=32)
        simd.set_exec_mask(0)  # All lanes inactive
        inst = Instruction(
            opcode=Opcode.V_CMP_CLASS_F32,
            dst=Register(RegKind.SPECIAL, 0, count=2),
            src0=Immediate(0.0),
            src1=Immediate(CLASS_MASK_ZERO),
        )
        proper_mask = simd.step(inst)
        self.assertEqual(proper_mask, 0)

        # Mutated: if inactive lanes leaked, mask would be non-zero
        mutated_mask = proper_mask | 0x00000001
        with self.assertRaises(AssertionError):
            self.assertEqual(mutated_mask, 0)


if __name__ == "__main__":
    unittest.main()
