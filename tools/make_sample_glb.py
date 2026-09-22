"""Generate sample.glb (a brass orb on a clay plinth) from scratch, with no 3D libraries.

Used as the demo page's default model and as a known-good fixture in the tests.
Run:  python tools/make_sample_glb.py
"""

import math
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "handoff"))
from core.glb import write_glb  # noqa: E402

FLOAT, USHORT = 5126, 5123
ARRAY_BUFFER, ELEMENT_ARRAY_BUFFER = 34962, 34963


def srgb_to_linear(c):
    # glTF factors are linear. Pasting an sRGB hex value straight in is a classic
    # "why is it so washed out" bug, so convert on the way in.
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def linear_rgba(hex_color):
    rgb = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    return [round(srgb_to_linear(c), 5) for c in rgb] + [1.0]


def box(sx, sy, sz):
    """Axis-aligned box with flat per-face normals (24 verts, 12 triangles)."""
    half = (sx / 2, sy / 2, sz / 2)
    axes = [(1, 0, 0), (0, 1, 0), (0, 0, 1)]
    positions, normals, indices = [], [], []
    for i in range(3):
        for sign in (1, -1):
            n = [sign * a for a in axes[i]]
            # Pick in-plane axes so u x v == n, which makes the quad counter-clockwise.
            u, v = (axes[(i + 1) % 3], axes[(i + 2) % 3]) if sign > 0 else (axes[(i + 2) % 3], axes[(i + 1) % 3])
            base = len(positions)
            for su, sv in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                positions.append([(n[k] + su * u[k] + sv * v[k]) * half[k] for k in range(3)])
                normals.append(n)
            indices += [base, base + 1, base + 2, base, base + 2, base + 3]
    return positions, normals, indices


def uv_sphere(radius, segments=48, rings=24):
    positions, normals = [], []
    for r in range(rings + 1):
        theta = math.pi * r / rings
        for s in range(segments + 1):
            phi = 2 * math.pi * s / segments
            n = [math.sin(theta) * math.cos(phi), math.cos(theta), math.sin(theta) * math.sin(phi)]
            normals.append(n)
            positions.append([c * radius for c in n])
    indices = []
    for r in range(rings):
        for s in range(segments):
            a = r * (segments + 1) + s
            b = a + segments + 1
            if r != 0:  # the top row's first triangle collapses into the pole
                indices += [a, a + 1, b]
            if r != rings - 1:  # likewise for the bottom row's second triangle
                indices += [a + 1, b + 1, b]
    return positions, normals, indices


def build_sample():
    blob = bytearray()
    views, accessors = [], []

    def add_view(data, target):
        blob.extend(b"\x00" * (-len(blob) % 4))  # accessor data must be 4-byte aligned
        views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": len(data), "target": target})
        blob.extend(data)
        return len(views) - 1

    def add_mesh(positions, normals, indices):
        flat = [c for p in positions for c in p]
        pos_view = add_view(struct.pack(f"<{len(flat)}f", *flat), ARRAY_BUFFER)
        accessors.append({
            "bufferView": pos_view, "componentType": FLOAT, "count": len(positions), "type": "VEC3",
            "min": [min(p[k] for p in positions) for k in range(3)],
            "max": [max(p[k] for p in positions) for k in range(3)],
        })
        flat = [c for n in normals for c in n]
        accessors.append({"bufferView": add_view(struct.pack(f"<{len(flat)}f", *flat), ARRAY_BUFFER),
                          "componentType": FLOAT, "count": len(normals), "type": "VEC3"})
        accessors.append({"bufferView": add_view(struct.pack(f"<{len(indices)}H", *indices), ELEMENT_ARRAY_BUFFER),
                          "componentType": USHORT, "count": len(indices), "type": "SCALAR"})
        n = len(accessors)
        return {"attributes": {"POSITION": n - 3, "NORMAL": n - 2}, "indices": n - 1}

    orb = add_mesh(*uv_sphere(0.45))
    plinth = add_mesh(*box(1.4, 0.6, 1.4))
    orb["material"], plinth["material"] = 0, 1

    doc = {
        "asset": {"version": "2.0", "generator": "handoff sample generator"},
        "scene": 0,
        "scenes": [{"name": "Sample", "nodes": [0, 1]}],
        "nodes": [
            {"name": "Orb", "mesh": 0, "translation": [0, 1.05, 0]},
            {"name": "Plinth", "mesh": 1, "translation": [0, 0.3, 0]},
        ],
        "meshes": [{"name": "Orb", "primitives": [orb]}, {"name": "Plinth", "primitives": [plinth]}],
        "materials": [
            {"name": "Brass", "pbrMetallicRoughness": {
                "baseColorFactor": linear_rgba("#f4e04d"), "metallicFactor": 1.0, "roughnessFactor": 0.28}},
            {"name": "Coral Clay", "pbrMetallicRoughness": {
                "baseColorFactor": linear_rgba("#ff6b5e"), "metallicFactor": 0.0, "roughnessFactor": 0.7}},
        ],
        "accessors": accessors,
        "bufferViews": views,
        "buffers": [{"byteLength": len(blob) + (-len(blob) % 4)}],
    }
    return write_glb(doc, bytes(blob))


if __name__ == "__main__":
    out = ROOT / "sample.glb"
    out.write_bytes(build_sample())
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")
