"""Semantic interpreter tests and reference mathematical oracle for RDNA2 ds_min_max_f32.

Models IEEE 754-2008 / IEEE 754-2019 binary32 single-precision minNum/maxNum and AMD RDNA2 / GFX10
Local Data Share (LDS) architectural semantics:
  - ds_min_f32: Atomic float minimum on LDS storage.
  - ds_max_f32: Atomic float maximum on LDS storage.
  - ds_min_rtn_f32: Atomic float minimum on LDS storage returning previous value to VGPR.
  - ds_max_rtn_f32: Atomic float maximum on LDS storage returning previous value to VGPR.

RDNA2 LDS / Floating-Point ISA Rules:
  1. Local Data Share Architecture:
     LDS is an on-chip, software-managed scratchpad memory shared across all threads
     in a workgroup. Addressing is byte-addressed but dword-aligned in hardware (low 2 bits
     dropped). Out-of-bounds addresses wrap within the allocated power-of-two LDS allocation.
     LDS is divided into 32 memory banks (bank = dword_index % 32).
  2. NaN Suppression (IEEE 754-2008 / 2019 minNum / maxNum):
     If one operand is NaN and the other is a valid floating-point number, the valid number is
     returned unchanged. If both operands are NaN, a quiet NaN is returned.
  3. Signed Zero Ordering (-0.0 < +0.0):
     RDNA2 orders -0.0 strictly less than +0.0:
       min(+0.0, -0.0) = -0.0, min(-0.0, +0.0) = -0.0
       max(+0.0, -0.0) = +0.0, max(-0.0, +0.0) = +0.0
  4. Infinities & Subnormals:
     -INF < all finite numbers < +INF.
     Subnormal float values are preserved without flushing to zero in IEEE mode.
  5. Atomic Operations & Return Semantics:
     Non-returning variants (ds_min_f32, ds_max_f32) discard the previous memory content.
     Returning variants (ds_min_rtn_f32, ds_max_rtn_f32) atomically fetch the previous
     32-bit floating-point value and write it into the destination VGPR.

Standard library Python only; zero third-party dependencies; zero game data.
Python 3.9+ compatible (from __future__ import annotations).
"""
from __future__ import annotations

import dataclasses
import enum
import math
import os
import struct
import sys
import unittest
from typing import Tuple, List, Optional, Dict, Any

# IEEE 754 binary32 architectural bitmasks
MASK32: int = 0xFFFFFFFF
SIGN_MASK: int = 0x80000000
EXP_MASK: int = 0x7F800000
FRAC_MASK: int = 0x007FFFFF
QNAN_BIT: int = 0x00400000
CANONICAL_QNAN: int = 0x7FC00000

# Canonical IEEE 754 binary32 bit patterns
POS_ZERO: int = 0x00000000
NEG_ZERO: int = 0x80000000
ONE_F32: int = 0x3F800000           # 1.0
NEG_ONE_F32: int = 0xBF800000       # -1.0
HALF_F32: int = 0x3F000000          # 0.5
NEG_HALF_F32: int = 0xBF000000      # -0.5
TWO_F32: int = 0x40000000           # 2.0
NEG_TWO_F32: int = 0xC0000000       # -2.0
THREE_F32: int = 0x40400000         # 3.0
TEN_F32: int = 0x41200000           # 10.0
NEG_TEN_F32: int = 0xC1200000       # -10.0

FLT_MIN: int = 0x00800000           # Smallest positive normal: 2^-126
NEG_FLT_MIN: int = 0x80800000       # -2^-126
FLT_MAX: int = 0x7F7FFFFF           # (2 - 2^-23) * 2^127
NEG_FLT_MAX: int = 0xFF7FFFFF
POS_INF: int = 0x7F800000           # +infinity
NEG_INF: int = 0xFF800000           # -infinity
QNAN_PAYLOAD_A: int = 0x7FC10001    # Quiet NaN with payload A
QNAN_PAYLOAD_B: int = 0xFFC20002    # Quiet NaN with payload B
SNAN_PAYLOAD_A: int = 0x7F810001    # Signaling NaN (MSB of mantissa is 0, payload != 0)
SNAN_PAYLOAD_B: int = 0xFF820002    # Signaling NaN
SUBNORM_MIN: int = 0x00000001       # Smallest subnormal: 2^-149
SUBNORM_MAX: int = 0x007FFFFF       # Largest positive subnormal: (1 - 2^-23) * 2^-126
NEG_SUBNORM_MIN: int = 0x80000001
NEG_SUBNORM_MAX: int = 0x807FFFFF

DEFAULT_LDS_SIZE_BYTES: int = 65536  # 64 KiB
DEFAULT_LDS_NUM_DWORDS: int = 16384
NUM_LDS_BANKS: int = 32


def f32_to_bits(val: float) -> int:
    """Encode float to IEEE 754 single-precision 32-bit unsigned integer."""
    try:
        return struct.unpack("<I", struct.pack("<f", val))[0]
    except OverflowError:
        return POS_INF if val > 0 else NEG_INF


def bits_to_f32(bits: int) -> float:
    """Decode 32-bit unsigned integer to IEEE 754 single-precision float."""
    return struct.unpack("<f", struct.pack("<I", bits & MASK32))[0]


def split_f32_bits(bits: int) -> Tuple[int, int, int]:
    """Unpack 32-bit integer into (sign, exponent, fraction) components."""
    bits &= MASK32
    sign = (bits & SIGN_MASK) >> 31
    exponent = (bits & EXP_MASK) >> 23
    fraction = bits & FRAC_MASK
    return sign, exponent, fraction


def is_nan(bits: int) -> bool:
    """Return True if 32-bit integer represents an IEEE 754 float NaN."""
    bits &= MASK32
    return ((bits & EXP_MASK) == EXP_MASK) and ((bits & FRAC_MASK) != 0)


def is_qnan(bits: int) -> bool:
    """Return True if 32-bit integer represents an IEEE 754 float Quiet NaN."""
    return is_nan(bits) and bool(bits & QNAN_BIT)


def is_snan(bits: int) -> bool:
    """Return True if 32-bit integer represents an IEEE 754 float Signaling NaN."""
    return is_nan(bits) and not bool(bits & QNAN_BIT)


def is_inf(bits: int) -> bool:
    """Return True if 32-bit integer represents IEEE 754 positive or negative infinity."""
    bits &= MASK32
    return ((bits & EXP_MASK) == EXP_MASK) and ((bits & FRAC_MASK) == 0)


def is_subnormal(bits: int) -> bool:
    """Return True if 32-bit integer represents a subnormal single-precision float."""
    bits &= MASK32
    return ((bits & EXP_MASK) == 0) and ((bits & FRAC_MASK) != 0)


def is_zero(bits: int) -> bool:
    """Return True if 32-bit integer represents positive or negative zero."""
    return (bits & ~SIGN_MASK) == 0


def oracle_min_f32(a_bits: int, b_bits: int) -> int:
    """Reference mathematical oracle for RDNA2 ds_min_f32 atomic operation.

    Follows IEEE 754-2008/2019 minNum:
      - If one operand is NaN and the other is a valid float, returns the valid float.
      - If both are NaN, returns a quiet NaN (preserving payload or quietened).
      - Signed zero ordering: -0.0 < +0.0 (min(+0.0, -0.0) -> -0.0).
      - Subnormals and infinities preserved.
    """
    a = a_bits & MASK32
    b = b_bits & MASK32

    a_is_nan = is_nan(a)
    b_is_nan = is_nan(b)

    if a_is_nan and b_is_nan:
        # Both NaN: return quieted NaN
        return a | QNAN_BIT
    if a_is_nan:
        return b
    if b_is_nan:
        return a

    # Signed zeros
    if is_zero(a) and is_zero(b):
        # If either is negative zero, negative zero is smaller
        return NEG_ZERO if ((a == NEG_ZERO) or (b == NEG_ZERO)) else POS_ZERO

    fa = bits_to_f32(a)
    fb = bits_to_f32(b)

    if fa < fb:
        return a
    elif fb < fa:
        return b
    else:
        # Numerically equal: negative sign bit is strictly smaller
        if (a & SIGN_MASK) != 0:
            return a
        if (b & SIGN_MASK) != 0:
            return b
        return a


