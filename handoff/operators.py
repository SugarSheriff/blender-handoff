# SPDX-License-Identifier: GPL-3.0-or-later
import os
import re
import shutil
import tempfile
import threading

import bpy
from bpy.props import StringProperty

from . import snapshot, state
from .core import glb, manifest, preflight
from .core.client import Cancelled, IngestClient, IngestError

DOCS_URL = "https://github.com/SugarSheriff/blender-handoff/blob/main/TROUBLESHOOTING.md"


def _prefs(context):
    return context.preferences.addons[__package__].preferences


def _run_preflight(context):
    snap = snapshot.capture(context, context.scene.handoff.scope)
    findings = preflight.run(snap, triangle_budget=_prefs(context).triangle_budget)
    state.findings[:] = findings
    state.preflight_ran = True
    return snap, findings


def _asset_name(context):
    name = context.scene.handoff.asset_name.strip()
    if not name:
        name = bpy.path.display_name_from_filepath(bpy.data.filepath) or context.scene.name
    return name


class HANDOFF_OT_preflight(bpy.types.Operator):
    bl_idname = "handoff.preflight"
    bl_label = "Run Preflight"
    bl_description = "Check the export for problems that would break the asset downstream"

    def execute(self, context):
        _, findings = _run_preflight(context)
        self.report({"WARNING"} if findings else {"INFO"}, preflight.summarize(findings))
        return {"FINISHED"}


class HANDOFF_OT_publish(bpy.types.Operator):
    bl_idname = "handoff.publish"
    bl_label = "Publish"
    bl_description = "Preflight, export GLB, and upload it to the ingest endpoint"

    @classmethod
    def poll(cls, context):
        return not state.publish.running

    def execute(self, context):
        prefs = _prefs(context)
        # Blender 4.2+ lets users disable network access globally; extensions must respect it.
        if not getattr(bpy.app, "online_access", True):
            self.report({"ERROR"}, "Online access is off (Preferences > System > Network)")
            return {"CANCELLED"}
        if not prefs.endpoint:
            self.report({"ERROR"}, "Set an ingest endpoint in the add-on preferences")
            return {"CANCELLED"}
        if context.mode != "OBJECT":
            # Edit-mode changes are not flushed to mesh data until you leave edit mode.
            self.report({"ERROR"}, "Switch to Object Mode before publishing")
            return {"CANCELLED"}

        snap, findings = _run_preflight(context)
        if not any(o["type"] == "MESH" for o in snap["objects"]):
            self.report({"ERROR"}, "Nothing to export: no mesh objects in scope")
            return {"CANCELLED"}
        if preflight.has_errors(findings):
            self.report({"ERROR"}, f"{preflight.summarize(findings)}. Fix errors before publishing")
            return {"CANCELLED"}

        name = _asset_name(context)
        filename = re.sub(r"[^\w.-]+", "_", name) + ".glb"
        workdir = tempfile.mkdtemp(prefix="handoff_")
        path = os.path.join(workdir, filename)
        scope = context.scene.handoff.scope
        result = bpy.ops.export_scene.gltf(
            filepath=path,
            export_format="GLB",
            use_selection=scope == "SELECTED",
            use_visible=scope == "VISIBLE",
            export_apply=True,  # apply modifiers, matching what preflight counted
            export_yup=True,  # glTF is +Y up; Blender is +Z up
            export_extras=True,  # custom properties ride along as glTF extras
        )
        if "FINISHED" not in result or not os.path.exists(path):
            self.report({"ERROR"}, "glTF export failed; see the system console for the exporter's error")
            return {"CANCELLED"}

        with open(path, "rb") as f:
            data = f.read()
        # Verify the output, not just the input: catches exporter surprises before the network does.
        try:
            report = glb.inspect(data)
        except glb.GlbError as exc:
            self.report({"ERROR"}, f"Exported file is not a valid GLB: {exc}")
            return {"CANCELLED"}
        if report["issues"]:
            self.report({"ERROR"}, "Export self-check failed: " + "; ".join(report["issues"]))
            return {"CANCELLED"}

        payload = manifest.build(
            asset_name=name,
            glb_bytes=data,
            glb_report=report,
            findings=findings,
            source=snapshot.source_info(context),
        )

        state.publish.start()
        state.publish.log(f"exported {report['triangles']:,} triangles, {report['counts']['materials']} material(s)")
        client = IngestClient(
            prefs.endpoint,
            prefs.token,
            timeout=prefs.timeout,
            log=state.publish.log,
            cancel=state.publish.cancel,
        )
        threading.Thread(
            target=_upload_worker,
            args=(client, data, filename, payload, workdir),
            name="handoff-upload",
            daemon=True,
        ).start()
        bpy.app.timers.register(_redraw_while_running, first_interval=0.2)
        return {"FINISHED"}


class HANDOFF_OT_cancel(bpy.types.Operator):
    bl_idname = "handoff.cancel"
    bl_label = "Cancel"
    bl_description = "Stop the upload after the current request"

    def execute(self, context):
        state.publish.cancel.set()
        return {"FINISHED"}


class HANDOFF_OT_select_object(bpy.types.Operator):
    bl_idname = "handoff.select_object"
    bl_label = "Select"
    bl_description = "Select the object this finding is about"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty()

    def execute(self, context):
        obj = context.scene.objects.get(self.name)
        if obj is None:
            self.report({"WARNING"}, f"{self.name} is no longer in the scene")
            return {"CANCELLED"}
        for other in context.selected_objects:
            other.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        return {"FINISHED"}


class HANDOFF_OT_open_docs(bpy.types.Operator):
    bl_idname = "handoff.open_docs"
    bl_label = "Explain"
    bl_description = "Open the troubleshooting entry for this finding"

    code: StringProperty()

    def execute(self, context):
        # GitHub heading anchors drop the dots: "texture.colorspace" -> "#texturecolorspace"
        bpy.ops.wm.url_open(url=f"{DOCS_URL}#{self.code.replace('.', '')}")
        return {"FINISHED"}


def _upload_worker(client, data, filename, payload, workdir):
    # Runs off the main thread: no bpy calls allowed in here.
    try:
        result = client.publish(data, filename, payload)
    except Cancelled:
        state.publish.log("cancelled")
        state.publish.finish("cancelled")
    except IngestError as exc:
        state.publish.log(f"failed: {exc}")
        state.publish.log(f"export kept for debugging: {workdir}")
        state.publish.finish("failed")
        return
    except Exception as exc:  # a worker that dies silently is worse than a noisy one
        state.publish.log(f"unexpected error: {exc!r}")
        state.publish.log(f"export kept for debugging: {workdir}")
        state.publish.finish("failed")
        return
    else:
        state.publish.finish("done", result)
    shutil.rmtree(workdir, ignore_errors=True)


def _redraw_while_running():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
    return 0.25 if state.publish.running else None


def shutdown():
    state.publish.cancel.set()
    if bpy.app.timers.is_registered(_redraw_while_running):
        bpy.app.timers.unregister(_redraw_while_running)
