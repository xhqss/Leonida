"""tools/sandbox_scene.py: entity world rows, nearest-first selection and the packed vertex layout."""
import os
import sys
import struct
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tools'))
import sandbox_scene

class TestSandboxScene(unittest.TestCase):
    def test_draw_rows(self):
        result = sandbox_scene.draw_rows((0, 0, 0, 1), 1.0, 1.0, (10, 20, 30))
        self.assertAlmostEqual(result[0][0], 1.0, places=6)
        self.assertAlmostEqual(result[0][1], 0.0, places=6)
        self.assertAlmostEqual(result[0][2], 0.0, places=6)
        self.assertAlmostEqual(result[1][0], 0.0, places=6)
        self.assertAlmostEqual(result[1][1], 1.0, places=6)
        self.assertAlmostEqual(result[1][2], 0.0, places=6)
        self.assertAlmostEqual(result[2][0], 0.0, places=6)
        self.assertAlmostEqual(result[2][1], 0.0, places=6)
        self.assertAlmostEqual(result[2][2], 1.0, places=6)
        self.assertAlmostEqual(result[3][0], 10.0, places=6)
        self.assertAlmostEqual(result[3][1], 20.0, places=6)
        self.assertAlmostEqual(result[3][2], 30.0, places=6)

    def test_draw_rows_rotation(self):
        result = sandbox_scene.draw_rows((0, 0, -0.70710678, 0.70710678), 1, 1, (0, 0, 0))
        self.assertAlmostEqual(result[0][0], 0.0, places=6)
        self.assertAlmostEqual(result[0][1], 1.0, places=6)
        self.assertAlmostEqual(result[0][2], 0.0, places=6)
        self.assertAlmostEqual(result[1][0], -1.0, places=6)
        self.assertAlmostEqual(result[1][1], 0.0, places=6)
        self.assertAlmostEqual(result[1][2], 0.0, places=6)
        self.assertAlmostEqual(result[2][0], 0.0, places=6)
        self.assertAlmostEqual(result[2][1], 0.0, places=6)
        self.assertAlmostEqual(result[2][2], 1.0, places=6)

    def test_draw_rows_scale(self):
        result = sandbox_scene.draw_rows((0, 0, 0, 1), 2.0, 3.0, (0, 0, 0))
        self.assertAlmostEqual(result[0][0], 2.0, places=6)
        self.assertAlmostEqual(result[0][1], 0.0, places=6)
        self.assertAlmostEqual(result[0][2], 0.0, places=6)
        self.assertAlmostEqual(result[1][0], 0.0, places=6)
        self.assertAlmostEqual(result[1][1], 2.0, places=6)
        self.assertAlmostEqual(result[1][2], 0.0, places=6)
        self.assertAlmostEqual(result[2][0], 0.0, places=6)
        self.assertAlmostEqual(result[2][1], 0.0, places=6)
        self.assertAlmostEqual(result[2][2], 3.0, places=6)

    def test_select_rows(self):
        rows = [
            {"_pos": [1, 1, 0]},
            {"_pos": [5, 5, 0]},
            {"_pos": [20, 20, 0]},
            {"_pos": [3, 3, 0]}
        ]
        result = sandbox_scene.select_rows(rows, (0, 0), 10, 10)
        self.assertEqual(len(result), 3)
        self.assertAlmostEqual(result[0]["_pos"][0], 1.0, places=6)
        self.assertAlmostEqual(result[0]["_pos"][1], 1.0, places=6)
        self.assertAlmostEqual(result[1]["_pos"][0], 3.0, places=6)
        self.assertAlmostEqual(result[1]["_pos"][1], 3.0, places=6)
        self.assertAlmostEqual(result[2]["_pos"][0], 5.0, places=6)
        self.assertAlmostEqual(result[2]["_pos"][1], 5.0, places=6)

        result = sandbox_scene.select_rows(rows, (0, 0), 10, 2)
        self.assertEqual(len(result), 2)
        self.assertAlmostEqual(result[0]["_pos"][0], 1.0, places=6)
        self.assertAlmostEqual(result[0]["_pos"][1], 1.0, places=6)
        self.assertAlmostEqual(result[1]["_pos"][0], 3.0, places=6)
        self.assertAlmostEqual(result[1]["_pos"][1], 3.0, places=6)

    def test_mesh_bytes(self):
        prim = {
            "positions": [(0, 0, 0), (1, 0, 0), (0, 1, 0)],
            "normals": [(0, 0, 1)] * 3,
            "uvs": [(0, 0), (1, 0), (0, 1)],
            "indices": [0, 1, 2]
        }
        b = sandbox_scene.mesh_bytes(prim)
        self.assertEqual(len(b), 116)
        self.assertEqual(tuple(struct.unpack_from('<II', b, 0)), (3, 3))
        self.assertEqual(tuple(struct.unpack_from('<8f', b, 40)), (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0))
        self.assertEqual(tuple(struct.unpack_from('<3I', b, 104)), (0, 1, 2))

    def test_mesh_bytes_no_normals(self):
        prim = {
            "positions": [(0, 0, 0), (1, 0, 0), (0, 1, 0)],
            "normals": [],
            "indices": [0, 1, 2]
        }
        b = sandbox_scene.mesh_bytes(prim)
        self.assertEqual(tuple(struct.unpack_from('<8f', b, 8)), (0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0))
        self.assertEqual(tuple(struct.unpack_from('<8f', b, 40)), (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0))

if __name__ == "__main__":
    unittest.main()