def oracle_max_f32(a_bits: int, b_bits: int) -> int:
    """Reference mathematical oracle for RDNA2 ds_max_f32 atomic operation.

    Follows IEEE 754-2008/2019 maxNum:
      - If one operand is NaN and the other is a valid float, returns the valid float.
      - If both are NaN, returns a quiet NaN (preserving payload or quietened).
      - Signed zero ordering: -0.0 < +0.0 (max(+0.0, -0.0) -> +0.0).
      - Subnormals and infinities preserved.
    """
    a = a_bits & MASK32
    b = b_bits & MASK32

    a_is_nan = is_nan(a)
    b_is_nan = is_nan(b)

    if a_is_nan and b_is_nan:
        # Both NaN: return quieted NaN
        return a | QNAN_BIT
    if a_is_nan:
        return b
    if b_is_nan:
        return a

    # Signed zeros
    if is_zero(a) and is_zero(b):
        # If either is positive zero, positive zero is larger
        return POS_ZERO if ((a == POS_ZERO) or (b == POS_ZERO)) else NEG_ZERO

    fa = bits_to_f32(a)
    fb = bits_to_f32(b)

    if fa > fb:
        return a
    elif fb > fa:
        return b
    else:
        # Numerically equal: positive sign bit (0) is strictly larger
        if (a & SIGN_MASK) == 0:
            return a
        if (b & SIGN_MASK) == 0:
            return b
        return a


class LDSMemory:
    """Architectural simulation model of AMD RDNA2 Local Data Share (LDS)."""

    def __init__(self, size_bytes: int = DEFAULT_LDS_SIZE_BYTES) -> None:
        if size_bytes <= 0 or (size_bytes & (size_bytes - 1)) != 0:
            raise ValueError(f"LDS size must be a positive power of 2, got {size_bytes}")
        self.size_bytes: int = size_bytes
        self.num_dwords: int = size_bytes // 4
        self.dwords: List[int] = [0] * self.num_dwords

    def get_dword_index(self, byte_addr: int, offset: int = 0) -> int:
        """Compute effective wrapped dword index, dropping bottom 2 unaligned bits."""
        effective_byte = (byte_addr + offset) & MASK32
        return (effective_byte >> 2) & (self.num_dwords - 1)

    def get_bank(self, dword_idx: int) -> int:
        """Compute memory bank index for the given dword (0 to 31)."""
        return dword_idx % NUM_LDS_BANKS

    def read_dword(self, dword_idx: int) -> int:
        """Read a 32-bit dword from LDS."""
        return self.dwords[dword_idx & (self.num_dwords - 1)]

    def write_dword(self, dword_idx: int, val: int) -> None:
        """Write a 32-bit dword into LDS."""
        self.dwords[dword_idx & (self.num_dwords - 1)] = val & MASK32

    def atomic_min_f32(self, byte_addr: int, val: int, offset: int = 0) -> int:
        """Execute atomic float minimum at (byte_addr + offset), returning previous value."""
        idx = self.get_dword_index(byte_addr, offset)
        old_val = self.dwords[idx]
        new_val = oracle_min_f32(old_val, val)
        self.dwords[idx] = new_val
        return old_val

    def atomic_max_f32(self, byte_addr: int, val: int, offset: int = 0) -> int:
        """Execute atomic float maximum at (byte_addr + offset), returning previous value."""
        idx = self.get_dword_index(byte_addr, offset)
        old_val = self.dwords[idx]
        new_val = oracle_max_f32(old_val, val)
        self.dwords[idx] = new_val
        return old_val


class TestLDSMinMaxF32ModelAndOracle(unittest.TestCase):
    """Unit tests for binary32 floating point model, predicates, and reference LDS oracle."""

    def test_bit_representation_roundtrip(self) -> None:
        values = [0.0, -0.0, 1.0, -1.0, 0.5, -2.5, 100.0, -100.0, 3.1415927]
        for val in values:
            with self.subTest(val=val):
                bits = f32_to_bits(val)
                decoded = bits_to_f32(bits)
                self.assertAlmostEqual(val, decoded, places=6)
                self.assertEqual(bits, f32_to_bits(decoded))

    def test_predicates(self) -> None:
        self.assertTrue(is_zero(POS_ZERO))
        self.assertTrue(is_zero(NEG_ZERO))
        self.assertFalse(is_zero(ONE_F32))

        self.assertTrue(is_nan(CANONICAL_QNAN))
        self.assertTrue(is_nan(QNAN_PAYLOAD_A))
        self.assertTrue(is_nan(SNAN_PAYLOAD_A))
        self.assertFalse(is_nan(POS_INF))
        self.assertFalse(is_nan(ONE_F32))

        self.assertTrue(is_qnan(CANONICAL_QNAN))
        self.assertTrue(is_qnan(QNAN_PAYLOAD_A))
        self.assertFalse(is_qnan(SNAN_PAYLOAD_A))

        self.assertTrue(is_snan(SNAN_PAYLOAD_A))
        self.assertFalse(is_snan(CANONICAL_QNAN))

        self.assertTrue(is_inf(POS_INF))
        self.assertTrue(is_inf(NEG_INF))
        self.assertFalse(is_inf(FLT_MAX))

        self.assertTrue(is_subnormal(SUBNORM_MIN))
        self.assertTrue(is_subnormal(SUBNORM_MAX))
        self.assertFalse(is_subnormal(FLT_MIN))
        self.assertFalse(is_subnormal(POS_ZERO))

    def test_oracle_min_f32_standard_values(self) -> None:
        self.assertEqual(oracle_min_f32(ONE_F32, TWO_F32), ONE_F32)
        self.assertEqual(oracle_min_f32(TWO_F32, ONE_F32), ONE_F32)
        self.assertEqual(oracle_min_f32(NEG_ONE_F32, NEG_TWO_F32), NEG_TWO_F32)
        self.assertEqual(oracle_min_f32(NEG_TEN_F32, TEN_F32), NEG_TEN_F32)

    def test_oracle_max_f32_standard_values(self) -> None:
        self.assertEqual(oracle_max_f32(ONE_F32, TWO_F32), TWO_F32)
        self.assertEqual(oracle_max_f32(TWO_F32, ONE_F32), TWO_F32)
        self.assertEqual(oracle_max_f32(NEG_ONE_F32, NEG_TWO_F32), NEG_ONE_F32)
        self.assertEqual(oracle_max_f32(NEG_TEN_F32, TEN_F32), TEN_F32)

    def test_oracle_signed_zeros(self) -> None:
        # In RDNA2 / IEEE 754-2019: -0.0 < +0.0
        self.assertEqual(oracle_min_f32(POS_ZERO, NEG_ZERO), NEG_ZERO)
        self.assertEqual(oracle_min_f32(NEG_ZERO, POS_ZERO), NEG_ZERO)
        self.assertEqual(oracle_max_f32(POS_ZERO, NEG_ZERO), POS_ZERO)
        self.assertEqual(oracle_max_f32(NEG_ZERO, POS_ZERO), POS_ZERO)
        self.assertEqual(oracle_min_f32(NEG_ZERO, NEG_ZERO), NEG_ZERO)
        self.assertEqual(oracle_max_f32(POS_ZERO, POS_ZERO), POS_ZERO)

    def test_oracle_nan_suppression(self) -> None:
        # Single NaN suppressed
        self.assertEqual(oracle_min_f32(CANONICAL_QNAN, ONE_F32), ONE_F32)
        self.assertEqual(oracle_min_f32(ONE_F32, CANONICAL_QNAN), ONE_F32)
        self.assertEqual(oracle_min_f32(SNAN_PAYLOAD_A, NEG_TWO_F32), NEG_TWO_F32)
        self.assertEqual(oracle_max_f32(CANONICAL_QNAN, ONE_F32), ONE_F32)
        self.assertEqual(oracle_max_f32(ONE_F32, CANONICAL_QNAN), ONE_F32)
        self.assertEqual(oracle_max_f32(SNAN_PAYLOAD_A, NEG_TWO_F32), NEG_TWO_F32)

        # Dual NaN produces quiet NaN
        res_min = oracle_min_f32(CANONICAL_QNAN, QNAN_PAYLOAD_B)
        self.assertTrue(is_qnan(res_min))
        res_max = oracle_max_f32(SNAN_PAYLOAD_A, SNAN_PAYLOAD_B)
        self.assertTrue(is_qnan(res_max))

    def test_oracle_infinities_and_subnormals(self) -> None:
        self.assertEqual(oracle_min_f32(POS_INF, FLT_MAX), FLT_MAX)
        self.assertEqual(oracle_min_f32(NEG_INF, NEG_FLT_MAX), NEG_INF)
        self.assertEqual(oracle_max_f32(POS_INF, FLT_MAX), POS_INF)
        self.assertEqual(oracle_max_f32(NEG_INF, NEG_FLT_MAX), NEG_FLT_MAX)

        self.assertEqual(oracle_min_f32(SUBNORM_MIN, SUBNORM_MAX), SUBNORM_MIN)
        self.assertEqual(oracle_max_f32(SUBNORM_MIN, SUBNORM_MAX), SUBNORM_MAX)
        self.assertEqual(oracle_min_f32(NEG_SUBNORM_MIN, NEG_ZERO), NEG_SUBNORM_MIN)
        self.assertEqual(oracle_max_f32(SUBNORM_MIN, POS_ZERO), SUBNORM_MIN)

    def test_lds_memory_model(self) -> None:
        lds = LDSMemory(65536)
        self.assertEqual(lds.num_dwords, 16384)

        # Byte address to dword index drops low 2 bits
        self.assertEqual(lds.get_dword_index(0x0004), 1)
        self.assertEqual(lds.get_dword_index(0x0007), 1)
        self.assertEqual(lds.get_dword_index(0x0008, offset=4), 3)

        # Address wrapping
        self.assertEqual(lds.get_dword_index(65536 + 16), 4)

        # Bank calculation
        self.assertEqual(lds.get_bank(0), 0)
        self.assertEqual(lds.get_bank(31), 31)
        self.assertEqual(lds.get_bank(32), 0)

        # Atomic min f32
        lds.write_dword(10, TWO_F32)
        old_val = lds.atomic_min_f32(10 * 4, ONE_F32)
        self.assertEqual(old_val, TWO_F32)
        self.assertEqual(lds.read_dword(10), ONE_F32)

        # Atomic max f32
        old_val2 = lds.atomic_max_f32(10 * 4, THREE_F32)
        self.assertEqual(old_val2, ONE_F32)
        self.assertEqual(lds.read_dword(10), THREE_F32)


