from __future__ import annotations
import unittest
import struct
import math

class IntToFloatOracle:
    @staticmethod
    def i32_to_f32_bits(val: int) -> int:
        f = float(val)
        return struct.unpack('<I', struct.pack('<f', f))[0]

    @staticmethod
    def u32_to_f32_bits(val: int) -> int:
        if val < 0:
            val += 0x100000000
        f = float(val)
        return struct.unpack('<I', struct.pack('<f', f))[0]

    @staticmethod
    def i32_to_f64_bits(val: int) -> int:
        f = float(val)
        return struct.unpack('<Q', struct.pack('<d', f))[0]

class TestIntToFloatReferenceOracle(unittest.TestCase):
    def test_oracle_basic(self):
        self.assertEqual(IntToFloatOracle.i32_to_f32_bits(0), 0x00000000)
        self.assertEqual(IntToFloatOracle.u32_to_f32_bits(0), 0x00000000)
        self.assertEqual(IntToFloatOracle.i32_to_f32_bits(1), 0x3f800000)
        self.assertEqual(IntToFloatOracle.u32_to_f32_bits(1), 0x3f800000)
        self.assertEqual(IntToFloatOracle.i32_to_f32_bits(-1), 0xbf800000)

class TestSignedVsUnsigned32Bit(unittest.TestCase):
    def test_signed_vs_unsigned_boundaries(self):
        max_i32 = 0x7fffffff
        min_i32 = -0x80000000
        u32_2_31 = 0x80000000
        max_u32 = 0xffffffff

        f_max_i32 = IntToFloatOracle.i32_to_f32_bits(max_i32)
        f_u32_2_31 = IntToFloatOracle.u32_to_f32_bits(u32_2_31)
        f_min_i32 = IntToFloatOracle.i32_to_f32_bits(min_i32)
        f_max_u32 = IntToFloatOracle.u32_to_f32_bits(max_u32)

        self.assertNotEqual(f_min_i32, f_u32_2_31)
        self.assertTrue((f_min_i32 & 0x80000000) != 0)
        self.assertTrue((f_u32_2_31 & 0x80000000) == 0)

class TestMantissaRoundingAndControls(unittest.TestCase):
    def test_24bit_mantissa_rounding_and_tie_breaking(self):
        # IEEE 754 single precision has 24 bits of mantissa (23 explicit + 1 hidden bit).
        # Integers larger than 2^24 (16777216) cannot be represented exactly in float32
        # and require rounding.
        
        # Exact power of 2
        exact_val = 16777216 # 2^24
        self.assertEqual(IntToFloatOracle.i32_to_f32_bits(exact_val), 0x4b800000)

        # Values around 2^24 testing rounding / tie-breaking
        # 16777217 (2^24 + 1)
        # Let's check Python's float rounding behavior vs IEEE 754 round-to-nearest-even.
        bits_plus_1 = IntToFloatOracle.i32_to_f32_bits(16777217)
        bits_exact = IntToFloatOracle.i32_to_f32_bits(16777216)
        # 16777217 rounds down to 16777216 in float32 because mantissa only has 24 bits.
        self.assertEqual(bits_plus_1, bits_exact)

    def test_negative_controls(self):
        # Verify invalid input types or out-of-range bounds handling / negative controls
        with self.assertRaises((TypeError, ValueError, struct.error)):
            struct.pack('<i', "not an int")

if __name__ == '__main__':
    unittest.main()
