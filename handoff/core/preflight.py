# SPDX-License-Identifier: GPL-3.0-or-later
"""Preflight rules: catch the scene problems that break assets after they leave Blender.

Rules run against a plain-dict snapshot (built by ../snapshot.py inside Blender, or by
hand in the tests), never against bpy directly. Snapshot shape:

    {
      "unit_scale": 1.0,
      "objects": [{"name", "type", "scale": [x, y, z], "has_children", "dimensions": [x, y, z],
                   "triangles", "uv_layers", "materials": [names]}],
      "materials": [{"name", "users": [object names], "uses_principled",
                     "unsupported_nodes": [labels],
                     "textures": [{"image", "role", "colorspace", "size": [w, h], "missing", "tiled"}]}],
    }

Every finding code has a matching heading in TROUBLESHOOTING.md.
"""

from dataclasses import asdict, dataclass

ERROR, WARN, INFO = "error", "warn", "info"

# Texture roles, from the Principled BSDF socket (or node) an image ends up feeding.
COLOR_ROLES = {"base_color", "emission"}
DATA_ROLES = {"normal", "roughness", "metallic", "orm", "occlusion", "alpha"}
# The same idea goes by different names across Blender versions and OCIO configs.
DATA_COLORSPACES = {"Non-Color", "Raw", "Generic Data"}

# Anything this large or small is more likely a unit mix-up than a real asset.
# The classic case: an FBX authored in centimeters imported 100x too big.
MAX_SANE_SIZE_M = 500.0
MIN_SANE_SIZE_M = 0.005
MAX_WEB_TEXTURE = 4096


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    target: str
    message: str
    fix: str
    object: str = ""  # an object the panel can select to show the artist where the problem is

    def to_dict(self):
        return asdict(self)


def run(snapshot, triangle_budget=500_000):
    findings = []
    findings += _units(snapshot)
    findings += _transforms(snapshot)
    findings += _meshes(snapshot, triangle_budget)
    findings += _materials(snapshot)
    order = {ERROR: 0, WARN: 1, INFO: 2}
    return sorted(findings, key=lambda f: (order[f.severity], f.code, f.target))


def has_errors(findings):
    return any(f.severity == ERROR for f in findings)


def summarize(findings):
    counts = {s: sum(1 for f in findings if f.severity == s) for s in (ERROR, WARN, INFO)}
    if not findings:
        return "Preflight clean"
    return f"Preflight: {counts[ERROR]} error(s), {counts[WARN]} warning(s), {counts[INFO]} note(s)"


def _units(snap):
    out = []
    scale = snap.get("unit_scale", 1.0)
    if abs(scale - 1.0) > 1e-6:
        out.append(Finding(
            "units.scale_length", WARN, "Scene",
            f"Scene unit scale is {scale:g}. The viewport shows scaled units, but the glTF "
            "exporter writes raw Blender units, and glTF defines 1 unit as 1 meter.",
            "Set Scene Properties > Units > Unit Scale to 1.0 and model at real-world size.",
        ))
    for obj in snap.get("objects", []):
        if obj.get("type") != "MESH":
            continue
        largest = max(obj.get("dimensions", [0, 0, 0]))
        if largest > MAX_SANE_SIZE_M:
            out.append(Finding(
                "units.suspicious_size", WARN, obj["name"],
                f"{obj['name']} is {largest:,.1f} m across. That is usually a centimeter asset "
                "imported as meters (100x too big).",
                "Check the source units; if it came from FBX, re-import with the right scale and apply it.",
                obj["name"],
            ))
        elif 0 < largest < MIN_SANE_SIZE_M:
            out.append(Finding(
                "units.suspicious_size", WARN, obj["name"],
                f"{obj['name']} is only {largest * 1000:.2f} mm across. Likely a meters/centimeters mix-up.",
                "Check the source units and apply the corrected scale.",
                obj["name"],
            ))
    return out


def _transforms(snap):
    out = []
    for obj in snap.get("objects", []):
        sx, sy, sz = obj.get("scale", (1, 1, 1))
        if sx * sy * sz < 0:
            out.append(Finding(
                "transform.negative_scale", WARN, obj["name"],
                f"{obj['name']} has negative scale, which mirrors the mesh and flips its triangle "
                "winding. Runtimes that cull back faces can render it inside-out.",
                "Apply scale (Ctrl+A > Scale) and recalculate normals outside (Shift+N).",
                obj["name"],
            ))
        uniform = max(abs(sx), abs(sy), abs(sz)) - min(abs(sx), abs(sy), abs(sz)) < 1e-4
        if not uniform and obj.get("has_children"):
            out.append(Finding(
                "transform.shear_risk", WARN, obj["name"],
                f"{obj['name']} has non-uniform scale and child objects. Rotated children inherit "
                "shear, which glTF's translation/rotation/scale nodes cannot represent.",
                "Apply scale on the parent before export.",
                obj["name"],
            ))
    return out