class DSOpcode(str, enum.Enum):
    """Opcode enumerations for single-precision LDS min/max operations."""
    DS_MIN_F32 = "ds_min_f32"
    DS_MAX_F32 = "ds_max_f32"
    DS_MIN_RTN_F32 = "ds_min_rtn_f32"
    DS_MAX_RTN_F32 = "ds_max_rtn_f32"

    @property
    def is_return(self) -> bool:
        """True if the instruction returns the previous LDS value to a destination VGPR."""
        return self in (DSOpcode.DS_MIN_RTN_F32, DSOpcode.DS_MAX_RTN_F32)

    @property
    def is_min(self) -> bool:
        """True if operation is a floating-point minimum."""
        return self in (DSOpcode.DS_MIN_F32, DSOpcode.DS_MIN_RTN_F32)

    @property
    def is_max(self) -> bool:
        """True if operation is a floating-point maximum."""
        return self in (DSOpcode.DS_MAX_F32, DSOpcode.DS_MAX_RTN_F32)


@dataclasses.dataclass(frozen=True)
class VGPR:
    """Vector General Purpose Register operand v[0] .. v[255]."""
    index: int

    def __post_init__(self) -> None:
        if not (0 <= self.index <= 255):
            raise ValueError(f"VGPR index out of range [0, 255]: {self.index}")

    def __str__(self) -> str:
        return f"v{self.index}"


@dataclasses.dataclass(frozen=True)
class OffsetImmediate:
    """Unsigned 16-bit byte offset immediate [0, 65535]."""
    value: int = 0

    def __post_init__(self) -> None:
        if not (0 <= self.value <= 65535):
            raise ValueError(f"Offset immediate out of range [0, 65535]: {self.value}")

    def __str__(self) -> str:
        return f"offset:{self.value}" if self.value != 0 else ""


@dataclasses.dataclass(frozen=True)
class DSMinMaxInstruction:
    """Symbolic AST node representing an RDNA2 DS min/max f32 instruction."""
    opcode: DSOpcode
    vaddr: VGPR
    vdata: VGPR
    vdst: Optional[VGPR] = None
    offset: OffsetImmediate = OffsetImmediate(0)

    def __post_init__(self) -> None:
        if self.opcode.is_return:
            if self.vdst is None:
                raise ValueError(f"Returning opcode {self.opcode.value} requires destination vdst")
        else:
            if self.vdst is not None:
                raise ValueError(f"Non-returning opcode {self.opcode.value} must not have destination vdst")

    def disasm(self) -> str:
        """Format symbolic instruction into canonical RDNA2 assembly text."""
        parts = [self.opcode.value]
        if self.opcode.is_return:
            operands = f"{self.vdst}, {self.vaddr}, {self.vdata}"
        else:
            operands = f"{self.vaddr}, {self.vdata}"
        parts.append(operands)
        if self.offset.value != 0:
            parts.append(str(self.offset))
        return " ".join(parts)

    @classmethod
    def parse(cls, text: str) -> DSMinMaxInstruction:
        """Parse canonical RDNA2 assembly string into a DSMinMaxInstruction AST."""
        tokens = text.strip().split(None, 1)
        if not tokens:
            raise ValueError("Empty assembly string")
        op_str = tokens[0]
        try:
            opcode = DSOpcode(op_str)
        except ValueError:
            raise ValueError(f"Unknown or unsupported DS opcode: {op_str}")

        if len(tokens) == 1:
            raise ValueError(f"Missing operands for {op_str}")

        rest = tokens[1].strip()
        offset_val = 0
        if "offset:" in rest:
            m = rest.split("offset:")
            rest = m[0].strip()
            offset_tok = m[1].split()[0].rstrip(",")
            offset_val = int(offset_tok, 0)

        # Parse comma-separated registers
        reg_tokens = [tok.strip().rstrip(",") for tok in rest.split(",") if tok.strip()]
        
        def parse_vgpr(s: str) -> VGPR:
            s = s.strip()
            if not s.startswith("v") or not s[1:].isdigit():
                raise ValueError(f"Invalid VGPR syntax: {s}")
            return VGPR(int(s[1:]))

        if opcode.is_return:
            if len(reg_tokens) != 3:
                raise ValueError(f"{opcode.value} expects 3 registers (vdst, vaddr, vdata), got {reg_tokens}")
            vdst = parse_vgpr(reg_tokens[0])
            vaddr = parse_vgpr(reg_tokens[1])
            vdata = parse_vgpr(reg_tokens[2])
            return cls(opcode=opcode, vaddr=vaddr, vdata=vdata, vdst=vdst, offset=OffsetImmediate(offset_val))
        else:
            if len(reg_tokens) != 2:
                raise ValueError(f"{opcode.value} expects 2 registers (vaddr, vdata), got {reg_tokens}")
            vaddr = parse_vgpr(reg_tokens[0])
            vdata = parse_vgpr(reg_tokens[1])
            return cls(opcode=opcode, vaddr=vaddr, vdata=vdata, vdst=None, offset=OffsetImmediate(offset_val))


class TestSymbolicAST(unittest.TestCase):
    """Unit tests for symbolic AST opcode definitions, registers, and assembly parsing."""

    def test_opcode_properties(self) -> None:
        self.assertFalse(DSOpcode.DS_MIN_F32.is_return)
        self.assertTrue(DSOpcode.DS_MIN_F32.is_min)
        self.assertFalse(DSOpcode.DS_MIN_F32.is_max)

        self.assertFalse(DSOpcode.DS_MAX_F32.is_return)
        self.assertFalse(DSOpcode.DS_MAX_F32.is_min)
        self.assertTrue(DSOpcode.DS_MAX_F32.is_max)

        self.assertTrue(DSOpcode.DS_MIN_RTN_F32.is_return)
        self.assertTrue(DSOpcode.DS_MIN_RTN_F32.is_min)
        self.assertFalse(DSOpcode.DS_MIN_RTN_F32.is_max)

        self.assertTrue(DSOpcode.DS_MAX_RTN_F32.is_return)
        self.assertFalse(DSOpcode.DS_MAX_RTN_F32.is_min)
        self.assertTrue(DSOpcode.DS_MAX_RTN_F32.is_max)

    def test_vgpr_validation(self) -> None:
        v0 = VGPR(0)
        v255 = VGPR(255)
        self.assertEqual(str(v0), "v0")
        self.assertEqual(str(v255), "v255")

        with self.assertRaises(ValueError):
            VGPR(-1)
        with self.assertRaises(ValueError):
            VGPR(256)

    def test_offset_immediate_validation(self) -> None:
        off0 = OffsetImmediate(0)
        off64 = OffsetImmediate(64)
        off_max = OffsetImmediate(65535)
        self.assertEqual(str(off0), "")
        self.assertEqual(str(off64), "offset:64")
        self.assertEqual(str(off_max), "offset:65535")

        with self.assertRaises(ValueError):
            OffsetImmediate(-1)
        with self.assertRaises(ValueError):
            OffsetImmediate(65536)

    def test_instruction_post_init_validation(self) -> None:
        # Non-returning must have vdst is None
        DSMinMaxInstruction(opcode=DSOpcode.DS_MIN_F32, vaddr=VGPR(0), vdata=VGPR(1))
        with self.assertRaises(ValueError):
            DSMinMaxInstruction(opcode=DSOpcode.DS_MIN_F32, vaddr=VGPR(0), vdata=VGPR(1), vdst=VGPR(2))

        # Returning requires vdst
        DSMinMaxInstruction(opcode=DSOpcode.DS_MIN_RTN_F32, vaddr=VGPR(0), vdata=VGPR(1), vdst=VGPR(2))
        with self.assertRaises(ValueError):
            DSMinMaxInstruction(opcode=DSOpcode.DS_MIN_RTN_F32, vaddr=VGPR(0), vdata=VGPR(1), vdst=None)

    def test_disasm_and_parse_roundtrip(self) -> None:
        test_cases = [
            DSMinMaxInstruction(DSOpcode.DS_MIN_F32, VGPR(2), VGPR(3)),
            DSMinMaxInstruction(DSOpcode.DS_MIN_F32, VGPR(2), VGPR(3), offset=OffsetImmediate(16)),
            DSMinMaxInstruction(DSOpcode.DS_MAX_F32, VGPR(10), VGPR(11), offset=OffsetImmediate(128)),
            DSMinMaxInstruction(DSOpcode.DS_MIN_RTN_F32, VGPR(4), VGPR(5), vdst=VGPR(0)),
            DSMinMaxInstruction(DSOpcode.DS_MAX_RTN_F32, VGPR(8), VGPR(9), vdst=VGPR(7), offset=OffsetImmediate(256)),
        ]
        for inst in test_cases:
            with self.subTest(inst=inst):
                asm = inst.disasm()
                parsed = DSMinMaxInstruction.parse(asm)
                self.assertEqual(inst, parsed)


