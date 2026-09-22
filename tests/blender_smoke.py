"""Headless end-to-end test inside a real Blender. Not part of `unittest discover`.

Installs dist/handoff-extension.zip the way a user would, builds a scene with one known
problem per preflight category, checks the findings, then publishes to the mock ingest
server with faults injected. Run through tests/run_blender_smoke.ps1, which isolates
Blender's user folders so nothing touches your real config.
"""

import os
import sys
import time
import traceback

import bpy

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from server.mock_ingest import IngestState, serve_in_thread  # noqa: E402

failures = []


def check(condition, message):
    print(("  ok    " if condition else "  FAIL  ") + message)
    if not condition:
        failures.append(message)


def install():
    zip_path = os.path.join(REPO, "dist", "handoff-extension.zip")
    bpy.ops.extensions.package_install_files(filepath=zip_path, repo="user_default", enable_on_install=True)
    module = next(name for name in bpy.context.preferences.addons.keys() if name.endswith(".handoff"))
    print(f"installed as {module}")
    return module


def image_texture(tree, name, size, colorspace, filepath=None):
    node = tree.nodes.new("ShaderNodeTexImage")
    if filepath:
        image = bpy.data.images.new(name, 4, 4)
        image.source = "FILE"
        image.filepath = filepath
    else:
        image = bpy.data.images.new(name, size, size)
    image.colorspace_settings.name = colorspace
    node.image = image
    return node


def new_material(name):
    mat = bpy.data.materials.new(name)
    if bpy.app.version < (5, 0, 0):  # 5.0+ materials always have nodes
        mat.use_nodes = True
    return mat


def principled_material(name):
    mat = new_material(name)
    tree = mat.node_tree
    bsdf = next(n for n in tree.nodes if n.type == "BSDF_PRINCIPLED")
    return mat, tree, bsdf


def cube(name, location=(0, 0, 0), scale=(1, 1, 1)):
    # Set object scale explicitly; the operator's own scale argument does not reliably
    # end up on obj.scale, and object-level scale is what the transform rules inspect.
    bpy.ops.mesh.primitive_cube_add(location=location)
    obj = bpy.context.active_object
    obj.name = name
    obj.scale = scale
    return obj


def build_scene():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj)

    # Chair: wrong color spaces both ways, a procedural node, NPOT texture, Subdivision.
    chair = cube("Chair")
    chair.modifiers.new("Subsurf", "SUBSURF").levels = 3
    chair.modifiers["Subsurf"].render_levels = 3
    mat, tree, bsdf = principled_material("Wood")
    base = image_texture(tree, "wood_base", 2048, "Non-Color")
    tree.links.new(base.outputs["Color"], bsdf.inputs["Base Color"])
    normal = image_texture(tree, "wood_normal", 1000, "sRGB")
    normal_map = tree.nodes.new("ShaderNodeNormalMap")
    tree.links.new(normal.outputs["Color"], normal_map.inputs["Color"])
    tree.links.new(normal_map.outputs["Normal"], bsdf.inputs["Normal"])
    noise = tree.nodes.new("ShaderNodeTexNoise")
    tree.links.new(noise.outputs["Fac"], bsdf.inputs["Roughness"])
    chair.data.materials.append(mat)

    cube("Giant", location=(10, 0, 0), scale=(400, 400, 400))
    cube("Mirror", location=(-4, 0, 0), scale=(-1, 1, 1))

    # NoUV: textured material on a mesh without UVs, and the texture file is missing.
    no_uv = cube("NoUV", location=(0, 5, 0))
    while no_uv.data.uv_layers:
        no_uv.data.uv_layers.remove(no_uv.data.uv_layers[0])
    mat, tree, bsdf = principled_material("Painted")
    missing = image_texture(tree, "paint_base", 0, "sRGB", filepath="//does_not_exist.png")
    tree.links.new(missing.outputs["Color"], bsdf.inputs["Base Color"])
    no_uv.data.materials.append(mat)

    # Stray: not Principled, plus a disconnected Noise node that must NOT be reported.
    stray = cube("Stray", location=(0, -5, 0))
    mat = new_material("Toon")
    tree = mat.node_tree
    for node in list(tree.nodes):
        if node.type != "OUTPUT_MATERIAL":
            tree.nodes.remove(node)
    diffuse = tree.nodes.new("ShaderNodeBsdfDiffuse")
    tree.links.new(diffuse.outputs["BSDF"], tree.nodes["Material Output"].inputs["Surface"])
    tree.nodes.new("ShaderNodeTexNoise")
    stray.data.materials.append(mat)

    # Rig: non-uniform scale on a parent with a child.
    rig = cube("Rig", location=(0, 10, 0), scale=(2, 1, 1))
    child = cube("RigChild", location=(0, 12, 0))
    child.parent = rig

    for obj in bpy.data.objects:
        obj.select_set(True)
    bpy.context.view_layer.update()


