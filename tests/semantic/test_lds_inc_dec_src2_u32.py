from __future__ import annotations

"""Semantic interpreter tests and reference mathematical oracle for RDNA2 ds_inc_dec_src2_u32.

Opcodes:
  - ds_inc_src2_u32: Atomic wrapping 32-bit increment using LDS secondary operand LDS[B].
      tmp_a = LDS[A]
      tmp_b = LDS[B]
      LDS[A] = 0 if tmp_a >= tmp_b else (tmp_a + 1) & 0xFFFFFFFF
  - ds_dec_src2_u32: Atomic wrapping 32-bit decrement using LDS secondary operand LDS[B].
      tmp_a = LDS[A]
      tmp_b = LDS[B]
      LDS[A] = tmp_b if (tmp_a == 0 or tmp_a > tmp_b) else (tmp_a - 1) & 0xFFFFFFFF
  - ds_add_src2_u32: Atomic 32-bit addition of LDS[B] into LDS[A].
      tmp_a = LDS[A]
      tmp_b = LDS[B]
      LDS[A] = (tmp_a + tmp_b) & 0xFFFFFFFF
  - ds_sub_src2_u32: Atomic 32-bit subtraction of LDS[B] from LDS[A].
      tmp_a = LDS[A]
      tmp_b = LDS[B]
      LDS[A] = (tmp_a - tmp_b) & 0xFFFFFFFF

Architecture & Specification:
  - Target: AMD RDNA2 / GFX10 Local Data Share (LDS).
  - Memory: On-chip software-managed scratchpad shared among threads of a workgroup.
  - Addressing:
      Base address in bytes from VGPR vaddr.
      offset0 (8-bit immediate) and offset1 (8-bit immediate) specify address offsets.
      Address A = (vaddr + offset0 * 4) or byte offset.
      Address B = secondary address in LDS.
      Dword indices are calculated by dropping low 2 bits and wrapping to LDS dwords.
  - Non-returning: Vector general-purpose registers (VGPRs) are not modified.
"""

from dataclasses import dataclass, field
from enum import Enum
import math
import struct
import sys
import unittest

MASK32 = 0xFFFFFFFF
MASK64 = 0xFFFFFFFFFFFFFFFF
DEFAULT_LDS_DWORDS = 16384  # 64 KiB default capacity in dwords (16384 * 4 bytes)
DEFAULT_NUM_LANES = 64     # Wave64 default width
DEFAULT_NUM_VGPRS = 256    # Standard GFX10 vector register file size
DEFAULT_NUM_SGPRS = 106    # Standard GFX10 scalar register file size

# IEEE 754 Single-Precision Boundary Constants
POS_ZERO_F32 = 0x00000000
NEG_ZERO_F32 = 0x80000000
POS_INF_F32 = 0x7F800000
NEG_INF_F32 = 0xFF800000
QNAN_F32 = 0x7FC00000
SNAN_F32 = 0x7F800001
MIN_NORMAL_F32 = 0x00800000
MIN_SUBNORMAL_F32 = 0x00000001
MAX_SUBNORMAL_F32 = 0x007FFFFF
MAX_NORMAL_F32 = 0x7F7FFFFF
ONE_F32 = 0x3F800000


def u32(val: int) -> int:
    """Normalize integer to unsigned 32-bit dword."""
    return val & MASK32


def s32(val: int) -> int:
    """Interpret a 32-bit unsigned dword as a signed two's-complement integer."""
    val &= MASK32
    return val - (1 << 32) if (val & 0x80000000) else val


def bits_to_f32(bits: int) -> float:
    """Interpret 32-bit unsigned integer as IEEE 754 single-precision float."""
    return struct.unpack("<f", struct.pack("<I", bits & MASK32))[0]


def f32_to_bits(val: float) -> int:
    """Pack IEEE 754 single-precision float into 32-bit unsigned integer."""
    return struct.unpack("<I", struct.pack("<f", val))[0]


def is_f32_nan(bits: int) -> bool:
    """Test if 32-bit bit pattern is an IEEE 754 NaN."""
    return (bits & 0x7F800000) == 0x7F800000 and (bits & 0x007FFFFF) != 0


def is_f32_subnormal(bits: int) -> bool:
    """Test if 32-bit bit pattern is an IEEE 754 subnormal float."""
    return (bits & 0x7F800000) == 0 and (bits & 0x007FFFFF) != 0


def ref_ds_inc_src2_u32(val_a: int, val_b: int) -> int:
    """Reference ISA calculation for ds_inc_src2_u32.

    If current memory value at A (val_a) is greater than or equal to wrap
    threshold at B (val_b), reset to 0. Otherwise, increment by 1.
    """
    a = u32(val_a)
    b = u32(val_b)
    if a >= b:
        return 0
    return u32(a + 1)


def ref_ds_dec_src2_u32(val_a: int, val_b: int) -> int:
    """Reference ISA calculation for ds_dec_src2_u32.

    If current memory value at A (val_a) is 0 or strictly exceeds the wrap
    threshold at B (val_b), wrap to val_b. Otherwise, decrement by 1.
    """
    a = u32(val_a)
    b = u32(val_b)
    if a == 0 or a > b:
        return b
    return u32(a - 1)


def ref_ds_add_src2_u32(val_a: int, val_b: int) -> int:
    """Reference ISA calculation for ds_add_src2_u32.

    LDS[A] = (LDS[A] + LDS[B]) mod 2^32.
    """
    return u32(u32(val_a) + u32(val_b))


def ref_ds_sub_src2_u32(val_a: int, val_b: int) -> int:
    """Reference ISA calculation for ds_sub_src2_u32.

    LDS[A] = (LDS[A] - LDS[B]) mod 2^32.
    """
    return u32(u32(val_a) - u32(val_b))


def calc_lds_src2_addresses(
    vaddr: int,
    offset0: int = 0,
    offset1: int = 0,
    lds_dwords: int = DEFAULT_LDS_DWORDS,
) -> tuple[int, int, int, int]:
    """Calculate byte addresses and dword indices for address A and address B.

    Address A = (vaddr + offset0 * 4) & MASK32
    Address B = (vaddr + offset1 * 4) & MASK32
    Dword index = (addr >> 2) & (lds_dwords - 1)

    Returns:
        tuple[int, int, int, int]: (byte_addr_a, byte_addr_b, dword_idx_a, dword_idx_b)
    """
    byte_addr_a = u32(vaddr + (offset0 * 4))
    byte_addr_b = u32(vaddr + (offset1 * 4))
    dword_idx_a = (byte_addr_a >> 2) & (lds_dwords - 1)
    dword_idx_b = (byte_addr_b >> 2) & (lds_dwords - 1)
    return byte_addr_a, byte_addr_b, dword_idx_a, dword_idx_b