class ScalarThreadState:
    """Architectural state of a single scalar thread executing LDS instructions."""

    def __init__(self, lds: Optional[LDSMemory] = None) -> None:
        self.vgprs: List[int] = [0] * 256
        self.lds: LDSMemory = lds if lds is not None else LDSMemory()

    def get_vgpr(self, reg: VGPR) -> int:
        """Read a 32-bit register value."""
        return self.vgprs[reg.index] & MASK32

    def set_vgpr(self, reg: VGPR, val: int) -> None:
        """Write a 32-bit register value."""
        self.vgprs[reg.index] = val & MASK32


class ScalarDSInterpreter:
    """Deterministic scalar instruction interpreter for RDNA2 DS min/max f32 opcodes."""

    def execute(self, state: ScalarThreadState, inst: DSMinMaxInstruction) -> Optional[int]:
        """Execute a single DS min/max instruction on the scalar thread state."""
        addr = state.get_vgpr(inst.vaddr)
        data = state.get_vgpr(inst.vdata)

        if inst.opcode.is_min:
            old_val = state.lds.atomic_min_f32(addr, data, offset=inst.offset.value)
        elif inst.opcode.is_max:
            old_val = state.lds.atomic_max_f32(addr, data, offset=inst.offset.value)
        else:
            raise ValueError(f"Unsupported opcode: {inst.opcode}")

        if inst.opcode.is_return:
            assert inst.vdst is not None
            state.set_vgpr(inst.vdst, old_val)
            return old_val
        return None


class TestScalarInterpreter(unittest.TestCase):
    """Unit tests for deterministic scalar instruction execution of DS min/max f32."""

    def setUp(self) -> None:
        self.lds = LDSMemory(65536)
        self.state = ScalarThreadState(lds=self.lds)
        self.interp = ScalarDSInterpreter()

    def test_scalar_ds_min_f32_non_returning(self) -> None:
        # Initial LDS value at dword 5 is 10.0
        self.lds.write_dword(5, TEN_F32)
        # v1 holds byte address 20 (dword 5), v2 holds 2.0
        self.state.set_vgpr(VGPR(1), 20)
        self.state.set_vgpr(VGPR(2), TWO_F32)

        inst = DSMinMaxInstruction(DSOpcode.DS_MIN_F32, vaddr=VGPR(1), vdata=VGPR(2))
        res = self.interp.execute(self.state, inst)

        self.assertIsNone(res)
        # LDS at dword 5 should now be 2.0
        self.assertEqual(self.lds.read_dword(5), TWO_F32)
        # Registers v1 and v2 unchanged
        self.assertEqual(self.state.get_vgpr(VGPR(1)), 20)
        self.assertEqual(self.state.get_vgpr(VGPR(2)), TWO_F32)

    def test_scalar_ds_max_f32_non_returning(self) -> None:
        self.lds.write_dword(8, ONE_F32)
        self.state.set_vgpr(VGPR(1), 32)  # dword 8
        self.state.set_vgpr(VGPR(2), THREE_F32)

        inst = DSMinMaxInstruction(DSOpcode.DS_MAX_F32, vaddr=VGPR(1), vdata=VGPR(2))
        res = self.interp.execute(self.state, inst)

        self.assertIsNone(res)
        self.assertEqual(self.lds.read_dword(8), THREE_F32)

    def test_scalar_ds_min_rtn_f32_returning(self) -> None:
        self.lds.write_dword(12, FIVE_F32 := f32_to_bits(5.0))
        self.state.set_vgpr(VGPR(1), 48)  # dword 12
        self.state.set_vgpr(VGPR(2), TWO_F32)
        self.state.set_vgpr(VGPR(0), 0xDEADBEEF)

        inst = DSMinMaxInstruction(DSOpcode.DS_MIN_RTN_F32, vaddr=VGPR(1), vdata=VGPR(2), vdst=VGPR(0))
        res = self.interp.execute(self.state, inst)

        # Returns old LDS value 5.0
        self.assertEqual(res, FIVE_F32)
        self.assertEqual(self.state.get_vgpr(VGPR(0)), FIVE_F32)
        # LDS updated to min(5.0, 2.0) = 2.0
        self.assertEqual(self.lds.read_dword(12), TWO_F32)

    def test_scalar_ds_max_rtn_f32_returning(self) -> None:
        self.lds.write_dword(15, TWO_F32)
        self.state.set_vgpr(VGPR(1), 60)  # dword 15
        self.state.set_vgpr(VGPR(2), TEN_F32)
        self.state.set_vgpr(VGPR(5), 0)

        inst = DSMinMaxInstruction(DSOpcode.DS_MAX_RTN_F32, vaddr=VGPR(1), vdata=VGPR(2), vdst=VGPR(5))
        res = self.interp.execute(self.state, inst)

        self.assertEqual(res, TWO_F32)
        self.assertEqual(self.state.get_vgpr(VGPR(5)), TWO_F32)
        self.assertEqual(self.lds.read_dword(15), TEN_F32)

    def test_scalar_offset_and_unaligned_address(self) -> None:
        # Target dword 20 (byte 80). Pass base addr 65 (dword 16 unaligned) with offset 15 -> 80
        self.lds.write_dword(20, TEN_F32)
        self.state.set_vgpr(VGPR(1), 65)
        self.state.set_vgpr(VGPR(2), ONE_F32)

        inst = DSMinMaxInstruction(
            DSOpcode.DS_MIN_RTN_F32,
            vaddr=VGPR(1),
            vdata=VGPR(2),
            vdst=VGPR(3),
            offset=OffsetImmediate(15),
        )
        self.interp.execute(self.state, inst)

        self.assertEqual(self.state.get_vgpr(VGPR(3)), TEN_F32)
        self.assertEqual(self.lds.read_dword(20), ONE_F32)

    def test_sequential_atomic_chain(self) -> None:
        # Chain several min/max operations on the same LDS dword
        self.lds.write_dword(0, f32_to_bits(10.0))
        self.state.set_vgpr(VGPR(1), 0)  # addr

        # Step 1: min with 15.0 -> remains 10.0
        self.state.set_vgpr(VGPR(2), f32_to_bits(15.0))
        self.interp.execute(self.state, DSMinMaxInstruction(DSOpcode.DS_MIN_F32, VGPR(1), VGPR(2)))
        self.assertEqual(self.lds.read_dword(0), f32_to_bits(10.0))

        # Step 2: min with 3.0 -> becomes 3.0
        self.state.set_vgpr(VGPR(2), f32_to_bits(3.0))
        self.interp.execute(self.state, DSMinMaxInstruction(DSOpcode.DS_MIN_F32, VGPR(1), VGPR(2)))
        self.assertEqual(self.lds.read_dword(0), f32_to_bits(3.0))

        # Step 3: max with 7.0 -> becomes 7.0
        self.state.set_vgpr(VGPR(2), f32_to_bits(7.0))
        self.interp.execute(self.state, DSMinMaxInstruction(DSOpcode.DS_MAX_F32, VGPR(1), VGPR(2)))
        self.assertEqual(self.lds.read_dword(0), f32_to_bits(7.0))

        # Step 4: max rtn with 2.0 -> remains 7.0, returns 7.0
        self.state.set_vgpr(VGPR(2), f32_to_bits(2.0))
        ret = self.interp.execute(self.state, DSMinMaxInstruction(DSOpcode.DS_MAX_RTN_F32, VGPR(1), VGPR(2), vdst=VGPR(4)))
        self.assertEqual(ret, f32_to_bits(7.0))
        self.assertEqual(self.state.get_vgpr(VGPR(4)), f32_to_bits(7.0))
        self.assertEqual(self.lds.read_dword(0), f32_to_bits(7.0))


