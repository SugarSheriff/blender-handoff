import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "handoff"))

from core import preflight  # noqa: E402


def mesh(name="Chair", **overrides):
    obj = {"name": name, "type": "MESH", "scale": [1, 1, 1], "has_children": False,
           "dimensions": [0.5, 0.5, 0.9], "triangles": 1200, "uv_layers": 1, "materials": ["Wood"]}
    obj.update(overrides)
    return obj


def material(name="Wood", textures=(), **overrides):
    mat = {"name": name, "users": ["Chair"], "uses_principled": True,
           "unsupported_nodes": [], "textures": list(textures)}
    mat.update(overrides)
    return mat


def texture(role, colorspace, **overrides):
    tex = {"image": f"{role}.png", "role": role, "colorspace": colorspace,
           "size": [2048, 2048], "missing": False, "tiled": False}
    tex.update(overrides)
    return tex


def codes(snapshot, **kwargs):
    return [f.code for f in preflight.run(snapshot, **kwargs)]


class Preflight(unittest.TestCase):
    def test_clean_scene(self):
        snap = {"unit_scale": 1.0, "objects": [mesh()],
                "materials": [material(textures=[texture("base_color", "sRGB"), texture("normal", "Non-Color")])]}
        self.assertEqual(preflight.run(snap), [])

    def test_color_space_by_role(self):
        snap = {"objects": [mesh()], "materials": [material(textures=[
            texture("base_color", "Non-Color"),   # wrong: color data read as linear
            texture("normal", "sRGB"),            # wrong: data run through a color curve
            texture("orm", "Non-Color"),          # right
            texture("emission", "sRGB"),          # right
        ])]}
        findings = [f for f in preflight.run(snap) if f.code == "texture.colorspace"]
        self.assertEqual(sorted(f.target for f in findings), ["Wood / base_color.png", "Wood / normal.png"])

    def test_centimeter_import(self):
        snap = {"objects": [mesh(dimensions=[50.0, 50.0, 90.0])], "materials": []}
        self.assertNotIn("units.suspicious_size", codes(snap))
        snap["objects"][0]["dimensions"] = [5000.0, 5000.0, 9000.0]
        self.assertIn("units.suspicious_size", codes(snap))

    def test_unit_scale(self):
        self.assertIn("units.scale_length", codes({"unit_scale": 0.01, "objects": [], "materials": []}))

    def test_missing_uvs_only_matter_when_textured(self):
        untextured = {"objects": [mesh(uv_layers=0)], "materials": [material()]}
        self.assertNotIn("mesh.missing_uvs", codes(untextured))
        textured = {"objects": [mesh(uv_layers=0)],
                    "materials": [material(textures=[texture("base_color", "sRGB")])]}
        self.assertIn("mesh.missing_uvs", codes(textured))
        self.assertTrue(preflight.has_errors(preflight.run(textured)))

    def test_missing_texture_is_an_error_and_short_circuits(self):
        snap = {"objects": [mesh()], "materials": [material(textures=[
            texture("normal", "sRGB", missing=True, size=[0, 0])])]}
        self.assertEqual(codes(snap), ["texture.missing"])

    def test_transforms(self):
        snap = {"objects": [mesh(scale=[-1, 1, 1]), mesh("Rig", scale=[2, 1, 1], has_children=True)],
                "materials": []}
        self.assertEqual(sorted(codes(snap)), ["transform.negative_scale", "transform.shear_risk"])

    def test_material_problems(self):
        snap = {"objects": [mesh()], "materials": [material(
            uses_principled=False, unsupported_nodes=["Noise Texture", "Noise Texture"])]}
        self.assertEqual(sorted(codes(snap)), ["material.not_principled", "material.procedural"])

    def test_texture_size_rules(self):
        snap = {"objects": [mesh()], "materials": [material(textures=[
            texture("base_color", "sRGB", size=[8192, 8192]), texture("roughness", "Non-Color", size=[1000, 1000])])]}
        self.assertEqual(sorted(codes(snap)), ["texture.npot", "texture.oversize"])

    def test_triangle_budget(self):
        snap = {"objects": [mesh(triangles=400_000), mesh("Table", triangles=200_000)], "materials": []}
        self.assertIn("budget.triangles", codes(snap, triangle_budget=500_000))
        self.assertNotIn("budget.triangles", codes(snap, triangle_budget=1_000_000))

    def test_errors_sort_first(self):
        snap = {"unit_scale": 0.01, "objects": [mesh(uv_layers=0)],
                "materials": [material(textures=[texture("base_color", "sRGB")])]}
        self.assertEqual(preflight.run(snap)[0].severity, preflight.ERROR)

    def test_every_code_is_documented(self):
        docs = (Path(__file__).resolve().parents[1] / "TROUBLESHOOTING.md").read_text(encoding="utf-8")
        source = (Path(__file__).resolve().parents[1] / "handoff" / "core" / "preflight.py").read_text(encoding="utf-8")
        import re
        for code in set(re.findall(r'"([a-z]+\.[a-z_]+)", (?:ERROR|WARN|INFO)', source)):
            self.assertIn(f"### {code}", docs, f"{code} has no TROUBLESHOOTING.md entry")


if __name__ == "__main__":
    unittest.main()
