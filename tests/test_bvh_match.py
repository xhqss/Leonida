"""Synthetic checks for tools/bvh_match.py: the manifest entry the BLAS was built from is found and verified, a
wrong geometry, a wrong shader rule and a missing transform are not, and geometries are matched inside one model.
No game data is read.

    python -I tests/test_bvh_match.py
"""
import contextlib
import io
import json
import os
import struct
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

import bvh_match as M  # noqa: E402
import bvh_rehydrate as R  # noqa: E402
from test_bvh_rehydrate import build  # noqa: E402

# Two triangles over x,y 0..1 sharing the edge (1, 2); the leaf holds both primitives.
QUAD = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (1.0, 1.0, 0.0)]
QUAD_FACES = [(0, 1, 2), (2, 1, 3)]
LEAVES = [(0, 0x81, 0, 1, 0x09)]
BOUNDS = (0.0, 0.0, 0.0, 1.0, 1.0, 0.0)
SHIFTED = ((1.0, 0.0, 0.0, 0.5), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0))


def group(triangles):
    """A geometry of `triangles` triangles over the quad's four vertices."""
    return QUAD, [QUAD_FACES[i % 2] for i in range(triangles)]


def write_obj(path, groups):
    """groups: [(positions, faces)] -> one `o` group each, in the shape CreateObj writes."""
    lines, first = [], 1
    for index, (positions, faces) in enumerate(groups):
        lines.append("o model_0_geometry_%d" % index)
        lines += ["v %.9g %.9g %.9g" % p for p in positions]
        lines += ["f %d//%d %d//%d %d//%d" % (first + a, first + a, first + b, first + b, first + c, first + c)
                  for a, b, c in faces]
        first += len(positions)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def item(offset, file, triangles, hashes=None):
    """One manifest row, as tools/rpf9dump writes it: per geometry triangles and shader index."""
    materials = [{"ShaderIndex": i, "Shader": "%08X" % h} for i, h in enumerate(hashes or [])]
    return {"Offset": offset, "File": file, "Name": "drawable_at_%X" % offset, "Meshes": len(triangles),
            "Verts": 0, "Tris": sum(triangles), "MeshTris": list(triangles),
            "MeshModels": [0] * len(triangles), "MeshGeoms": list(range(len(triangles))),
            "MeshShaders": list(range(len(triangles))) if hashes else [None] * len(triangles),
            "Materials": materials}