# ==============================================================================
# Symbolic AST Definitions and Typed Expressions
# ==============================================================================

class DSOpcode(Enum):
    """Supported DS src2 family opcodes."""
    DS_INC_SRC2_U32 = "ds_inc_src2_u32"
    DS_DEC_SRC2_U32 = "ds_dec_src2_u32"
    DS_ADD_SRC2_U32 = "ds_add_src2_u32"
    DS_SUB_SRC2_U32 = "ds_sub_src2_u32"


class RegType(Enum):
    """Register file class."""
    VGPR = "v"
    SGPR = "s"


@dataclass(frozen=True)
class RegisterOperand:
    """Symbolic register operand representation."""
    index: int
    reg_type: RegType = RegType.VGPR

    def __post_init__(self) -> None:
        if not (0 <= self.index < DEFAULT_NUM_VGPRS):
            raise ValueError(
                f"Register index out of bounds: {self.reg_type.value}{self.index} "
                f"(expected 0 <= index < {DEFAULT_NUM_VGPRS})"
            )

    @property
    def name(self) -> str:
        return f"{self.reg_type.value}{self.index}"


@dataclass(frozen=True)
class LDSAddressExpression:
    """Symbolic representation of LDS address with dual offset descriptors."""
    base_reg: RegisterOperand
    offset0: int = 0
    offset1: int = 0
    gds: bool = False

    def __post_init__(self) -> None:
        if not (0 <= self.offset0 <= 0xFF):
            raise ValueError(f"offset0 out of 8-bit range: {self.offset0}")
        if not (0 <= self.offset1 <= 0xFF):
            raise ValueError(f"offset1 out of 8-bit range: {self.offset1}")
        if self.base_reg.reg_type != RegType.VGPR:
            raise TypeError("LDS base address register must be a VGPR")


@dataclass(frozen=True)
class DSSrc2Instruction:
    """Symbolic AST node for DS src2 atomic instruction."""
    opcode: DSOpcode
    addr: LDSAddressExpression

    def disasm(self) -> str:
        """Format canonical assembly mnemonic and operand string."""
        parts = [self.opcode.value, self.addr.base_reg.name]
        if self.addr.offset0 != 0:
            parts.append(f"offset0:{self.addr.offset0}")
        if self.addr.offset1 != 0:
            parts.append(f"offset1:{self.addr.offset1}")
        if self.addr.gds:
            parts.append("gds")
        return " ".join(parts)


# ==============================================================================
# Deterministic Scalar Interpreter & Machine State
# ==============================================================================

class LDSMemory:
    """Model of Local Data Share memory storage."""

    def __init__(self, size_dwords: int = DEFAULT_LDS_DWORDS) -> None:
        if size_dwords <= 0 or (size_dwords & (size_dwords - 1)) != 0:
            raise ValueError(f"LDS size must be a positive power of two: {size_dwords}")
        self.size_dwords = size_dwords
        self.data = [0] * size_dwords

    def read_dword(self, index: int) -> int:
        return self.data[index & (self.size_dwords - 1)]

    def write_dword(self, index: int, val: int) -> None:
        self.data[index & (self.size_dwords - 1)] = u32(val)


class ScalarMachineState:
    """Scalar architectural machine state for single-lane emulation."""

    def __init__(self, lds_dwords: int = DEFAULT_LDS_DWORDS) -> None:
        self.vgprs = [0] * DEFAULT_NUM_VGPRS
        self.sgprs = [0] * DEFAULT_NUM_SGPRS
        self.lds = LDSMemory(lds_dwords)

    def get_vgpr(self, index: int) -> int:
        return self.vgprs[index]

    def set_vgpr(self, index: int, val: int) -> None:
        self.vgprs[index] = u32(val)

    def get_lds(self, dword_idx: int) -> int:
        return self.lds.read_dword(dword_idx)

    def set_lds(self, dword_idx: int, val: int) -> None:
        self.lds.write_dword(dword_idx, val)


class ScalarInterpreter:
    """Deterministic scalar instruction interpreter for DS src2 opcodes."""

    def execute(self, instr: DSSrc2Instruction, state: ScalarMachineState) -> None:
        base_addr = state.get_vgpr(instr.addr.base_reg.index)
        _, _, idx_a, idx_b = calc_lds_src2_addresses(
            vaddr=base_addr,
            offset0=instr.addr.offset0,
            offset1=instr.addr.offset1,
            lds_dwords=state.lds.size_dwords,
        )

        val_a = state.lds.read_dword(idx_a)
        val_b = state.lds.read_dword(idx_b)

        if instr.opcode == DSOpcode.DS_INC_SRC2_U32:
            new_a = ref_ds_inc_src2_u32(val_a, val_b)
        elif instr.opcode == DSOpcode.DS_DEC_SRC2_U32:
            new_a = ref_ds_dec_src2_u32(val_a, val_b)
        elif instr.opcode == DSOpcode.DS_ADD_SRC2_U32:
            new_a = ref_ds_add_src2_u32(val_a, val_b)
        elif instr.opcode == DSOpcode.DS_SUB_SRC2_U32:
            new_a = ref_ds_sub_src2_u32(val_a, val_b)
        else:
            raise NotImplementedError(f"Unsupported opcode: {instr.opcode}")

        # In-place atomic update to LDS location A
        state.lds.write_dword(idx_a, new_a)


# ==============================================================================
# SIMD Multi-Lane State and Divergence Interpreter
# ==============================================================================

