import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "handoff"), str(ROOT)]

from core import glb  # noqa: E402
from tools.make_sample_glb import build_sample  # noqa: E402


class InspectSample(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = glb.inspect(build_sample())

    def test_counts(self):
        counts = self.report["counts"]
        self.assertEqual(counts["meshes"], 2)
        self.assertEqual(counts["primitives"], 2)
        self.assertEqual(counts["materials"], 2)

    def test_triangles_skip_degenerate_poles(self):
        # 48 segments x 24 rings x 2, minus one collapsed triangle per segment at each pole, plus the box.
        self.assertEqual(self.report["triangles"], 48 * 24 * 2 - 2 * 48 + 12)

    def test_bounds_are_world_space(self):
        # Plinth spans y 0..0.6 after its translation; the orb sits on top up to 1.5.
        bounds = self.report["bounds"]
        for got, want in zip(bounds["min"], [-0.7, 0.0, -0.7]):
            self.assertAlmostEqual(got, want, places=4)
        for got, want in zip(bounds["max"], [0.7, 1.5, 0.7]):
            self.assertAlmostEqual(got, want, places=4)

    def test_clean_file_has_no_issues(self):
        self.assertEqual(self.report["issues"], [])


class InspectTransforms(unittest.TestCase):
    def test_rotation_and_scale_move_bounds(self):
        # A unit-cube primitive under a node rotated 90 degrees about Z and scaled 2x in X.
        doc = {
            "asset": {"version": "2.0"},
            "scenes": [{"nodes": [0]}],
            "nodes": [{"mesh": 0, "rotation": [0, 0, 0.7071068, 0.7071068], "scale": [2, 1, 1]}],
            "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
            "accessors": [{"count": 3, "type": "VEC3", "componentType": 5126,
                           "min": [0, 0, 0], "max": [1, 1, 1]}],
        }
        bounds = glb.inspect(glb.write_glb(doc))["bounds"]
        # X extent (2 after scaling) now lies along Y; Y extent (1) lies along -X.
        for got, want in zip(bounds["size"], [1, 2, 1]):
            self.assertAlmostEqual(got, want, places=4)

    def test_instances_count_per_draw(self):
        doc = {
            "asset": {"version": "2.0"},
            "scenes": [{"nodes": [0, 1]}],
            "nodes": [{"mesh": 0}, {"mesh": 0, "translation": [5, 0, 0]}],
            "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
            "accessors": [{"count": 6, "type": "VEC3", "componentType": 5126, "min": [0, 0, 0], "max": [1, 1, 1]}],
        }
        self.assertEqual(glb.inspect(glb.write_glb(doc))["triangles"], 4)


class InspectRejects(unittest.TestCase):
    def test_wrong_magic(self):
        data = bytearray(build_sample())
        data[0:4] = b"FBX "
        with self.assertRaisesRegex(glb.GlbError, "magic"):
            glb.inspect(bytes(data))

    def test_truncated(self):
        with self.assertRaisesRegex(glb.GlbError, "truncated"):
            glb.inspect(build_sample()[:-100])

    def test_bad_json(self):
        body = b"{not json}"
        data = struct.pack("<III", glb.GLB_MAGIC, 2, 12 + 8 + len(body) + 2) + \
            struct.pack("<II", len(body) + 2, glb.CHUNK_JSON) + body + b"  "
        with self.assertRaisesRegex(glb.GlbError, "not valid JSON"):
            glb.inspect(data)

    def test_undecodable_required_extension(self):
        doc = {"asset": {"version": "2.0"}, "extensionsUsed": ["KHR_draco_mesh_compression"],
               "extensionsRequired": ["KHR_draco_mesh_compression"]}
        issues = glb.inspect(glb.write_glb(doc))["issues"]
        self.assertTrue(any("KHR_draco_mesh_compression" in i for i in issues))

    def test_missing_position_bounds(self):
        doc = {
            "asset": {"version": "2.0"},
            "meshes": [{"name": "Bad", "primitives": [{"attributes": {"POSITION": 0}}]}],
            "accessors": [{"count": 3, "type": "VEC3", "componentType": 5126}],
        }
        issues = glb.inspect(glb.write_glb(doc))["issues"]
        self.assertTrue(any("min/max" in i for i in issues))


if __name__ == "__main__":
    unittest.main()