class SIMDWaveState:
    """Architectural wavefront execution state for 32 or 64 lanes."""

    def __init__(self, wave_size: int = 32, exec_mask: int = 0xFFFFFFFF, lds: Optional[LDSMemory] = None) -> None:
        if wave_size not in (32, 64):
            raise ValueError(f"Wavefront size must be 32 or 64, got {wave_size}")
        self.wave_size: int = wave_size
        self.exec_mask: int = exec_mask & ((1 << wave_size) - 1)
        self.vgprs: List[List[int]] = [[0] * 256 for _ in range(wave_size)]
        self.lds: LDSMemory = lds if lds is not None else LDSMemory()

    def is_lane_active(self, lane: int) -> bool:
        """Return True if the given lane is enabled in the current EXEC mask."""
        return bool((self.exec_mask >> lane) & 1)

    def get_vgpr(self, lane: int, reg: VGPR) -> int:
        """Read 32-bit VGPR for a specific lane."""
        return self.vgprs[lane][reg.index] & MASK32

    def set_vgpr(self, lane: int, reg: VGPR, val: int) -> None:
        """Write 32-bit VGPR for a specific lane."""
        self.vgprs[lane][reg.index] = val & MASK32


class SIMDDSInterpreter:
    """Multi-lane SIMD instruction interpreter modeling RDNA2 lockstep execution and LDS atomics."""

    def execute(self, state: SIMDWaveState, inst: DSMinMaxInstruction) -> Dict[int, int]:
        """Execute DS min/max instruction across all active lanes of the wave.
        
        Returns a dict mapping {active_lane: return_val} for returning opcodes.
        """
        # Step 1: Latch inputs for all active lanes
        active_lanes = [lane for lane in range(state.wave_size) if state.is_lane_active(lane)]
        latched_inputs = [
            (lane, state.get_vgpr(lane, inst.vaddr), state.get_vgpr(lane, inst.vdata))
            for lane in active_lanes
        ]

        returns: Dict[int, int] = {}

        # Step 2: Concurrently execute atomic operations across active lanes
        for lane, addr, data in latched_inputs:
            if inst.opcode.is_min:
                old_val = state.lds.atomic_min_f32(addr, data, offset=inst.offset.value)
            elif inst.opcode.is_max:
                old_val = state.lds.atomic_max_f32(addr, data, offset=inst.offset.value)
            else:
                raise ValueError(f"Unsupported opcode: {inst.opcode}")

            if inst.opcode.is_return:
                assert inst.vdst is not None
                state.set_vgpr(lane, inst.vdst, old_val)
                returns[lane] = old_val

        return returns


class TestSIMDMultiLaneDivergence(unittest.TestCase):
    """Unit tests for SIMD lockstep execution, 32/64-bit EXEC masking, and address convergence."""

    def test_wave32_full_mask_divergent_addresses(self) -> None:
        lds = LDSMemory(65536)
        wave = SIMDWaveState(wave_size=32, exec_mask=0xFFFFFFFF, lds=lds)
        interp = SIMDDSInterpreter()

        # Initialize LDS dwords 0..31 with 100.0
        for i in range(32):
            lds.write_dword(i, f32_to_bits(100.0))

        # Lane i targets dword i with float value (i * 2.0)
        for lane in range(32):
            wave.set_vgpr(lane, VGPR(1), lane * 4)  # vaddr
            wave.set_vgpr(lane, VGPR(2), f32_to_bits(float(lane * 2)))  # vdata

        inst = DSMinMaxInstruction(DSOpcode.DS_MIN_F32, vaddr=VGPR(1), vdata=VGPR(2))
        interp.execute(wave, inst)

        # Each dword i should have min(100.0, i * 2.0)
        for i in range(32):
            expected = f32_to_bits(float(i * 2))
            self.assertEqual(lds.read_dword(i), expected)

    def test_wave64_full_mask_divergent_addresses(self) -> None:
        lds = LDSMemory(65536)
        wave = SIMDWaveState(wave_size=64, exec_mask=0xFFFFFFFFFFFFFFFF, lds=lds)
        interp = SIMDDSInterpreter()

        # Initialize LDS dwords 0..63 with -50.0
        for i in range(64):
            lds.write_dword(i, f32_to_bits(-50.0))

        for lane in range(64):
            wave.set_vgpr(lane, VGPR(1), lane * 4)
            wave.set_vgpr(lane, VGPR(2), f32_to_bits(float(lane)))

        inst = DSMinMaxInstruction(DSOpcode.DS_MAX_F32, vaddr=VGPR(1), vdata=VGPR(2))
        interp.execute(wave, inst)

        for i in range(64):
            expected = f32_to_bits(float(i))
            self.assertEqual(lds.read_dword(i), expected)

    def test_wave32_partial_exec_masking(self) -> None:
        lds = LDSMemory(65536)
        # Even lanes only: 0x55555555
        wave = SIMDWaveState(wave_size=32, exec_mask=0x55555555, lds=lds)
        interp = SIMDDSInterpreter()

        # Initialize LDS dwords 0..31 with 50.0
        for i in range(32):
            lds.write_dword(i, f32_to_bits(50.0))

        for lane in range(32):
            wave.set_vgpr(lane, VGPR(1), lane * 4)
            wave.set_vgpr(lane, VGPR(2), f32_to_bits(10.0))
            wave.set_vgpr(lane, VGPR(0), 0xCAFEBABE)  # destination sentinel

        inst = DSMinMaxInstruction(DSOpcode.DS_MIN_RTN_F32, vaddr=VGPR(1), vdata=VGPR(2), vdst=VGPR(0))
        returns = interp.execute(wave, inst)

        # Exactly 16 active lanes returned
        self.assertEqual(len(returns), 16)
        for lane in range(32):
            if lane % 2 == 0:
                # Active lane: LDS updated to 10.0, vdst received previous 50.0
                self.assertEqual(lds.read_dword(lane), f32_to_bits(10.0))
                self.assertEqual(wave.get_vgpr(lane, VGPR(0)), f32_to_bits(50.0))
                self.assertIn(lane, returns)
            else:
                # Inactive lane: LDS untouched at 50.0, vdst untouched at sentinel
                self.assertEqual(lds.read_dword(lane), f32_to_bits(50.0))
                self.assertEqual(wave.get_vgpr(lane, VGPR(0)), 0xCAFEBABE)
                self.assertNotIn(lane, returns)

    def test_zero_exec_mask_no_mutation(self) -> None:
        lds = LDSMemory(65536)
        wave = SIMDWaveState(wave_size=32, exec_mask=0x0, lds=lds)
        interp = SIMDDSInterpreter()

        lds.write_dword(0, f32_to_bits(42.0))
        for lane in range(32):
            wave.set_vgpr(lane, VGPR(1), 0)
            wave.set_vgpr(lane, VGPR(2), f32_to_bits(1.0))
            wave.set_vgpr(lane, VGPR(3), 0x12345678)

        inst = DSMinMaxInstruction(DSOpcode.DS_MIN_RTN_F32, vaddr=VGPR(1), vdata=VGPR(2), vdst=VGPR(3))
        returns = interp.execute(wave, inst)

        self.assertEqual(len(returns), 0)
        self.assertEqual(lds.read_dword(0), f32_to_bits(42.0))
        for lane in range(32):
            self.assertEqual(wave.get_vgpr(lane, VGPR(3)), 0x12345678)

    def test_convergent_collision_all_lanes_single_dword(self) -> None:
        # All 32 active lanes write to dword 4 with various float values
        lds = LDSMemory(65536)
        wave = SIMDWaveState(wave_size=32, exec_mask=0xFFFFFFFF, lds=lds)
        interp = SIMDDSInterpreter()

        # Initial LDS value 100.0
        lds.write_dword(4, f32_to_bits(100.0))

        # Lane values: 32.0, 31.0, ..., 1.0 (Lane 31 writes 1.0)
        for lane in range(32):
            wave.set_vgpr(lane, VGPR(1), 16)  # dword 4
            wave.set_vgpr(lane, VGPR(2), f32_to_bits(float(32 - lane)))

        inst = DSMinMaxInstruction(DSOpcode.DS_MIN_F32, vaddr=VGPR(1), vdata=VGPR(2))
        interp.execute(wave, inst)

        # LDS at dword 4 must converge to global minimum: 1.0
        self.assertEqual(lds.read_dword(4), f32_to_bits(1.0))

    def test_convergent_collision_max_f32_all_lanes(self) -> None:
        lds = LDSMemory(65536)
        wave = SIMDWaveState(wave_size=32, exec_mask=0xFFFFFFFF, lds=lds)
        interp = SIMDDSInterpreter()

        # Initial LDS value -10.0
        lds.write_dword(7, f32_to_bits(-10.0))

        for lane in range(32):
            wave.set_vgpr(lane, VGPR(1), 28)  # dword 7
            wave.set_vgpr(lane, VGPR(2), f32_to_bits(float(lane * 5)))  # max is lane 31 (155.0)

        inst = DSMinMaxInstruction(DSOpcode.DS_MAX_F32, vaddr=VGPR(1), vdata=VGPR(2))
        interp.execute(wave, inst)

        self.assertEqual(lds.read_dword(7), f32_to_bits(155.0))