class SIMDState:
    """SIMD vector machine state with multi-lane VGPR files and shared LDS."""

    def __init__(
        self,
        num_lanes: int = DEFAULT_NUM_LANES,
        exec_mask: int = MASK64,
        lds_dwords: int = DEFAULT_LDS_DWORDS,
    ) -> None:
        if num_lanes not in (32, 64):
            raise ValueError(f"num_lanes must be 32 or 64, got {num_lanes}")
        self.num_lanes = num_lanes
        self.exec_mask = (exec_mask & MASK32) if num_lanes == 32 else (exec_mask & MASK64)
        self.vgprs = [[0] * DEFAULT_NUM_VGPRS for _ in range(num_lanes)]
        self.lds = LDSMemory(lds_dwords)

    def is_lane_active(self, lane: int) -> bool:
        if 0 <= lane < self.num_lanes:
            return bool((self.exec_mask >> lane) & 1)
        return False

    def get_lane_vgpr(self, lane: int, reg: int) -> int:
        return self.vgprs[lane][reg]

    def set_lane_vgpr(self, lane: int, reg: int, val: int) -> None:
        self.vgprs[lane][reg] = u32(val)


class SIMDInterpreter:
    """SIMD multi-lane execution interpreter handling divergence and EXEC masking."""

    def execute(self, instr: DSSrc2Instruction, state: SIMDState) -> None:
        for lane in range(state.num_lanes):
            if not state.is_lane_active(lane):
                continue

            base_addr = state.get_lane_vgpr(lane, instr.addr.base_reg.index)
            _, _, idx_a, idx_b = calc_lds_src2_addresses(
                vaddr=base_addr,
                offset0=instr.addr.offset0,
                offset1=instr.addr.offset1,
                lds_dwords=state.lds.size_dwords,
            )

            val_a = state.lds.read_dword(idx_a)
            val_b = state.lds.read_dword(idx_b)

            if instr.opcode == DSOpcode.DS_INC_SRC2_U32:
                new_a = ref_ds_inc_src2_u32(val_a, val_b)
            elif instr.opcode == DSOpcode.DS_DEC_SRC2_U32:
                new_a = ref_ds_dec_src2_u32(val_a, val_b)
            elif instr.opcode == DSOpcode.DS_ADD_SRC2_U32:
                new_a = ref_ds_add_src2_u32(val_a, val_b)
            elif instr.opcode == DSOpcode.DS_SUB_SRC2_U32:
                new_a = ref_ds_sub_src2_u32(val_a, val_b)
            else:
                raise NotImplementedError(f"Unsupported opcode: {instr.opcode}")

            state.lds.write_dword(idx_a, new_a)


# ==============================================================================
# Condition Codes, Status Flags, and Carry/Borrow Evaluation
# ==============================================================================

@dataclass(frozen=True)
class ArithmeticStatusFlags:
    """Condition codes and arithmetic status flags for 32-bit operations."""
    zero: bool
    sign: bool
    carry: bool
    borrow: bool
    overflow: bool
    wrapped: bool


def evaluate_src2_status_flags(
    opcode: DSOpcode,
    val_a: int,
    val_b: int,
    result: int,
) -> ArithmeticStatusFlags:
    """Evaluate condition codes, arithmetic flags, and carry/borrow generation."""
    a = u32(val_a)
    b = u32(val_b)
    res = u32(result)

    zero = (res == 0)
    sign = bool(res & 0x80000000)
    carry = False
    borrow = False
    overflow = False
    wrapped = False

    if opcode == DSOpcode.DS_ADD_SRC2_U32:
        carry = (a + b) > MASK32
        sa, sb, sres = s32(a), s32(b), s32(res)
        overflow = ((sa > 0 and sb > 0 and sres < 0) or (sa < 0 and sb < 0 and sres > 0))
    elif opcode == DSOpcode.DS_SUB_SRC2_U32:
        borrow = (a < b)
        sa, sb, sres = s32(a), s32(b), s32(res)
        overflow = ((sa > 0 and sb < 0 and sres < 0) or (sa < 0 and sb > 0 and sres > 0))
    elif opcode == DSOpcode.DS_INC_SRC2_U32:
        wrapped = (a >= b)
        carry = wrapped
    elif opcode == DSOpcode.DS_DEC_SRC2_U32:
        wrapped = (a == 0 or a > b)
        borrow = wrapped

    return ArithmeticStatusFlags(
        zero=zero,
        sign=sign,
        carry=carry,
        borrow=borrow,
        overflow=overflow,
        wrapped=wrapped,
    )


