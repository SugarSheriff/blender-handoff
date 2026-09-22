# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal GLB (binary glTF 2.0) reader, writer, and inspector.

Used in three places: the add-on checks its own output right after export, the mock
ingest server validates uploads, and the tests build fixtures with write_glb().
Stdlib only.
"""

import json
import math
import struct

GLB_MAGIC = 0x46546C67  # b"glTF" read as little-endian uint32
CHUNK_JSON = 0x4E4F534A
CHUNK_BIN = 0x004E4942

MODE_TRIANGLES, MODE_TRIANGLE_STRIP, MODE_TRIANGLE_FAN = 4, 5, 6

# Extensions this receiver can decode. Anything listed in extensionsRequired but missing
# here means the file cannot be loaded at all, so it is a hard rejection rather than a
# warning. Draco is deliberately absent: the pipeline re-compresses on its own, and
# decoding someone else's Draco settings first only adds a lossy step.
SUPPORTED_EXTENSIONS = frozenset({
    "KHR_materials_clearcoat",
    "KHR_materials_emissive_strength",
    "KHR_materials_ior",
    "KHR_materials_sheen",
    "KHR_materials_specular",
    "KHR_materials_transmission",
    "KHR_materials_unlit",
    "KHR_materials_volume",
    "KHR_mesh_quantization",
    "KHR_texture_transform",
    "KHR_lights_punctual",
    "EXT_texture_webp",
})


class GlbError(ValueError):
    """The bytes are not a readable GLB container."""


def read_glb(data):
    """Split a GLB into its parsed JSON document and BIN chunk (or None)."""
    if len(data) < 12:
        raise GlbError("file is shorter than the 12-byte GLB header")
    magic, version, length = struct.unpack_from("<III", data, 0)
    if magic != GLB_MAGIC:
        raise GlbError("missing 'glTF' magic; not a binary glTF (a .gltf JSON file, or a renamed FBX?)")
    if version != 2:
        raise GlbError(f"GLB container version {version}; only version 2 is supported")
    if length != len(data):
        raise GlbError(f"header declares {length} bytes but the file is {len(data)} bytes (truncated transfer?)")

    doc, bin_chunk = None, None
    offset = 12
    while offset < length:
        if offset + 8 > length:
            raise GlbError("truncated chunk header")
        chunk_len, chunk_type = struct.unpack_from("<II", data, offset)
        start, end = offset + 8, offset + 8 + chunk_len
        if end > length:
            raise GlbError("a chunk runs past the end of the file")
        if chunk_type == CHUNK_JSON and doc is None:
            try:
                doc = json.loads(bytes(data[start:end]).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise GlbError(f"JSON chunk is not valid JSON: {exc}") from exc
        elif chunk_type == CHUNK_BIN and bin_chunk is None:
            bin_chunk = bytes(data[start:end])
        # The spec says unknown chunk types must be skipped, not rejected.
        offset = end

    if doc is None:
        raise GlbError("no JSON chunk")
    return doc, bin_chunk


def write_glb(doc, bin_chunk=b""):
    """Pack a glTF document and binary buffer into GLB bytes (both chunks 4-byte aligned)."""
    json_bytes = json.dumps(doc, separators=(",", ":")).encode("utf-8")
    json_bytes += b" " * (-len(json_bytes) % 4)  # JSON chunk pads with spaces
    bin_chunk = bytes(bin_chunk) + b"\x00" * (-len(bin_chunk) % 4)  # BIN pads with zeros

    total = 12 + 8 + len(json_bytes) + (8 + len(bin_chunk) if bin_chunk else 0)
    out = bytearray(struct.pack("<III", GLB_MAGIC, 2, total))
    out += struct.pack("<II", len(json_bytes), CHUNK_JSON) + json_bytes
    if bin_chunk:
        out += struct.pack("<II", len(bin_chunk), CHUNK_BIN) + bin_chunk
    return bytes(out)


def inspect(data, supported_extensions=SUPPORTED_EXTENSIONS):
    """Summarize a GLB and list the problems that would stop a receiver from using it.

    `issues` holds hard problems (spec violations, undecodable extensions). Everything
    else is descriptive: counts, rendered triangle count, and world-space bounds.
    """
    doc, bin_chunk = read_glb(data)
    issues = []

    asset = doc.get("asset", {})
    if asset.get("version") != "2.0":
        issues.append(f"asset.version is {asset.get('version')!r}, expected '2.0'")

    accessors = doc.get("accessors", [])
    bin_len = len(bin_chunk) if bin_chunk else 0
    first_buffer = (doc.get("buffers") or [{}])[0]
    for i, view in enumerate(doc.get("bufferViews", [])):
        # Buffer 0 without a uri is the GLB's own BIN chunk; others are external files.
        is_glb_buffer = view.get("buffer", 0) == 0 and "uri" not in first_buffer
        if is_glb_buffer and view.get("byteOffset", 0) + view.get("byteLength", 0) > bin_len:
            issues.append(f"bufferView {i} reads past the end of the BIN chunk")

    for mesh_index, mesh in enumerate(doc.get("meshes", [])):
        name = mesh.get("name", f"mesh {mesh_index}")
        for prim in mesh.get("primitives", []):
            position = prim.get("attributes", {}).get("POSITION")
            if position is None:
                issues.append(f"{name!r} has a primitive with no POSITION attribute")
            elif "min" not in accessors[position] or "max" not in accessors[position]:
                issues.append(f"{name!r} POSITION accessor is missing min/max (required by the spec)")

    required = doc.get("extensionsRequired", [])
    for ext in required:
        if ext not in supported_extensions:
            issues.append(f"requires extension {ext}, which this receiver cannot decode")

    triangles, primitives, bounds = _walk_scene(doc, accessors)

    return {
        "generator": asset.get("generator"),
        "version": asset.get("version"),
        "bytes": len(data),
        "counts": {
            "nodes": len(doc.get("nodes", [])),
            "meshes": len(doc.get("meshes", [])),
            "primitives": primitives,
            "materials": len(doc.get("materials", [])),
            "textures": len(doc.get("textures", [])),
            "images": len(doc.get("images", [])),
            "animations": len(doc.get("animations", [])),
        },
        "triangles": triangles,
        "bounds": bounds,
        "extensions_used": doc.get("extensionsUsed", []),
        "extensions_required": required,
        "images": [img.get("mimeType", "external uri") for img in doc.get("images", [])],
        "materials": [_material_summary(m, i) for i, m in enumerate(doc.get("materials", []))],
        "issues": issues,
    }


def _material_summary(mat, index):
    pbr = mat.get("pbrMetallicRoughness", {})
    return {
        "name": mat.get("name", f"material {index}"),
        "alpha_mode": mat.get("alphaMode", "OPAQUE"),
        "double_sided": mat.get("doubleSided", False),
        "maps": [slot for slot, present in (
            ("baseColor", "baseColorTexture" in pbr),
            ("metallicRoughness", "metallicRoughnessTexture" in pbr),
            ("normal", "normalTexture" in mat),
            ("occlusion", "occlusionTexture" in mat),
            ("emissive", "emissiveTexture" in mat),
        ) if present],
    }


def _walk_scene(doc, accessors):
    """Traverse the default scene, returning (rendered triangles, primitives, world bounds).

    Accessor min/max are in mesh-local space, so each primitive's box is pushed through
    its node's world matrix. Instanced meshes are counted once per instance, because
    that is what a renderer actually draws.
    """
    nodes = doc.get("nodes", [])
    meshes = doc.get("meshes", [])
    scenes = doc.get("scenes", [])
    if scenes:
        roots = scenes[doc.get("scene", 0)].get("nodes", [])
    else:
        children = {c for n in nodes for c in n.get("children", [])}
        roots = [i for i in range(len(nodes)) if i not in children]

    lo, hi = [math.inf] * 3, [-math.inf] * 3
    triangles = primitives = 0
    stack = [(i, _IDENTITY) for i in roots]
    seen = set()
    while stack:
        index, parent = stack.pop()
        if (index, id(parent)) in seen:  # guards against malformed cyclic hierarchies
            continue
        seen.add((index, id(parent)))
        node = nodes[index]
        world = _mul(parent, _local_matrix(node))
        if "mesh" in node:
            for prim in meshes[node["mesh"]].get("primitives", []):
                position = prim.get("attributes", {}).get("POSITION")
                if position is None:
                    continue
                primitives += 1
                acc = accessors[position]
                count = accessors[prim["indices"]]["count"] if "indices" in prim else acc["count"]
                mode = prim.get("mode", MODE_TRIANGLES)
                if mode == MODE_TRIANGLES:
                    triangles += count // 3
                elif mode in (MODE_TRIANGLE_STRIP, MODE_TRIANGLE_FAN):
                    triangles += max(count - 2, 0)
                if "min" in acc and "max" in acc:
                    for corner in _box_corners(acc["min"], acc["max"]):
                        p = _transform(world, corner)
                        for k in range(3):
                            lo[k], hi[k] = min(lo[k], p[k]), max(hi[k], p[k])
        stack.extend((c, world) for c in node.get("children", []))

    bounds = None
    if lo[0] != math.inf:
        bounds = {
            "min": [round(v, 6) for v in lo],
            "max": [round(v, 6) for v in hi],
            "size": [round(hi[k] - lo[k], 6) for k in range(3)],
        }
    return triangles, primitives, bounds


_IDENTITY = [[1.0 if r == c else 0.0 for c in range(4)] for r in range(4)]


def _local_matrix(node):
    if "matrix" in node:
        m = node["matrix"]  # glTF stores matrices column-major
        return [[m[c * 4 + r] for c in range(4)] for r in range(4)]
    tx, ty, tz = node.get("translation", (0.0, 0.0, 0.0))
    x, y, z, w = node.get("rotation", (0.0, 0.0, 0.0, 1.0))
    sx, sy, sz = node.get("scale", (1.0, 1.0, 1.0))
    # T * R * S, with R built from the unit quaternion and S folded into its columns.
    return [
        [(1 - 2 * (y * y + z * z)) * sx, 2 * (x * y - z * w) * sy, 2 * (x * z + y * w) * sz, tx],
        [2 * (x * y + z * w) * sx, (1 - 2 * (x * x + z * z)) * sy, 2 * (y * z - x * w) * sz, ty],
        [2 * (x * z - y * w) * sx, 2 * (y * z + x * w) * sy, (1 - 2 * (x * x + y * y)) * sz, tz],
        [0.0, 0.0, 0.0, 1.0],
    ]


def _mul(a, b):
    return [[sum(a[r][k] * b[k][c] for k in range(4)) for c in range(4)] for r in range(4)]


def _transform(m, p):
    return [m[r][0] * p[0] + m[r][1] * p[1] + m[r][2] * p[2] + m[r][3] for r in range(3)]


def _box_corners(lo, hi):
    return [(x, y, z) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]
