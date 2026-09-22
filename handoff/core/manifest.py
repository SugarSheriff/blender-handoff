# SPDX-License-Identifier: GPL-3.0-or-later
"""The sidecar manifest sent alongside every GLB.

The bytes say what the asset is; the manifest says what the sender *meant*: units, axes,
color conventions, where it came from, and what preflight already knew. When an asset
looks wrong on the receiving end, this is the first thing support should read.
"""

import hashlib

SCHEMA = "handoff.manifest/1"

# glTF fixes these by spec. Stating them explicitly means a receiver never has to guess,
# and a mismatch report can point at a concrete field instead of a hunch.
CONVENTIONS = {
    "units": "meters",
    "up_axis": "+Y",
    "forward_axis": "+Z",
    "handedness": "right",
    "base_color_encoding": "sRGB",
    "data_texture_encoding": "linear (normal, metallic/roughness, occlusion)",
    "normal_map_convention": "tangent space, OpenGL (+Y up)",
}


def build(*, asset_name, glb_bytes, glb_report, findings, source):
    return {
        "schema": SCHEMA,
        "asset": {
            "name": asset_name,
            "format": "model/gltf-binary",
            "bytes": len(glb_bytes),
            "sha256": hashlib.sha256(glb_bytes).hexdigest(),
        },
        "conventions": CONVENTIONS,
        "source": source,
        "stats": {
            "triangles": glb_report["triangles"],
            "counts": glb_report["counts"],
            "bounds": glb_report["bounds"],
            "extensions_used": glb_report["extensions_used"],
        },
        "preflight": [f.to_dict() for f in findings],
    }
