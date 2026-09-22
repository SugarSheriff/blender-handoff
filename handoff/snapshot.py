# SPDX-License-Identifier: GPL-3.0-or-later
"""Turn the live Blender scene into the plain-dict snapshot core.preflight expects.

This is the only module that reads scene data for preflight, which keeps the rules
themselves testable without Blender.
"""

import os

import addon_utils
import bpy

# Nodes the glTF exporter cannot translate. They render in Blender and silently vanish
# on export, which is exactly the kind of thing an artist should hear about beforehand.
PROCEDURAL_NODES = {
    "TEX_NOISE": "Noise Texture",
    "TEX_VORONOI": "Voronoi Texture",
    "TEX_WAVE": "Wave Texture",
    "TEX_MUSGRAVE": "Musgrave Texture",
    "TEX_GRADIENT": "Gradient Texture",
    "TEX_MAGIC": "Magic Texture",
    "TEX_BRICK": "Brick Texture",
    "TEX_CHECKER": "Checker Texture",
    "TEX_WHITE_NOISE": "White Noise Texture",
    "TEX_GABOR": "Gabor Texture",
    "BUMP": "Bump (only Normal Map nodes export)",
}

# Which Principled BSDF input an image feeds decides the color space it should use.
# "Emission" is the pre-4.0 socket name for "Emission Color".
PRINCIPLED_ROLES = {
    "Base Color": "base_color",
    "Metallic": "metallic",
    "Roughness": "roughness",
    "Normal": "normal",
    "Emission Color": "emission",
    "Emission": "emission",
    "Alpha": "alpha",
}
GLTF_OUTPUT_GROUPS = {"glTF Material Output", "glTF Settings"}


def capture(context, scope):
    scene = context.scene
    if scope == "SELECTED":
        objects = list(context.selected_objects)
    else:
        objects = [o for o in scene.objects if o.visible_get()]

    depsgraph = context.evaluated_depsgraph_get()
    entries = []
    material_users = {}
    for obj in objects:
        entry = {
            "name": obj.name,
            "type": obj.type,
            "scale": list(obj.scale),
            "has_children": bool(obj.children),
            # Raw Blender units: that is what the exporter writes, whatever the unit scale says.
            "dimensions": list(obj.dimensions),
            "triangles": 0,
            "uv_layers": 0,
            "materials": [],
        }
        if obj.type == "MESH":
            # Evaluate with modifiers, since the export applies them.
            evaluated = obj.evaluated_get(depsgraph)
            mesh = evaluated.to_mesh()
            try:
                mesh.calc_loop_triangles()
                entry["triangles"] = len(mesh.loop_triangles)
                entry["uv_layers"] = len(mesh.uv_layers)
            finally:
                evaluated.to_mesh_clear()
            for slot in obj.material_slots:
                if slot.material:
                    entry["materials"].append(slot.material.name)
                    material_users.setdefault(slot.material, []).append(obj.name)
        entries.append(entry)

    return {
        "unit_scale": scene.unit_settings.scale_length,
        "objects": entries,
        "materials": [_material(mat, users) for mat, users in material_users.items()],
    }


def source_info(context):
    return {
        "application": "Blender",
        "blender_version": bpy.app.version_string,
        "gltf_exporter_version": _exporter_version(),
        "file": os.path.basename(bpy.data.filepath) or "(unsaved)",
        "scene": context.scene.name,
        "unit_scale": context.scene.unit_settings.scale_length,
    }


def _material(mat, users):
    info = {
        "name": mat.name,
        "users": users,
        "uses_principled": False,
        "unsupported_nodes": [],
        "textures": [],
    }
    # Blender 5.0 made every material node-based and deprecated use_nodes; only
    # consult it on versions where a material can genuinely have nodes switched off.
    uses_nodes = mat.use_nodes if bpy.app.version < (5, 0, 0) else True
    if not uses_nodes or not mat.node_tree:
        return info
    for node in _reachable_nodes(mat.node_tree):
        if node.type == "BSDF_PRINCIPLED":
            info["uses_principled"] = True
        elif node.type in PROCEDURAL_NODES:
            info["unsupported_nodes"].append(PROCEDURAL_NODES[node.type])
        elif node.type == "TEX_IMAGE" and node.image:
            info["textures"].append(_texture(node))
    return info


def _reachable_nodes(tree):
    """Nodes that actually contribute to the active Material Output.

    Walking upstream from the output (instead of scanning tree.nodes) keeps stray,
    disconnected nodes from producing false alarms. Node groups are not entered;
    that is a known limitation, noted in the README.
    """
    outputs = [n for n in tree.nodes if n.type == "OUTPUT_MATERIAL"]
    output = next((n for n in outputs if n.is_active_output), outputs[0] if outputs else None)
    if output is None:
        return []
    seen, stack = {}, [output]
    while stack:
        node = stack.pop()
        if node.name in seen:
            continue
        seen[node.name] = node
        for socket in node.inputs:
            stack.extend(link.from_node for link in socket.links if not link.is_muted)
    return list(seen.values())


def _texture(node):
    image = node.image
    missing = False
    if image.source in {"FILE", "SEQUENCE"} and not image.packed_file:
        missing = not os.path.exists(bpy.path.abspath(image.filepath, library=image.library))
    return {
        "image": image.name,
        "role": _texture_role(node) or "other",
        "colorspace": image.colorspace_settings.name,
        "size": list(image.size),
        "missing": missing,
        "tiled": image.source == "TILED",
    }


def _texture_role(node, depth=0):
    """Follow an image's outputs downstream until they land somewhere meaningful."""
    if depth > 8:
        return None
    for output in node.outputs:
        for link in output.links:
            if link.is_muted:
                continue
            target, socket = link.to_node, link.to_socket.name
            if target.type == "BSDF_PRINCIPLED":
                if socket in PRINCIPLED_ROLES:
                    return PRINCIPLED_ROLES[socket]
            elif target.type == "NORMAL_MAP":
                return "normal"
            elif target.type in {"SEPARATE_COLOR", "SEPRGB"}:
                return "orm"  # channel-packed occlusion/roughness/metallic: all data
            elif target.type == "GROUP" and target.node_tree and target.node_tree.name in GLTF_OUTPUT_GROUPS:
                if socket == "Occlusion":
                    return "occlusion"
            else:
                role = _texture_role(target, depth + 1)  # Mix, Math, Reroute, and friends
                if role:
                    return role
    return None


def _exporter_version():
    for module in addon_utils.modules():
        if module.__name__ == "io_scene_gltf2":
            version = getattr(module, "bl_info", {}).get("version", ())
            return ".".join(str(v) for v in version) or "unknown"
    return "unknown"