class TestRegisterAliasing(unittest.TestCase):
    """Unit tests for register aliasing and in-place operand preservation."""

    def setUp(self) -> None:
        self.lds = LDSMemory(65536)
        self.state = ScalarThreadState(lds=self.lds)
        self.interp = ScalarDSInterpreter()

    def test_scalar_dst_aliases_addr(self) -> None:
        # ds_min_rtn_f32 v1, v1, v2: vdst == vaddr
        # LDS dword 3 has 50.0. v1 initially has byte address 12 (dword 3).
        # v2 has 10.0.
        self.lds.write_dword(3, f32_to_bits(50.0))
        self.state.set_vgpr(VGPR(1), 12)
        self.state.set_vgpr(VGPR(2), f32_to_bits(10.0))

        inst = DSMinMaxInstruction(DSOpcode.DS_MIN_RTN_F32, vaddr=VGPR(1), vdata=VGPR(2), vdst=VGPR(1))
        ret = self.interp.execute(self.state, inst)

        # v1 should now contain the previous LDS value (50.0), having correctly latched the address 12
        self.assertEqual(ret, f32_to_bits(50.0))
        self.assertEqual(self.state.get_vgpr(VGPR(1)), f32_to_bits(50.0))
        # v2 (data) should remain untouched at 10.0
        self.assertEqual(self.state.get_vgpr(VGPR(2)), f32_to_bits(10.0))
        # LDS at dword 3 should be min(50.0, 10.0) = 10.0
        self.assertEqual(self.lds.read_dword(3), f32_to_bits(10.0))

    def test_scalar_dst_aliases_data(self) -> None:
        # ds_max_rtn_f32 v2, v1, v2: vdst == vdata
        # LDS dword 6 has 5.0. v1 has byte address 24 (dword 6).
        # v2 initially has 25.0.
        self.lds.write_dword(6, f32_to_bits(5.0))
        self.state.set_vgpr(VGPR(1), 24)
        self.state.set_vgpr(VGPR(2), f32_to_bits(25.0))

        inst = DSMinMaxInstruction(DSOpcode.DS_MAX_RTN_F32, vaddr=VGPR(1), vdata=VGPR(2), vdst=VGPR(2))
        ret = self.interp.execute(self.state, inst)

        # v2 receives old LDS value (5.0)
        self.assertEqual(ret, f32_to_bits(5.0))
        self.assertEqual(self.state.get_vgpr(VGPR(2)), f32_to_bits(5.0))
        # v1 (addr) remains untouched at 24
        self.assertEqual(self.state.get_vgpr(VGPR(1)), 24)
        # LDS at dword 6 updated to max(5.0, 25.0) = 25.0
        self.assertEqual(self.lds.read_dword(6), f32_to_bits(25.0))

    def test_scalar_addr_aliases_data_non_returning(self) -> None:
        # ds_min_f32 v5, v5: vaddr == vdata
        # Suppose v5 holds 16 (byte address 16 -> dword 4).
        # As float bits, 16 is a tiny subnormal float (0x00000010).
        # LDS dword 4 has 1.0.
        self.lds.write_dword(4, ONE_F32)
        self.state.set_vgpr(VGPR(5), 16)

        inst = DSMinMaxInstruction(DSOpcode.DS_MIN_F32, vaddr=VGPR(5), vdata=VGPR(5))
        self.interp.execute(self.state, inst)

        # v5 register should be preserved exactly at 16
        self.assertEqual(self.state.get_vgpr(VGPR(5)), 16)
        # LDS at dword 4 should now have 16 (which is smaller than 1.0)
        self.assertEqual(self.lds.read_dword(4), 16)

    def test_scalar_triple_alias(self) -> None:
        # ds_min_rtn_f32 v0, v0, v0: vdst == vaddr == vdata
        # v0 holds 32 (byte address 32 -> dword 8).
        # LDS dword 8 has 2.0.
        self.lds.write_dword(8, TWO_F32)
        self.state.set_vgpr(VGPR(0), 32)

        inst = DSMinMaxInstruction(DSOpcode.DS_MIN_RTN_F32, vaddr=VGPR(0), vdata=VGPR(0), vdst=VGPR(0))
        ret = self.interp.execute(self.state, inst)

        # Returns old LDS value 2.0
        self.assertEqual(ret, TWO_F32)
        self.assertEqual(self.state.get_vgpr(VGPR(0)), TWO_F32)
        # LDS dword 8 updated to min(2.0, float(32)) = 32
        self.assertEqual(self.lds.read_dword(8), 32)

    def test_operand_preservation_non_aliased(self) -> None:
        # Non-aliased registers must not be corrupted
        self.lds.write_dword(10, ONE_F32)
        self.state.set_vgpr(VGPR(1), 40)
        self.state.set_vgpr(VGPR(2), TWO_F32)
        self.state.set_vgpr(VGPR(3), 0xCAFEBABE)

        inst = DSMinMaxInstruction(DSOpcode.DS_MAX_RTN_F32, vaddr=VGPR(1), vdata=VGPR(2), vdst=VGPR(3))
        self.interp.execute(self.state, inst)

        self.assertEqual(self.state.get_vgpr(VGPR(1)), 40)
        self.assertEqual(self.state.get_vgpr(VGPR(2)), TWO_F32)
        self.assertEqual(self.state.get_vgpr(VGPR(3)), ONE_F32)

    def test_simd_aliasing_under_divergence(self) -> None:
        lds = LDSMemory(65536)
        wave = SIMDWaveState(wave_size=32, exec_mask=0xAAAAAAAA, lds=lds)  # Odd lanes active
        interp = SIMDDSInterpreter()

        # Initialize LDS dwords with 10.0
        for i in range(32):
            lds.write_dword(i, f32_to_bits(10.0))

        # Each lane has vdst == vaddr (v1 == v1)
        for lane in range(32):
            wave.set_vgpr(lane, VGPR(1), lane * 4)
            wave.set_vgpr(lane, VGPR(2), f32_to_bits(float(lane)))

        inst = DSMinMaxInstruction(DSOpcode.DS_MIN_RTN_F32, vaddr=VGPR(1), vdata=VGPR(2), vdst=VGPR(1))
        returns = interp.execute(wave, inst)

        for lane in range(32):
            if lane % 2 != 0:
                # Active lane: v1 received previous LDS value (10.0)
                self.assertEqual(wave.get_vgpr(lane, VGPR(1)), f32_to_bits(10.0))
                # LDS updated
                expected_min = f32_to_bits(min(10.0, float(lane)))
                self.assertEqual(lds.read_dword(lane), expected_min)
            else:
                # Inactive lane: v1 retained its initial address (lane * 4)
                self.assertEqual(wave.get_vgpr(lane, VGPR(1)), lane * 4)
                # LDS untouched
                self.assertEqual(lds.read_dword(lane), f32_to_bits(10.0))


@dataclasses.dataclass
class LDSStatusFlags:
    """Architectural status flags, IEEE exceptions, and LDS condition codes."""
    invalid_op: bool = False
    denorm: bool = False
    carry_out: bool = False
    unaligned_address: bool = False
    oob_wrapped: bool = False
    bank_conflicts: int = 0
    address_collisions: int = 0


def evaluate_status_flags(
    wave: SIMDWaveState,
    inst: DSMinMaxInstruction,
) -> LDSStatusFlags:
    """Evaluate architectural flags, IEEE exception conditions, and bank collisions."""
    flags = LDSStatusFlags()
    active_lanes = [l for l in range(wave.wave_size) if wave.is_lane_active(l)]

    bank_to_dwords: Dict[int, set[int]] = {}
    dword_to_lanes: Dict[int, list[int]] = {}

    for lane in active_lanes:
        addr = wave.get_vgpr(lane, inst.vaddr)
        data = wave.get_vgpr(lane, inst.vdata)
        off = inst.offset.value

        # Carry out detection: 32-bit addition overflow
        if (addr + off) > MASK32:
            flags.carry_out = True

        effective_byte = (addr + off) & MASK32
        if (effective_byte & 3) != 0:
            flags.unaligned_address = True

        if effective_byte >= wave.lds.size_bytes:
            flags.oob_wrapped = True

        dword_idx = wave.lds.get_dword_index(addr, off)
        bank_idx = wave.lds.get_bank(dword_idx)

        bank_to_dwords.setdefault(bank_idx, set()).add(dword_idx)
        dword_to_lanes.setdefault(dword_idx, []).append(lane)

        # Current LDS value
        curr_lds = wave.lds.read_dword(dword_idx)

        # IEEE exception: Signaling NaN triggers invalid operation
        if is_snan(data) or is_snan(curr_lds):
            flags.invalid_op = True

        # IEEE exception: subnormal operand
        if is_subnormal(data) or is_subnormal(curr_lds):
            flags.denorm = True

    # Bank conflicts: multiple distinct dwords mapping to the same bank
    for bank, dword_set in bank_to_dwords.items():
        if len(dword_set) > 1:
            flags.bank_conflicts += (len(dword_set) - 1)

    # Address collisions: multiple lanes hitting the exact same dword
    for dword, lanes in dword_to_lanes.items():
        if len(lanes) > 1:
            flags.address_collisions += (len(lanes) - 1)

    return flags