class TestReferenceModelAndOracle(unittest.TestCase):
    """Unit tests for reference mathematical oracle and architectural model."""

    def test_inc_src2_normal_increment(self) -> None:
        self.assertEqual(ref_ds_inc_src2_u32(0, 5), 1)
        self.assertEqual(ref_ds_inc_src2_u32(1, 5), 2)
        self.assertEqual(ref_ds_inc_src2_u32(3, 5), 4)
        self.assertEqual(ref_ds_inc_src2_u32(4, 5), 5)

    def test_inc_src2_wrap_equal_and_greater(self) -> None:
        self.assertEqual(ref_ds_inc_src2_u32(5, 5), 0)
        self.assertEqual(ref_ds_inc_src2_u32(6, 5), 0)
        self.assertEqual(ref_ds_inc_src2_u32(100, 5), 0)
        self.assertEqual(ref_ds_inc_src2_u32(MASK32, 5), 0)

    def test_inc_src2_boundary_zero_and_max(self) -> None:
        self.assertEqual(ref_ds_inc_src2_u32(0, 0), 0)
        self.assertEqual(ref_ds_inc_src2_u32(1, 0), 0)
        self.assertEqual(ref_ds_inc_src2_u32(0, MASK32), 1)
        self.assertEqual(ref_ds_inc_src2_u32(MASK32 - 1, MASK32), MASK32)
        self.assertEqual(ref_ds_inc_src2_u32(MASK32, MASK32), 0)

    def test_dec_src2_normal_decrement(self) -> None:
        self.assertEqual(ref_ds_dec_src2_u32(5, 5), 4)
        self.assertEqual(ref_ds_dec_src2_u32(4, 5), 3)
        self.assertEqual(ref_ds_dec_src2_u32(2, 5), 1)
        self.assertEqual(ref_ds_dec_src2_u32(1, 5), 0)

    def test_dec_src2_wrap_zero_and_greater(self) -> None:
        self.assertEqual(ref_ds_dec_src2_u32(0, 5), 5)
        self.assertEqual(ref_ds_dec_src2_u32(6, 5), 5)
        self.assertEqual(ref_ds_dec_src2_u32(100, 5), 5)
        self.assertEqual(ref_ds_dec_src2_u32(MASK32, 5), 5)

    def test_dec_src2_boundary_zero_and_max(self) -> None:
        self.assertEqual(ref_ds_dec_src2_u32(0, 0), 0)
        self.assertEqual(ref_ds_dec_src2_u32(1, 0), 0)
        self.assertEqual(ref_ds_dec_src2_u32(0, MASK32), MASK32)
        self.assertEqual(ref_ds_dec_src2_u32(MASK32, MASK32), MASK32 - 1)

    def test_add_src2_u32_arithmetic(self) -> None:
        self.assertEqual(ref_ds_add_src2_u32(10, 20), 30)
        self.assertEqual(ref_ds_add_src2_u32(0, 0), 0)
        self.assertEqual(ref_ds_add_src2_u32(MASK32, 1), 0)
        self.assertEqual(ref_ds_add_src2_u32(MASK32, 5), 4)
        self.assertEqual(ref_ds_add_src2_u32(0x80000000, 0x80000000), 0)

    def test_sub_src2_u32_arithmetic(self) -> None:
        self.assertEqual(ref_ds_sub_src2_u32(30, 10), 20)
        self.assertEqual(ref_ds_sub_src2_u32(10, 10), 0)
        self.assertEqual(ref_ds_sub_src2_u32(0, 1), MASK32)
        self.assertEqual(ref_ds_sub_src2_u32(0, 5), MASK32 - 4)
        self.assertEqual(ref_ds_sub_src2_u32(0x80000000, 1), 0x7FFFFFFF)

    def test_address_calculation_and_alignment(self) -> None:
        byte_a, byte_b, idx_a, idx_b = calc_lds_src2_addresses(
            vaddr=0x1000, offset0=2, offset1=4, lds_dwords=1024
        )
        self.assertEqual(byte_a, 0x1008)
        self.assertEqual(byte_b, 0x1010)
        self.assertEqual(idx_a, (0x1008 >> 2) & 1023)
        self.assertEqual(idx_b, (0x1010 >> 2) & 1023)

    def test_self_referencing_aliasing_oracle(self) -> None:
        self.assertEqual(ref_ds_inc_src2_u32(4, 4), 0)
        self.assertEqual(ref_ds_dec_src2_u32(4, 4), 3)
        self.assertEqual(ref_ds_dec_src2_u32(0, 0), 0)
        self.assertEqual(ref_ds_add_src2_u32(4, 4), 8)
        self.assertEqual(ref_ds_sub_src2_u32(4, 4), 0)


class TestSymbolicAST(unittest.TestCase):
    """Unit tests for symbolic AST opcode definitions and typed expressions."""

    def test_opcode_enumeration(self) -> None:
        self.assertEqual(DSOpcode.DS_INC_SRC2_U32.value, "ds_inc_src2_u32")
        self.assertEqual(DSOpcode.DS_DEC_SRC2_U32.value, "ds_dec_src2_u32")
        self.assertEqual(DSOpcode.DS_ADD_SRC2_U32.value, "ds_add_src2_u32")
        self.assertEqual(DSOpcode.DS_SUB_SRC2_U32.value, "ds_sub_src2_u32")

    def test_register_operand_validation_and_naming(self) -> None:
        v0 = RegisterOperand(0, RegType.VGPR)
        self.assertEqual(v0.name, "v0")
        v255 = RegisterOperand(255, RegType.VGPR)
        self.assertEqual(v255.name, "v255")

        with self.assertRaises(ValueError):
            RegisterOperand(-1, RegType.VGPR)
        with self.assertRaises(ValueError):
            RegisterOperand(256, RegType.VGPR)

    def test_address_expression_validation(self) -> None:
        v5 = RegisterOperand(5, RegType.VGPR)
        addr = LDSAddressExpression(base_reg=v5, offset0=10, offset1=20)
        self.assertEqual(addr.base_reg.name, "v5")
        self.assertEqual(addr.offset0, 10)
        self.assertEqual(addr.offset1, 20)

        with self.assertRaises(ValueError):
            LDSAddressExpression(base_reg=v5, offset0=-1)
        with self.assertRaises(ValueError):
            LDSAddressExpression(base_reg=v5, offset0=256)
        with self.assertRaises(ValueError):
            LDSAddressExpression(base_reg=v5, offset1=300)

        s0 = RegisterOperand(0, RegType.SGPR)
        with self.assertRaises(TypeError):
            LDSAddressExpression(base_reg=s0)

    def test_disasm_formatting(self) -> None:
        v2 = RegisterOperand(2, RegType.VGPR)
        addr = LDSAddressExpression(base_reg=v2, offset0=4, offset1=8)
        instr_inc = DSSrc2Instruction(DSOpcode.DS_INC_SRC2_U32, addr)
        self.assertEqual(instr_inc.disasm(), "ds_inc_src2_u32 v2 offset0:4 offset1:8")

        addr_no_offset = LDSAddressExpression(base_reg=v2, offset0=0, offset1=0)
        instr_dec = DSSrc2Instruction(DSOpcode.DS_DEC_SRC2_U32, addr_no_offset)
        self.assertEqual(instr_dec.disasm(), "ds_dec_src2_u32 v2")

        addr_gds = LDSAddressExpression(base_reg=v2, offset0=1, offset1=2, gds=True)
        instr_add = DSSrc2Instruction(DSOpcode.DS_ADD_SRC2_U32, addr_gds)
        self.assertEqual(instr_add.disasm(), "ds_add_src2_u32 v2 offset0:1 offset1:2 gds")

    def test_ast_immutability_and_equality(self) -> None:
        v1 = RegisterOperand(1, RegType.VGPR)
        addr1 = LDSAddressExpression(base_reg=v1, offset0=2, offset1=3)
        addr2 = LDSAddressExpression(base_reg=v1, offset0=2, offset1=3)
        instr1 = DSSrc2Instruction(DSOpcode.DS_SUB_SRC2_U32, addr1)
        instr2 = DSSrc2Instruction(DSOpcode.DS_SUB_SRC2_U32, addr2)
        self.assertEqual(instr1, instr2)