class TestMatching(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = directory.name

    def guide(self, blas, items, obj_groups, argv=()):
        """Write the export this run reads, call the matcher, return (status, stdout, stderr)."""
        manifest = os.path.join(self.dir, "manifest.json")
        with open(manifest, "w", encoding="utf-8") as f:
            json.dump({"Count": len(items), "Items": items}, f)
        for name, groups in obj_groups.items():
            write_obj(os.path.join(self.dir, name), groups)
        path = os.path.join(self.dir, "blas.bin")
        with open(path, "wb") as f:
            f.write(blas)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = M.main([path, manifest, *argv])
        return status, out.getvalue(), err.getvalue()

    def test_a_manifest_that_is_not_an_object_is_one_error_line(self):
        path = os.path.join(self.dir, "blas.bin")
        with open(path, "wb") as f:
            f.write(build(LEAVES, bounds=BOUNDS)[0])
        for doc in ([1], [], "x", 3, None):
            manifest = os.path.join(self.dir, "manifest.json")
            with open(manifest, "w", encoding="utf-8") as f:
                json.dump(doc, f)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                status = M.main([path, manifest])
            self.assertEqual(status, 2, doc)
            lines = err.getvalue().splitlines()
            self.assertEqual(len(lines), 1, err.getvalue())
            self.assertTrue(lines[0].startswith("error: "), lines[0])
            self.assertNotIn("Traceback", err.getvalue())

    def test_the_right_geometry_is_found_and_consistent(self):
        src, _, _ = build(LEAVES, bounds=BOUNDS)
        status, out, _ = self.guide(src, [item(0x1000, "a.obj", [5, 2, 3])],
                                    {"a.obj": [group(5), group(2), group(3)]})
        self.assertEqual(status, 0, out)
        self.assertIn("triangles per geometry index: [2]", out)
        self.assertIn("1 of 1 scored candidates are consistent", out)
        self.assertIn("model 0 geometry [1] | outside 0 of 1", out)

    def test_a_geometry_with_other_triangle_counts_is_not_a_candidate(self):
        src, _, _ = build(LEAVES, bounds=BOUNDS)
        status, out, _ = self.guide(src, [item(0x1000, "a.obj", [5, 3])], {"a.obj": [group(5), group(3)]})
        self.assertEqual(status, 1, out)
        self.assertIn("0 candidates", out)
        self.assertIn("0 of 0 scored candidates are consistent", out)
        # An empty run says why: the drawable is outside the exported window, or the rule does not fit.
        self.assertIn("outside the exported window", out)

    def test_a_wrong_geometry_is_rejected(self):
        src, _, _ = build(LEAVES, bounds=BOUNDS)
        # The same two triangles, half a unit away from what the BLAS stores: the leaves fall outside sceneBounds.
        moved = [(x + 0.5, y, z) for x, y, z in QUAD]
        status, out, _ = self.guide(src, [item(0x1000, "a.obj", [2])], {"a.obj": [(moved, QUAD_FACES)]})
        self.assertEqual(status, 1, out)
        self.assertIn("rejected", out)
        self.assertIn("outside 1 of 1", out)

    def test_the_shader_rule_leaves_a_geometry_out(self):
        src, _, _ = build(LEAVES, bounds=BOUNDS)
        hashes = [0xAAAA1111, 0xBBBB2222]
        files = {"a.obj": [group(4), group(2)]}
        # Geometry 0 has the excluded shader, so the two triangles belong to geometry 1.
        status, out, _ = self.guide(src, [item(0x1000, "a.obj", [4, 2], hashes)], files,
                                    ["--skip-shaders", "%08X" % hashes[0]])
        self.assertEqual(status, 0, out)
        self.assertIn("model 0 geometry [1]", out)
        # Excluding the other shader instead leaves a four-triangle geometry, which the signature does not fit.
        status, out, _ = self.guide(src, [item(0x1000, "a.obj", [4, 2], hashes)], files,
                                    ["--skip-shaders", "%08X" % hashes[1]])
        self.assertEqual(status, 1, out)

    def test_the_instance_transform_decides(self):
        src, _, _ = build(LEAVES, bounds=(0.5, 0.0, 0.0, 1.5, 1.0, 0.0))
        descriptor = os.path.join(self.dir, "instance208.bin")
        with open(descriptor, "wb") as f:
            f.write(bytes(0x50) + struct.pack("<12f", *[v for row in SHIFTED for v in row]) + bytes(60))
        status, out, _ = self.guide(src, [item(0x1000, "a.obj", [2])], {"a.obj": [group(2)]},
                                    ["--descriptor", descriptor])
        self.assertEqual(status, 0, out)
        status, out, err = self.guide(src, [item(0x1000, "a.obj", [2])], {"a.obj": [group(2)]})
        self.assertEqual(status, 1, out)
        self.assertIn("rejected", out)
        self.assertIn("without the instance transform", err)

    def test_geometries_are_matched_inside_one_model(self):
        src, _, _ = build(LEAVES, bounds=BOUNDS)
        row = item(0x1000, "a.obj", [5, 2])
        row["MeshModels"] = [0, 1]               # two models in the primary LOD, one geometry each
        row["MeshGeoms"] = [0, 0]
        status, out, _ = self.guide(src, [row], {"a.obj": [group(5), group(2)]})
        self.assertEqual(status, 0, out)
        self.assertIn("model 1 geometry [0]", out)

    def test_a_manifest_without_per_geometry_counts_is_an_input_error(self):
        src, _, _ = build(LEAVES, bounds=BOUNDS)
        row = item(0x1000, "a.obj", [2])
        del row["MeshTris"]
        status, _, err = self.guide(src, [row], {"a.obj": [group(2)]})
        self.assertEqual(status, 2)
        self.assertIn("MeshTris", err)

    def test_a_manifest_that_lists_more_geometries_than_its_obj_holds_is_an_error_not_a_traceback(self):
        src, _, _ = build(LEAVES, bounds=BOUNDS)
        row = item(0x1000, "a.obj", [5, 2])          # the signature fits the second geometry only
        row["MeshTris"] = [5, 2]
        status, out, _ = self.guide(src, [row], {"a.obj": [group(5)]})   # ... but the OBJ has one group
        self.assertEqual(status, 1, out)
        self.assertIn("the manifest lists", out)
        self.assertIn("scored candidates are consistent", out)

    def test_a_manifest_with_no_models_says_what_is_wrong(self):
        src, _, _ = build(LEAVES, bounds=BOUNDS)
        status, _, err = self.guide(src, [], {"a.obj": [group(2)]})
        self.assertEqual(status, 2)
        self.assertIn("found no model in its window", err)

    def test_write_produces_the_buffers_bvh_rehydrate_reads(self):
        src, _, _ = build(LEAVES, bounds=BOUNDS)
        out = os.path.join(self.dir, "written")
        status, text, _ = self.guide(src, [item(0x1000, "a.obj", [5, 2, 3])],
                                    {"a.obj": [group(5), group(2), group(3)]}, ["--write", out])
        self.assertEqual(status, 0, text)
        with open(os.path.join(out, "export.json"), encoding="utf-8") as f:
            rows = json.load(f)
        self.assertEqual(len(rows), 1)                        # only the geometry the BLAS uses is written
        row = rows[0]
        self.assertEqual((row["drawable_geometry"], row["vertices"], row["triangles"]), (1, 4, 2))
        self.assertIsNone(row["transform"])
        with open(os.path.join(out, row["vb"]), "rb") as f:
            vb = f.read()
        with open(os.path.join(out, row["ib"]), "rb") as f:
            ib = f.read()
        self.assertEqual((len(vb), len(ib)), (4 * 12, 2 * 12))
        geometry = R.Geometry(vb, row["stride"], ib, False)
        self.assertEqual([geometry.triangle(0), geometry.triangle(1)], list(QUAD_FACES))

    def test_write_stores_nothing_when_no_candidate_is_consistent(self):
        src, _, _ = build(LEAVES, bounds=BOUNDS)
        moved = [(x + 0.5, y, z) for x, y, z in QUAD]
        out = os.path.join(self.dir, "nothing")
        status, text, _ = self.guide(src, [item(0x1000, "a.obj", [2])], {"a.obj": [(moved, QUAD_FACES)]},
                                     ["--write", out])
        self.assertEqual(status, 1, text)
        self.assertIn("nothing written", text)
        self.assertFalse(os.path.exists(out))

    def test_the_shader_rule_is_not_applied_without_every_hash(self):
        src, _, _ = build(LEAVES, bounds=BOUNDS)
        status, out, _ = self.guide(src, [item(0x1000, "a.obj", [2, 7])], {"a.obj": [group(2), group(7)]},
                                    ["--skip-shaders", "12345678"])
        self.assertEqual(status, 0, out)
        self.assertIn("shader rule not applied", out)


class TestPieces(unittest.TestCase):
    def test_leaf_signature_counts_a_paired_leaf_twice(self):
        self.assertEqual(M.leaf_signature(build([(0, 0x81, 0, 1, 0x09)], bounds=BOUNDS)[0]), [2])
        self.assertEqual(M.leaf_signature(build([(0, 0x01, 0, None, 0x09)], bounds=BOUNDS)[0]), [1])
        self.assertEqual(M.leaf_signature(build([(0, 0x01, 0, None, 0x09), (1, 0x01, 0, None, 0x09)], bounds=BOUNDS)[0]),
                         [1, 1])

    def test_leaf_signature_applies_the_header_offset(self):
        src, _, _ = build(LEAVES, bounds=BOUNDS)
        self.assertEqual(M.leaf_signature(bytes(0x100) + src, 0x100), [2])

    def test_embeddings_are_ordered_by_default(self):
        self.assertEqual(M.embeddings([2, 3], [2, 3, 3], 8), [[0, 1], [0, 2]])
        self.assertEqual(M.embeddings([3, 2], [2, 3, 3], 8), [])
        self.assertEqual(M.embeddings([3, 2], [2, 3, 3], 8, ordered=False), [[1, 0], [2, 0]])
        self.assertEqual(len(M.embeddings([1], [1] * 20, 5)), 5)          # the cap holds

    def test_read_groups_rebases_faces_per_group(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "a.obj")
            write_obj(path, [(QUAD, QUAD_FACES), (QUAD, [(0, 1, 2)])])
            groups = M.read_groups(path)
            self.assertEqual([len(positions) for positions, _ in groups], [4, 4])
            self.assertEqual(groups[0][1], QUAD_FACES)
            self.assertEqual(groups[1][1], [(0, 1, 2)])      # second group's faces start at its own first vertex
            self.assertEqual(groups[1][0], QUAD)

    def test_read_transform_reads_the_matrix_at_0x50(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "instance208.bin")
            with open(path, "wb") as f:
                f.write(bytes(0x50) + struct.pack("<12f", *[v for row in SHIFTED for v in row]) + bytes(60))
            self.assertEqual(M.read_transform(path), SHIFTED)

    def test_shader_hashes_parse_from_a_list(self):
        self.assertEqual(M.parse_shader_hashes("0xAA, bb"), frozenset({0xAA, 0xBB}))
        self.assertEqual(M.parse_shader_hashes(""), frozenset())
        self.assertEqual(M.parse_shader_hashes(None), frozenset())


if __name__ == "__main__":
    unittest.main()