def _meshes(snap, triangle_budget):
    out = []
    textured = {m["name"] for m in snap.get("materials", []) if m.get("textures")}
    total = 0
    for obj in snap.get("objects", []):
        if obj.get("type") != "MESH":
            continue
        total += obj.get("triangles", 0)
        if obj.get("uv_layers", 0) == 0 and textured.intersection(obj.get("materials", [])):
            out.append(Finding(
                "mesh.missing_uvs", ERROR, obj["name"],
                f"{obj['name']} uses image textures but has no UV map, so every texture "
                "lookup samples a single texel.",
                "Unwrap the mesh (U > Smart UV Project is a fast start) or add a UV map.",
                obj["name"],
            ))
    if total > triangle_budget:
        out.append(Finding(
            "budget.triangles", WARN, "Scene",
            f"{total:,} triangles after modifiers, over the {triangle_budget:,} budget set in preferences.",
            "Decimate or remove hidden geometry, or raise the budget if the target can take it.",
        ))
    return out


def _materials(snap):
    out = []
    for mat in snap.get("materials", []):
        name = mat["name"]
        owner = (mat.get("users") or [""])[0]
        if not mat.get("uses_principled"):
            out.append(Finding(
                "material.not_principled", WARN, name,
                f"{name} is not built on a Principled BSDF. The glTF exporter maps only Principled "
                "inputs to PBR, so this material ships as a flat default.",
                "Rebuild it on a Principled BSDF, or bake its look to textures.",
                owner,
            ))
        for label in sorted(set(mat.get("unsupported_nodes", []))):
            out.append(Finding(
                "material.procedural", WARN, name,
                f"{name} uses a {label} node. glTF has no procedural textures, so that part of "
                "the look is dropped on export.",
                "Bake the procedural result to an image texture and plug that in instead.",
                owner,
            ))
        for tex in mat.get("textures", []):
            out += _texture(tex, name, owner)
    return out


def _texture(tex, mat_name, owner):
    out = []
    image, role, space = tex["image"], tex.get("role", "other"), tex.get("colorspace", "")
    target = f"{mat_name} / {image}"

    if tex.get("missing"):
        return [Finding(
            "texture.missing", ERROR, target,
            f"{image} points at a file that does not exist, so the exporter has nothing to embed.",
            "Relink it (File > External Data > Find Missing Files) or pack it into the .blend.",
            owner,
        )]
    if tex.get("tiled"):
        out.append(Finding(
            "texture.udim", WARN, target,
            f"{image} is a UDIM tiled image. glTF has no UDIM support, so only one tile survives.",
            "Bake the tiles down to a single texture per material before export.",
            owner,
        ))

    if role in COLOR_ROLES and (space in DATA_COLORSPACES or space.startswith("Linear")):
        out.append(Finding(
            "texture.colorspace", WARN, target,
            f"{image} feeds {role.replace('_', ' ')} but is tagged {space}. Blender shows it lighter "
            "than it is; glTF viewers always decode color textures as sRGB, so the delivered "
            "asset will look darker than what was approved in Blender.",
            "Set the Image Texture node's Color Space to sRGB.",
            owner,
        ))
    elif role in DATA_ROLES and space not in DATA_COLORSPACES:
        out.append(Finding(
            "texture.colorspace", WARN, target,
            f"{image} feeds {role} data but is tagged {space}. Blender applies a color curve to "
            "values that are not colors, so the viewport lies; runtimes read the file as raw data, "
            "so shading downstream will not match what was approved in Blender.",
            "Set the Image Texture node's Color Space to Non-Color.",
            owner,
        ))

    w, h = (tex.get("size") or (0, 0))[:2]
    if w and h:
        if max(w, h) > MAX_WEB_TEXTURE:
            out.append(Finding(
                "texture.oversize", WARN, target,
                f"{image} is {w}x{h}. Above {MAX_WEB_TEXTURE}px costs a lot of GPU memory on phones "
                "and headsets for detail nobody can see.",
                f"Downscale to {MAX_WEB_TEXTURE}px or less.",
                owner,
            ))
        if w & (w - 1) or h & (h - 1):
            out.append(Finding(
                "texture.npot", INFO, target,
                f"{image} is {w}x{h}, not a power of two. Most modern runtimes cope, but GPU "
                "compression (KTX2/Basis) and mipmapping behave best at power-of-two sizes.",
                "Resize to the nearest power of two (e.g. 1024 or 2048).",
                owner,
            ))
    return out