class TestScalarInterpreter(unittest.TestCase):
    """Unit tests for deterministic scalar instruction interpreter."""

    def setUp(self) -> None:
        self.interp = ScalarInterpreter()
        self.state = ScalarMachineState(lds_dwords=1024)

    def test_scalar_ds_inc_src2_u32_execution(self) -> None:
        self.state.set_vgpr(0, 0x40)
        self.state.set_lds(16, 2)
        self.state.set_lds(17, 3)

        v0 = RegisterOperand(0, RegType.VGPR)
        instr = DSSrc2Instruction(
            DSOpcode.DS_INC_SRC2_U32,
            LDSAddressExpression(v0, offset0=0, offset1=1),
        )
        self.interp.execute(instr, self.state)
        self.assertEqual(self.state.get_lds(16), 3)

        self.interp.execute(instr, self.state)
        self.assertEqual(self.state.get_lds(16), 0)
        self.assertEqual(self.state.get_vgpr(0), 0x40)

    def test_scalar_ds_dec_src2_u32_execution(self) -> None:
        self.state.set_vgpr(1, 0x80)
        self.state.set_lds(32, 2)
        self.state.set_lds(33, 5)

        v1 = RegisterOperand(1, RegType.VGPR)
        instr = DSSrc2Instruction(
            DSOpcode.DS_DEC_SRC2_U32,
            LDSAddressExpression(v1, offset0=0, offset1=1),
        )
        self.interp.execute(instr, self.state)
        self.assertEqual(self.state.get_lds(32), 1)

        self.interp.execute(instr, self.state)
        self.assertEqual(self.state.get_lds(32), 0)

        self.interp.execute(instr, self.state)
        self.assertEqual(self.state.get_lds(32), 5)

    def test_scalar_ds_add_src2_u32_execution(self) -> None:
        self.state.set_vgpr(2, 0)
        self.state.set_lds(0, 100)
        self.state.set_lds(1, 50)

        v2 = RegisterOperand(2, RegType.VGPR)
        instr = DSSrc2Instruction(
            DSOpcode.DS_ADD_SRC2_U32,
            LDSAddressExpression(v2, offset0=0, offset1=1),
        )
        self.interp.execute(instr, self.state)
        self.assertEqual(self.state.get_lds(0), 150)
        self.assertEqual(self.state.get_lds(1), 50)

    def test_scalar_ds_sub_src2_u32_execution(self) -> None:
        self.state.set_vgpr(3, 0)
        self.state.set_lds(0, 40)
        self.state.set_lds(1, 50)

        v3 = RegisterOperand(3, RegType.VGPR)
        instr = DSSrc2Instruction(
            DSOpcode.DS_SUB_SRC2_U32,
            LDSAddressExpression(v3, offset0=0, offset1=1),
        )
        self.interp.execute(instr, self.state)
        self.assertEqual(self.state.get_lds(0), u32(-10))
        self.assertEqual(self.state.get_lds(1), 50)


class TestSIMDMultiLaneDivergence(unittest.TestCase):
    """Unit tests for SIMD multi-lane divergence and 32/64-bit EXEC masking."""

    def setUp(self) -> None:
        self.interp = SIMDInterpreter()

    def test_wave32_full_exec_independent_lanes(self) -> None:
        state = SIMDState(num_lanes=32, exec_mask=0xFFFFFFFF, lds_dwords=1024)
        v0 = RegisterOperand(0, RegType.VGPR)
        instr = DSSrc2Instruction(
            DSOpcode.DS_ADD_SRC2_U32,
            LDSAddressExpression(v0, offset0=0, offset1=1),
        )
        for lane in range(32):
            state.set_lane_vgpr(lane, 0, (lane * 2) * 4)
            state.lds.write_dword(lane * 2, 10)
            state.lds.write_dword(lane * 2 + 1, lane)

        self.interp.execute(instr, state)

        for lane in range(32):
            self.assertEqual(state.lds.read_dword(lane * 2), 10 + lane)
            self.assertEqual(state.lds.read_dword(lane * 2 + 1), lane)

    def test_wave64_full_exec_independent_lanes(self) -> None:
        state = SIMDState(num_lanes=64, exec_mask=MASK64, lds_dwords=1024)
        v0 = RegisterOperand(0, RegType.VGPR)
        instr = DSSrc2Instruction(
            DSOpcode.DS_INC_SRC2_U32,
            LDSAddressExpression(v0, offset0=0, offset1=1),
        )
        for lane in range(64):
            state.set_lane_vgpr(lane, 0, (lane * 2) * 4)
            state.lds.write_dword(lane * 2, 0)
            state.lds.write_dword(lane * 2 + 1, 5)

        self.interp.execute(instr, state)

        for lane in range(64):
            self.assertEqual(state.lds.read_dword(lane * 2), 1)

    def test_sparse_exec_mask_even_lanes(self) -> None:
        exec_mask = 0x5555555555555555
        state = SIMDState(num_lanes=64, exec_mask=exec_mask, lds_dwords=1024)
        v0 = RegisterOperand(0, RegType.VGPR)
        instr = DSSrc2Instruction(
            DSOpcode.DS_ADD_SRC2_U32,
            LDSAddressExpression(v0, offset0=0, offset1=1),
        )
        for lane in range(64):
            state.set_lane_vgpr(lane, 0, (lane * 2) * 4)
            state.lds.write_dword(lane * 2, 100)
            state.lds.write_dword(lane * 2 + 1, 5)

        self.interp.execute(instr, state)

        for lane in range(64):
            if lane % 2 == 0:
                self.assertEqual(state.lds.read_dword(lane * 2), 105)
            else:
                self.assertEqual(state.lds.read_dword(lane * 2), 100)

    def test_zero_exec_mask_no_op(self) -> None:
        state = SIMDState(num_lanes=32, exec_mask=0, lds_dwords=1024)
        v0 = RegisterOperand(0, RegType.VGPR)
        instr = DSSrc2Instruction(
            DSOpcode.DS_ADD_SRC2_U32,
            LDSAddressExpression(v0, offset0=0, offset1=1),
        )
        for lane in range(32):
            state.set_lane_vgpr(lane, 0, (lane * 2) * 4)
            state.lds.write_dword(lane * 2, 50)
            state.lds.write_dword(lane * 2 + 1, 5)

        self.interp.execute(instr, state)

        for lane in range(32):
            self.assertEqual(state.lds.read_dword(lane * 2), 50)

    def test_lane_divergence_same_address_atomic_accumulate(self) -> None:
        exec_mask = 0xFF
        state = SIMDState(num_lanes=32, exec_mask=exec_mask, lds_dwords=1024)
        v0 = RegisterOperand(0, RegType.VGPR)
        instr = DSSrc2Instruction(
            DSOpcode.DS_ADD_SRC2_U32,
            LDSAddressExpression(v0, offset0=0, offset1=1),
        )
        for lane in range(32):
            state.set_lane_vgpr(lane, 0, 0)
        state.lds.write_dword(0, 10)
        state.lds.write_dword(1, 3)

        self.interp.execute(instr, state)
        self.assertEqual(state.lds.read_dword(0), 34)

    def test_lane_divergence_inc_wrap_cyclic(self) -> None:
        exec_mask = 0x3F
        state = SIMDState(num_lanes=32, exec_mask=exec_mask, lds_dwords=1024)
        v0 = RegisterOperand(0, RegType.VGPR)
        instr = DSSrc2Instruction(
            DSOpcode.DS_INC_SRC2_U32,
            LDSAddressExpression(v0, offset0=0, offset1=1),
        )
        for lane in range(32):
            state.set_lane_vgpr(lane, 0, 0)
        state.lds.write_dword(0, 0)
        state.lds.write_dword(1, 2)

        self.interp.execute(instr, state)
        self.assertEqual(state.lds.read_dword(0), 0)