def main():
    print(f"Blender {bpy.app.version_string}, online_access={getattr(bpy.app, 'online_access', 'n/a')}")
    module = install()
    state = sys.modules[f"{module}.state"]
    prefs = bpy.context.preferences.addons[module].preferences

    print("\n[preflight]")
    build_scene()
    bpy.ops.handoff.preflight()
    got = {f.code for f in state.findings}
    expected = {
        "texture.colorspace", "texture.npot", "material.procedural", "units.suspicious_size",
        "transform.negative_scale", "mesh.missing_uvs", "texture.missing",
        "material.not_principled", "transform.shear_risk",
    }
    for f in state.findings:
        print(f"        {f.severity:5} {f.code:26} {f.target}")
    check(got == expected, f"finding codes match (missing {expected - got or '{}'}, extra {got - expected or '{}'})")
    colorspace_targets = sorted(f.target for f in state.findings if f.code == "texture.colorspace")
    check(colorspace_targets == ["Wood / wood_base", "Wood / wood_normal"],
          f"both color-space mistakes found by role: {colorspace_targets}")
    procedural = [f.target for f in state.findings if f.code == "material.procedural"]
    check(procedural == ["Wood"], f"disconnected Noise node in Toon ignored: {procedural}")
    check(any(f.object == "Chair" for f in state.findings if f.code == "material.procedural"),
          "finding points at an object the panel can select")

    snapshot = sys.modules[f"{module}.snapshot"]
    snap = snapshot.capture(bpy.context, "SELECTED")
    chair = next(o for o in snap["objects"] if o["name"] == "Chair")
    check(chair["triangles"] == 6 * 4 ** 3 * 2, f"triangles counted after Subdivision: {chair['triangles']}")

    print("\n[publish is blocked by errors]")
    prefs.endpoint = "http://127.0.0.1:9"  # never reached
    try:
        bpy.ops.handoff.publish()
        check(False, "publish refused while preflight has errors")
    except RuntimeError as exc:
        check("Fix errors before publishing" in str(exc), f"publish refused: {str(exc).strip()}")

    print("\n[publish through injected faults]")
    st = IngestState(token="smoke", fail_first=1, lose_ack=True, processing_delay=0.3, log=lambda m: None)
    server, url = serve_in_thread(st)
    prefs.endpoint, prefs.token = url, "smoke"
    for obj in bpy.data.objects:
        obj.select_set(obj.name in {"Chair", "Giant"})
    bpy.context.scene.handoff.asset_name = "smoke chair"
    check(bpy.ops.handoff.publish() == {"FINISHED"}, "publish started with warnings only")

    deadline = time.monotonic() + 60
    while state.publish.running and time.monotonic() < deadline:
        time.sleep(0.1)
    for line in state.publish.lines():
        print("        " + line)
    result = state.publish.result or {}
    check(state.publish.status == "done", f"publish finished: {state.publish.status}")
    check(result.get("status") == "ready", f"asset ready: {result.get('asset_id')}")
    check(len(st.uploads) == 1, f"lost ack did not duplicate the upload ({len(st.uploads)} upload)")
    report = result.get("report") or {}
    check(report.get("triangles") == 6 * 4 ** 3 * 2 + 12, f"server counted Chair after modifiers + Giant: {report.get('triangles')}")
    size = (report.get("bounds") or {}).get("size", [0, 0, 0])
    check(abs(size[1] - 800) < 1e-3, f"Y-up in the GLB: Giant's height lands on Y ({size[1]:.1f} m)")
    check(result.get("manifest_schema") == "handoff.manifest/1", "manifest arrived with the upload")
    server.shutdown()

    print("\n[cancel]")
    st = IngestState(token="smoke", fail_first=50, log=lambda m: None)
    server, url = serve_in_thread(st)
    prefs.endpoint = url
    bpy.ops.handoff.publish()
    time.sleep(0.5)
    bpy.ops.handoff.cancel()
    deadline = time.monotonic() + 10
    while state.publish.running and time.monotonic() < deadline:
        time.sleep(0.05)
    check(state.publish.status == "cancelled", f"cancel stops a publish stuck in backoff: {state.publish.status}")
    server.shutdown()

    print("\n[unregister]")
    bpy.ops.preferences.addon_disable(module=module)
    check(not hasattr(bpy.types.Scene, "handoff"), "disabling the add-on cleans up Scene.handoff")


try:
    main()
except Exception:
    traceback.print_exc()
    failures.append("unhandled exception")

print(f"\n{'PASSED' if not failures else 'FAILED'}: {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