class TestConditionCodesAndStatusFlags(unittest.TestCase):
    """Unit tests for condition codes, IEEE status flags, and bank conflict metrics."""

    def test_snan_triggers_invalid_op_flag(self) -> None:
        wave = SIMDWaveState(wave_size=32, exec_mask=0x1)
        # vaddr = 0, vdata = SNAN
        wave.set_vgpr(0, VGPR(1), 0)
        wave.set_vgpr(0, VGPR(2), SNAN_PAYLOAD_A)

        inst = DSMinMaxInstruction(DSOpcode.DS_MIN_F32, vaddr=VGPR(1), vdata=VGPR(2))
        flags = evaluate_status_flags(wave, inst)

        self.assertTrue(flags.invalid_op)
        self.assertFalse(flags.denorm)

    def test_subnormal_triggers_denorm_flag(self) -> None:
        wave = SIMDWaveState(wave_size=32, exec_mask=0x1)
        wave.set_vgpr(0, VGPR(1), 0)
        wave.set_vgpr(0, VGPR(2), SUBNORM_MIN)

        inst = DSMinMaxInstruction(DSOpcode.DS_MAX_F32, vaddr=VGPR(1), vdata=VGPR(2))
        flags = evaluate_status_flags(wave, inst)

        self.assertFalse(flags.invalid_op)
        self.assertTrue(flags.denorm)

    def test_address_carry_out_generation(self) -> None:
        wave = SIMDWaveState(wave_size=32, exec_mask=0x1)
        # Address close to 2^32 - 1, offset adds 16 causing carry
        wave.set_vgpr(0, VGPR(1), 0xFFFFFFFF)
        wave.set_vgpr(0, VGPR(2), ONE_F32)

        inst = DSMinMaxInstruction(DSOpcode.DS_MIN_F32, vaddr=VGPR(1), vdata=VGPR(2), offset=OffsetImmediate(16))
        flags = evaluate_status_flags(wave, inst)

        self.assertTrue(flags.carry_out)

    def test_unaligned_byte_address_detection(self) -> None:
        wave = SIMDWaveState(wave_size=32, exec_mask=0x1)
        wave.set_vgpr(0, VGPR(1), 7)  # byte address 7 is not 4-byte aligned
        wave.set_vgpr(0, VGPR(2), ONE_F32)

        inst = DSMinMaxInstruction(DSOpcode.DS_MIN_F32, vaddr=VGPR(1), vdata=VGPR(2))
        flags = evaluate_status_flags(wave, inst)

        self.assertTrue(flags.unaligned_address)

    def test_oob_address_wrapping_detection(self) -> None:
        wave = SIMDWaveState(wave_size=32, exec_mask=0x1)
        # 64 KiB LDS = 65536 bytes. Address 70000 exceeds LDS size
        wave.set_vgpr(0, VGPR(1), 70000)
        wave.set_vgpr(0, VGPR(2), ONE_F32)

        inst = DSMinMaxInstruction(DSOpcode.DS_MAX_F32, vaddr=VGPR(1), vdata=VGPR(2))
        flags = evaluate_status_flags(wave, inst)

        self.assertTrue(flags.oob_wrapped)

    def test_bank_conflicts_detection(self) -> None:
        # Lane 0 targets dword 0 (bank 0)
        # Lane 1 targets dword 32 (bank 0)
        # Lane 2 targets dword 64 (bank 0)
        wave = SIMDWaveState(wave_size=32, exec_mask=0x7)
        wave.set_vgpr(0, VGPR(1), 0 * 4)
        wave.set_vgpr(1, VGPR(1), 32 * 4)
        wave.set_vgpr(2, VGPR(1), 64 * 4)

        inst = DSMinMaxInstruction(DSOpcode.DS_MIN_F32, vaddr=VGPR(1), vdata=VGPR(2))
        flags = evaluate_status_flags(wave, inst)

        # 3 dwords targeting bank 0 -> 2 bank conflicts
        self.assertEqual(flags.bank_conflicts, 2)
        self.assertEqual(flags.address_collisions, 0)

    def test_address_collisions_detection(self) -> None:
        # Lanes 0, 1, 2, 3 all target dword 10 (address 40)
        wave = SIMDWaveState(wave_size=32, exec_mask=0xF)
        for l in range(4):
            wave.set_vgpr(l, VGPR(1), 40)

        inst = DSMinMaxInstruction(DSOpcode.DS_MAX_F32, vaddr=VGPR(1), vdata=VGPR(2))
        flags = evaluate_status_flags(wave, inst)

        # 4 lanes on the same dword -> 3 collisions, 0 bank conflict
        self.assertEqual(flags.address_collisions, 3)
        self.assertEqual(flags.bank_conflicts, 0)


class TestIEEE754BoundaryExtremes(unittest.TestCase):
    """Exhaustive semantic tests for IEEE 754 boundary extremes, subnormals, and NaN handling."""

    def setUp(self) -> None:
        self.lds = LDSMemory(65536)
        self.state = ScalarThreadState(lds=self.lds)
        self.interp = ScalarDSInterpreter()

    def test_signed_zeros_all_permutations(self) -> None:
        permutations = [
            (POS_ZERO, NEG_ZERO, NEG_ZERO, POS_ZERO),
            (NEG_ZERO, POS_ZERO, NEG_ZERO, POS_ZERO),
            (POS_ZERO, POS_ZERO, POS_ZERO, POS_ZERO),
            (NEG_ZERO, NEG_ZERO, NEG_ZERO, NEG_ZERO),
        ]
        for a, b, exp_min, exp_max in permutations:
            with self.subTest(a=hex(a), b=hex(b)):
                # Test via scalar interpreter
                # LDS dword 0 starts at 'a'
                self.lds.write_dword(0, a)
                self.state.set_vgpr(VGPR(1), 0)
                self.state.set_vgpr(VGPR(2), b)
                self.interp.execute(self.state, DSMinMaxInstruction(DSOpcode.DS_MIN_F32, VGPR(1), VGPR(2)))
                self.assertEqual(self.lds.read_dword(0), exp_min)

                self.lds.write_dword(0, a)
                self.interp.execute(self.state, DSMinMaxInstruction(DSOpcode.DS_MAX_F32, VGPR(1), VGPR(2)))
                self.assertEqual(self.lds.read_dword(0), exp_max)

    def test_infinities_against_extremes(self) -> None:
        # -inf < -FLT_MAX < ... < +FLT_MAX < +inf
        pairs = [
            (POS_INF, FLT_MAX, FLT_MAX, POS_INF),
            (NEG_INF, NEG_FLT_MAX, NEG_INF, NEG_FLT_MAX),
            (NEG_INF, POS_INF, NEG_INF, POS_INF),
            (POS_INF, POS_INF, POS_INF, POS_INF),
            (NEG_INF, NEG_INF, NEG_INF, NEG_INF),
        ]
        for a, b, exp_min, exp_max in pairs:
            with self.subTest(a=hex(a), b=hex(b)):
                self.assertEqual(oracle_min_f32(a, b), exp_min)
                self.assertEqual(oracle_max_f32(a, b), exp_max)

    def test_subnormal_precision_preservation(self) -> None:
        # Subnormals are strictly ordered and never flushed to zero
        subnormals = [
            (SUBNORM_MIN, POS_ZERO, POS_ZERO, SUBNORM_MIN),
            (NEG_SUBNORM_MIN, NEG_ZERO, NEG_SUBNORM_MIN, NEG_ZERO),
            (SUBNORM_MIN, SUBNORM_MAX, SUBNORM_MIN, SUBNORM_MAX),
            (NEG_SUBNORM_MAX, NEG_SUBNORM_MIN, NEG_SUBNORM_MAX, NEG_SUBNORM_MIN),
        ]
        for a, b, exp_min, exp_max in subnormals:
            with self.subTest(a=hex(a), b=hex(b)):
                self.assertEqual(oracle_min_f32(a, b), exp_min)
                self.assertEqual(oracle_max_f32(a, b), exp_max)

    def test_nan_suppression_exhaustive(self) -> None:
        nan_variants = [
            CANONICAL_QNAN,
            QNAN_PAYLOAD_A,
            QNAN_PAYLOAD_B,
            SNAN_PAYLOAD_A,
            SNAN_PAYLOAD_B,
        ]
        valid_floats = [
            POS_ZERO,
            NEG_ZERO,
            ONE_F32,
            NEG_ONE_F32,
            FLT_MIN,
            FLT_MAX,
            NEG_FLT_MAX,
            SUBNORM_MIN,
            SUBNORM_MAX,
            POS_INF,
            NEG_INF,
        ]
        for nan_val in nan_variants:
            for num in valid_floats:
                # min(nan, num) == num and min(num, nan) == num
                self.assertEqual(oracle_min_f32(nan_val, num), num)
                self.assertEqual(oracle_min_f32(num, nan_val), num)
                # max(nan, num) == num and max(num, nan) == num
                self.assertEqual(oracle_max_f32(nan_val, num), num)
                self.assertEqual(oracle_max_f32(num, nan_val), num)

    def test_dual_nans_quieting(self) -> None:
        # Two NaNs produce a quiet NaN
        nan1 = SNAN_PAYLOAD_A
        nan2 = SNAN_PAYLOAD_B
        res_min = oracle_min_f32(nan1, nan2)
        res_max = oracle_max_f32(nan1, nan2)

        self.assertTrue(is_nan(res_min))
        self.assertTrue(is_qnan(res_min))
        self.assertTrue(is_nan(res_max))
        self.assertTrue(is_qnan(res_max))