class TestRegisterAliasing(unittest.TestCase):
    """Unit tests for register aliasing and in-place operand preservation."""

    def setUp(self) -> None:
        self.scalar_interp = ScalarInterpreter()
        self.simd_interp = SIMDInterpreter()

    def test_base_address_vgpr_preservation_scalar(self) -> None:
        state = ScalarMachineState(lds_dwords=1024)
        for reg in range(DEFAULT_NUM_VGPRS):
            state.set_vgpr(reg, 0x12340000 | reg)

        v7 = RegisterOperand(7, RegType.VGPR)
        state.set_vgpr(7, 0x100)
        instr = DSSrc2Instruction(
            DSOpcode.DS_ADD_SRC2_U32,
            LDSAddressExpression(v7, offset0=0, offset1=1),
        )
        self.scalar_interp.execute(instr, state)

        self.assertEqual(state.get_vgpr(7), 0x100)
        for reg in range(DEFAULT_NUM_VGPRS):
            if reg != 7:
                self.assertEqual(state.get_vgpr(reg), 0x12340000 | reg)

    def test_base_address_vgpr_preservation_simd(self) -> None:
        state = SIMDState(num_lanes=64, exec_mask=MASK64, lds_dwords=1024)
        for lane in range(64):
            for reg in range(16):
                state.set_lane_vgpr(lane, reg, (lane << 16) | reg)

        v3 = RegisterOperand(3, RegType.VGPR)
        instr = DSSrc2Instruction(
            DSOpcode.DS_INC_SRC2_U32,
            LDSAddressExpression(v3, offset0=0, offset1=1),
        )
        self.simd_interp.execute(instr, state)

        for lane in range(64):
            for reg in range(16):
                self.assertEqual(state.get_lane_vgpr(lane, reg), (lane << 16) | reg)

    def test_secondary_memory_operand_preservation(self) -> None:
        state = ScalarMachineState(lds_dwords=1024)
        state.set_vgpr(0, 0x20)
        state.set_lds(8, 100)
        state.set_lds(9, 25)

        v0 = RegisterOperand(0, RegType.VGPR)
        for opcode in (
            DSOpcode.DS_ADD_SRC2_U32,
            DSOpcode.DS_SUB_SRC2_U32,
            DSOpcode.DS_INC_SRC2_U32,
            DSOpcode.DS_DEC_SRC2_U32,
        ):
            instr = DSSrc2Instruction(opcode, LDSAddressExpression(v0, offset0=0, offset1=1))
            self.scalar_interp.execute(instr, state)
            self.assertEqual(state.get_lds(9), 25)

    def test_address_aliasing_same_cell_inc(self) -> None:
        state = ScalarMachineState(lds_dwords=1024)
        state.set_vgpr(0, 0)
        v0 = RegisterOperand(0, RegType.VGPR)
        instr = DSSrc2Instruction(
            DSOpcode.DS_INC_SRC2_U32,
            LDSAddressExpression(v0, offset0=2, offset1=2),
        )

        for val in (0, 1, 42, 0x7FFFFFFF, MASK32):
            state.set_lds(2, val)
            self.scalar_interp.execute(instr, state)
            self.assertEqual(state.get_lds(2), 0)

    def test_address_aliasing_same_cell_dec(self) -> None:
        state = ScalarMachineState(lds_dwords=1024)
        state.set_vgpr(0, 0)
        v0 = RegisterOperand(0, RegType.VGPR)
        instr = DSSrc2Instruction(
            DSOpcode.DS_DEC_SRC2_U32,
            LDSAddressExpression(v0, offset0=5, offset1=5),
        )

        state.set_lds(5, 0)
        self.scalar_interp.execute(instr, state)
        self.assertEqual(state.get_lds(5), 0)

        state.set_lds(5, 10)
        self.scalar_interp.execute(instr, state)
        self.assertEqual(state.get_lds(5), 9)

    def test_address_aliasing_same_cell_add(self) -> None:
        state = ScalarMachineState(lds_dwords=1024)
        state.set_vgpr(0, 0)
        v0 = RegisterOperand(0, RegType.VGPR)
        instr = DSSrc2Instruction(
            DSOpcode.DS_ADD_SRC2_U32,
            LDSAddressExpression(v0, offset0=1, offset1=1),
        )

        state.set_lds(1, 21)
        self.scalar_interp.execute(instr, state)
        self.assertEqual(state.get_lds(1), 42)

        state.set_lds(1, 0x80000000)
        self.scalar_interp.execute(instr, state)
        self.assertEqual(state.get_lds(1), 0)

    def test_address_aliasing_same_cell_sub(self) -> None:
        state = ScalarMachineState(lds_dwords=1024)
        state.set_vgpr(0, 0)
        v0 = RegisterOperand(0, RegType.VGPR)
        instr = DSSrc2Instruction(
            DSOpcode.DS_SUB_SRC2_U32,
            LDSAddressExpression(v0, offset0=3, offset1=3),
        )

        for val in (0, 1, 999, 0x80000000, MASK32):
            state.set_lds(3, val)
            self.scalar_interp.execute(instr, state)
            self.assertEqual(state.get_lds(3), 0)


class TestConditionCodesAndStatusFlags(unittest.TestCase):
    """Unit tests for condition codes, status flags, and carry/borrow generation."""

    def test_add_carry_generation(self) -> None:
        flags = evaluate_src2_status_flags(
            DSOpcode.DS_ADD_SRC2_U32, MASK32, 1, 0
        )
        self.assertTrue(flags.carry)
        self.assertTrue(flags.zero)
        self.assertFalse(flags.sign)
        self.assertFalse(flags.borrow)

        flags_norm = evaluate_src2_status_flags(
            DSOpcode.DS_ADD_SRC2_U32, 10, 20, 30
        )
        self.assertFalse(flags_norm.carry)
        self.assertFalse(flags_norm.zero)
        self.assertFalse(flags_norm.sign)

    def test_add_signed_overflow(self) -> None:
        flags = evaluate_src2_status_flags(
            DSOpcode.DS_ADD_SRC2_U32, 0x7FFFFFFF, 1, 0x80000000
        )
        self.assertTrue(flags.overflow)
        self.assertTrue(flags.sign)
        self.assertFalse(flags.carry)

    def test_sub_borrow_generation(self) -> None:
        flags = evaluate_src2_status_flags(
            DSOpcode.DS_SUB_SRC2_U32, 0, 1, MASK32
        )
        self.assertTrue(flags.borrow)
        self.assertFalse(flags.zero)
        self.assertTrue(flags.sign)
        self.assertFalse(flags.carry)

        flags_zero = evaluate_src2_status_flags(
            DSOpcode.DS_SUB_SRC2_U32, 10, 10, 0
        )
        self.assertFalse(flags_zero.borrow)
        self.assertTrue(flags_zero.zero)

    def test_inc_wrap_flag_generation(self) -> None:
        flags_no_wrap = evaluate_src2_status_flags(
            DSOpcode.DS_INC_SRC2_U32, 2, 5, 3
        )
        self.assertFalse(flags_no_wrap.wrapped)
        self.assertFalse(flags_no_wrap.carry)
        self.assertFalse(flags_no_wrap.zero)

        flags_wrap = evaluate_src2_status_flags(
            DSOpcode.DS_INC_SRC2_U32, 5, 5, 0
        )
        self.assertTrue(flags_wrap.wrapped)
        self.assertTrue(flags_wrap.carry)
        self.assertTrue(flags_wrap.zero)

        flags_over = evaluate_src2_status_flags(
            DSOpcode.DS_INC_SRC2_U32, 10, 5, 0
        )
        self.assertTrue(flags_over.wrapped)
        self.assertTrue(flags_over.carry)
        self.assertTrue(flags_over.zero)

    def test_dec_wrap_flag_generation(self) -> None:
        flags_zero = evaluate_src2_status_flags(
            DSOpcode.DS_DEC_SRC2_U32, 0, 8, 8
        )
        self.assertTrue(flags_zero.wrapped)
        self.assertTrue(flags_zero.borrow)
        self.assertFalse(flags_zero.zero)

        flags_greater = evaluate_src2_status_flags(
            DSOpcode.DS_DEC_SRC2_U32, 12, 8, 8
        )
        self.assertTrue(flags_greater.wrapped)
        self.assertTrue(flags_greater.borrow)

        flags_norm = evaluate_src2_status_flags(
            DSOpcode.DS_DEC_SRC2_U32, 5, 8, 4
        )
        self.assertFalse(flags_norm.wrapped)
        self.assertFalse(flags_norm.borrow)


class TestIEEE754BoundaryExtremes(unittest.TestCase):
    """Unit tests for IEEE 754 boundary extremes, subnormals, and NaN handling."""

    def test_nan_as_wrap_threshold_inc(self) -> None:
        self.assertEqual(ref_ds_inc_src2_u32(0, QNAN_F32), 1)
        self.assertEqual(ref_ds_inc_src2_u32(QNAN_F32, QNAN_F32), 0)
        self.assertEqual(ref_ds_inc_src2_u32(NEG_ZERO_F32, QNAN_F32), 0)

    def test_subnormal_increment_and_transitions(self) -> None:
        val = MIN_SUBNORMAL_F32
        res = ref_ds_inc_src2_u32(val, POS_INF_F32)
        self.assertEqual(res, 0x00000002)
        self.assertTrue(is_f32_subnormal(res))

        res_norm = ref_ds_inc_src2_u32(MAX_SUBNORMAL_F32, POS_INF_F32)
        self.assertEqual(res_norm, MIN_NORMAL_F32)
        self.assertFalse(is_f32_subnormal(res_norm))

    def test_float_zeros_unsigned_asymmetry(self) -> None:
        self.assertEqual(ref_ds_inc_src2_u32(POS_ZERO_F32, NEG_ZERO_F32), 1)
        self.assertEqual(ref_ds_inc_src2_u32(NEG_ZERO_F32, POS_ZERO_F32), 0)
        self.assertEqual(ref_ds_dec_src2_u32(POS_ZERO_F32, NEG_ZERO_F32), NEG_ZERO_F32)
        self.assertEqual(ref_ds_dec_src2_u32(NEG_ZERO_F32, POS_ZERO_F32), POS_ZERO_F32)
        self.assertEqual(ref_ds_add_src2_u32(NEG_ZERO_F32, NEG_ZERO_F32), POS_ZERO_F32)

    def test_infinity_threshold_arithmetic(self) -> None:
        self.assertEqual(ref_ds_add_src2_u32(MAX_NORMAL_F32, 1), POS_INF_F32)
        self.assertEqual(ref_ds_sub_src2_u32(POS_INF_F32, 1), MAX_NORMAL_F32)

    def test_integer_boundary_extremes(self) -> None:
        self.assertEqual(ref_ds_inc_src2_u32(MASK32, MASK32), 0)
        self.assertEqual(ref_ds_dec_src2_u32(0, MASK32), MASK32)
        self.assertEqual(ref_ds_add_src2_u32(MASK32, MASK32), MASK32 - 1)
        self.assertEqual(ref_ds_sub_src2_u32(0, MASK32), 1)