class TestNegativeControlsAndMutations(unittest.TestCase):
    """Unit tests for negative controls, syntax rejection, and mutation detection."""

    def test_syntax_rejection_invalid_opcodes(self) -> None:
        invalid_assemblies = [
            "",
            "   ",
            "v_min_f32 v0, v1",
            "ds_add_f32 v0, v1",
            "ds_min_f64 v0, v1",
            "ds_min_f32",
            "ds_min_rtn_f32 v0, v1",  # returning needs 3 registers
            "ds_min_f32 v0, v1, v2",  # non-returning takes 2 registers
            "ds_min_f32 r0, v1",      # invalid reg prefix
            "ds_min_f32 v0, s1",
            "ds_min_f32 v256, v1",    # out of bounds register index
            "ds_min_f32 v0, v1 offset:70000",  # out of bounds offset
            "ds_min_f32 v0, v1 offset:-1",
        ]
        for asm in invalid_assemblies:
            with self.subTest(asm=asm):
                with self.assertRaises((ValueError, IndexError)):
                    DSMinMaxInstruction.parse(asm)

    def test_architectural_model_negative_controls(self) -> None:
        # Non-power-of-two LDS allocation
        with self.assertRaises(ValueError):
            LDSMemory(1000)
        with self.assertRaises(ValueError):
            LDSMemory(-64)

        # Invalid wave size (not 32 or 64)
        with self.assertRaises(ValueError):
            SIMDWaveState(wave_size=16)
        with self.assertRaises(ValueError):
            SIMDWaveState(wave_size=128)

    def test_mutation_detection_inverted_min_max(self) -> None:
        # Mutant: swaps min and max
        def mutant_min(a: int, b: int) -> int:
            return oracle_max_f32(a, b)

        # Valid oracle returns ONE_F32; mutant returns TWO_F32
        self.assertNotEqual(mutant_min(ONE_F32, TWO_F32), oracle_min_f32(ONE_F32, TWO_F32))

    def test_mutation_detection_no_nan_suppression(self) -> None:
        # Mutant: fails to suppress NaN and returns NaN instead of valid float
        def mutant_min(a: int, b: int) -> int:
            if is_nan(a) or is_nan(b):
                return CANONICAL_QNAN
            return oracle_min_f32(a, b)

        self.assertNotEqual(mutant_min(CANONICAL_QNAN, ONE_F32), oracle_min_f32(CANONICAL_QNAN, ONE_F32))

    def test_mutation_detection_wrong_signed_zero(self) -> None:
        # Mutant: treats -0.0 and +0.0 as identical, returning +0.0
        def mutant_min(a: int, b: int) -> int:
            if is_zero(a) and is_zero(b):
                return POS_ZERO
            return oracle_min_f32(a, b)

        self.assertNotEqual(mutant_min(POS_ZERO, NEG_ZERO), oracle_min_f32(POS_ZERO, NEG_ZERO))

    def test_mutation_detection_destructive_aliasing(self) -> None:
        # Mutant interpreter that writes destination before reading source address
        class DestructiveMutantInterpreter(ScalarDSInterpreter):
            def execute(self, state: ScalarThreadState, inst: DSMinMaxInstruction) -> Optional[int]:
                if inst.opcode.is_return:
                    # Bug: prematurely clobbers vdst with garbage before reading vaddr
                    assert inst.vdst is not None
                    state.set_vgpr(inst.vdst, 0xBAD00000)
                return super().execute(state, inst)

        lds = LDSMemory(65536)
        lds.write_dword(2, TEN_F32)
        state = ScalarThreadState(lds=lds)
        state.set_vgpr(VGPR(1), 8)  # dword 2
        state.set_vgpr(VGPR(2), ONE_F32)

        inst = DSMinMaxInstruction(DSOpcode.DS_MIN_RTN_F32, vaddr=VGPR(1), vdata=VGPR(2), vdst=VGPR(1))
        mutant = DestructiveMutantInterpreter()
        mutant.execute(state, inst)

        # Destructive mutant clobbered address before reading, so dword 2 was never updated to 1.0!
        self.assertNotEqual(lds.read_dword(2), ONE_F32)

    def test_mutation_detection_ignore_exec_mask(self) -> None:
        # Mutant SIMD interpreter that ignores exec_mask and processes inactive lanes
        class MutantSIMDInterpreter(SIMDDSInterpreter):
            def execute(self, state: SIMDWaveState, inst: DSMinMaxInstruction) -> Dict[int, int]:
                # Bug: forces all lanes active
                saved_mask = state.exec_mask
                state.exec_mask = (1 << state.wave_size) - 1
                try:
                    return super().execute(state, inst)
                finally:
                    state.exec_mask = saved_mask

        lds = LDSMemory(65536)
        lds.write_dword(5, TEN_F32)
        wave = SIMDWaveState(wave_size=32, exec_mask=0x0, lds=lds)  # all lanes masked off
        for l in range(32):
            wave.set_vgpr(l, VGPR(1), 20)
            wave.set_vgpr(l, VGPR(2), ONE_F32)

        inst = DSMinMaxInstruction(DSOpcode.DS_MIN_F32, vaddr=VGPR(1), vdata=VGPR(2))
        mutant = MutantSIMDInterpreter()
        mutant.execute(wave, inst)

        # Mutant modified LDS despite exec_mask == 0!
        self.assertEqual(lds.read_dword(5), ONE_F32)
        # Normal interpreter leaves it at TEN_F32
        lds.write_dword(5, TEN_F32)
        SIMDDSInterpreter().execute(wave, inst)
        self.assertEqual(lds.read_dword(5), TEN_F32)

    def test_mutation_detection_dropped_offset(self) -> None:
        # Mutant interpreter that ignores offset
        class DroppedOffsetInterpreter(ScalarDSInterpreter):
            def execute(self, state: ScalarThreadState, inst: DSMinMaxInstruction) -> Optional[int]:
                # Bug: sets offset to 0
                no_off_inst = DSMinMaxInstruction(
                    opcode=inst.opcode,
                    vaddr=inst.vaddr,
                    vdata=inst.vdata,
                    vdst=inst.vdst,
                    offset=OffsetImmediate(0),
                )
                return super().execute(state, no_off_inst)

        lds = LDSMemory(65536)
        lds.write_dword(1, TEN_F32)
        lds.write_dword(2, TEN_F32)
        state = ScalarThreadState(lds=lds)
        state.set_vgpr(VGPR(1), 4)  # dword 1
        state.set_vgpr(VGPR(2), ONE_F32)

        # Offset 4 shifts target from dword 1 to dword 2
        inst = DSMinMaxInstruction(DSOpcode.DS_MIN_F32, vaddr=VGPR(1), vdata=VGPR(2), offset=OffsetImmediate(4))
        mutant = DroppedOffsetInterpreter()
        mutant.execute(state, inst)

        # Mutant targeted dword 1 instead of dword 2
        self.assertEqual(lds.read_dword(1), ONE_F32)
        self.assertEqual(lds.read_dword(2), TEN_F32)


if __name__ == "__main__":
    unittest.main()