class TestNegativeControlsAndMutations(unittest.TestCase):
    """Unit tests for negative controls, syntax rejection, and mutation detection."""

    def test_syntax_rejection_registers(self) -> None:
        with self.assertRaises(ValueError):
            RegisterOperand(-1, RegType.VGPR)
        with self.assertRaises(ValueError):
            RegisterOperand(256, RegType.VGPR)
        with self.assertRaises(ValueError):
            RegisterOperand(500, RegType.SGPR)

    def test_syntax_rejection_offsets(self) -> None:
        v0 = RegisterOperand(0, RegType.VGPR)
        with self.assertRaises(ValueError):
            LDSAddressExpression(v0, offset0=-1)
        with self.assertRaises(ValueError):
            LDSAddressExpression(v0, offset0=256)
        with self.assertRaises(ValueError):
            LDSAddressExpression(v0, offset1=-1)
        with self.assertRaises(ValueError):
            LDSAddressExpression(v0, offset1=256)

    def test_syntax_rejection_lds_capacity(self) -> None:
        with self.assertRaises(ValueError):
            LDSMemory(size_dwords=0)
        with self.assertRaises(ValueError):
            LDSMemory(size_dwords=-100)
        with self.assertRaises(ValueError):
            LDSMemory(size_dwords=1000)  # Not a power of 2

    def test_syntax_rejection_simd_lanes(self) -> None:
        with self.assertRaises(ValueError):
            SIMDState(num_lanes=16)
        with self.assertRaises(ValueError):
            SIMDState(num_lanes=128)

    def test_mutation_detection_inc_strict_greater(self) -> None:
        # Mutant: wraps only on `val_a > val_b` instead of `>=`
        def mutant_inc(a: int, b: int) -> int:
            return 0 if u32(a) > u32(b) else u32(a + 1)

        # Equality case must fail on mutant!
        val_a, val_b = 5, 5
        correct_res = ref_ds_inc_src2_u32(val_a, val_b)
        mutant_res = mutant_inc(val_a, val_b)
        self.assertEqual(correct_res, 0)
        self.assertEqual(mutant_res, 6)
        self.assertNotEqual(correct_res, mutant_res)

    def test_mutation_detection_dec_missing_zero(self) -> None:
        # Mutant: forgets the `val_a == 0` wrap check
        def mutant_dec(a: int, b: int) -> int:
            return u32(b) if u32(a) > u32(b) else u32(a - 1)

        val_a, val_b = 0, 8
        correct_res = ref_ds_dec_src2_u32(val_a, val_b)
        mutant_res = mutant_dec(val_a, val_b)
        self.assertEqual(correct_res, 8)
        self.assertEqual(mutant_res, MASK32)
        self.assertNotEqual(correct_res, mutant_res)

    def test_mutation_detection_sub_reversed(self) -> None:
        # Mutant: subtracts minuend from subtrahend
        def mutant_sub(a: int, b: int) -> int:
            return u32(u32(b) - u32(a))

        val_a, val_b = 20, 5
        correct_res = ref_ds_sub_src2_u32(val_a, val_b)
        mutant_res = mutant_sub(val_a, val_b)
        self.assertEqual(correct_res, 15)
        self.assertEqual(mutant_res, u32(-15))
        self.assertNotEqual(correct_res, mutant_res)

    def test_mutation_detection_simd_exec_leak(self) -> None:
        # Mutant interpreter that executes regardless of EXEC mask
        class MutantSIMDInterpreter(SIMDInterpreter):
            def execute(self, instr: DSSrc2Instruction, state: SIMDState) -> None:
                for lane in range(state.num_lanes):
                    # Bug: missing `if not state.is_lane_active(lane): continue`
                    base_addr = state.get_lane_vgpr(lane, instr.addr.base_reg.index)
                    _, _, idx_a, _ = calc_lds_src2_addresses(
                        vaddr=base_addr,
                        offset0=instr.addr.offset0,
                        offset1=instr.addr.offset1,
                        lds_dwords=state.lds.size_dwords,
                    )
                    state.lds.write_dword(idx_a, 999)

        state = SIMDState(num_lanes=32, exec_mask=0, lds_dwords=1024)
        v0 = RegisterOperand(0, RegType.VGPR)
        instr = DSSrc2Instruction(
            DSOpcode.DS_ADD_SRC2_U32,
            LDSAddressExpression(v0, offset0=0, offset1=1),
        )
        state.lds.write_dword(0, 42)

        mutant = MutantSIMDInterpreter()
        mutant.execute(instr, state)
        # Mutant erroneously modified LDS dword 0 from 42 to 999 despite EXEC=0
        self.assertEqual(state.lds.read_dword(0), 999)

        # Standard interpreter correctly preserves 42
        state.lds.write_dword(0, 42)
        standard = SIMDInterpreter()
        standard.execute(instr, state)
        self.assertEqual(state.lds.read_dword(0), 42)

    def test_mutation_detection_vaddr_clobber(self) -> None:
        # Mutant interpreter that corrupts base_reg VGPR
        class MutantScalarInterpreter(ScalarInterpreter):
            def execute(self, instr: DSSrc2Instruction, state: ScalarMachineState) -> None:
                super().execute(instr, state)
                # Bug: clobbers base_reg
                state.set_vgpr(instr.addr.base_reg.index, 0xDEADBEEF)

        state = ScalarMachineState(lds_dwords=1024)
        state.set_vgpr(0, 0x100)
        v0 = RegisterOperand(0, RegType.VGPR)
        instr = DSSrc2Instruction(
            DSOpcode.DS_INC_SRC2_U32,
            LDSAddressExpression(v0, offset0=0, offset1=1),
        )

        mutant = MutantScalarInterpreter()
        mutant.execute(instr, state)
        self.assertEqual(state.get_vgpr(0), 0xDEADBEEF)

        # Reset and verify correct interpreter never clobbers vaddr
        state.set_vgpr(0, 0x100)
        standard = ScalarInterpreter()
        standard.execute(instr, state)
        self.assertEqual(state.get_vgpr(0), 0x100)


if __name__ == "__main__":
    unittest.main()
