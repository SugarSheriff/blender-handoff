# SPDX-License-Identifier: GPL-3.0-or-later
import textwrap

import bpy

from . import state
from .core.preflight import ERROR, WARN

SEVERITY_ICON = {ERROR: "CANCEL", WARN: "ERROR", "info": "INFO"}


def _wrapped(layout, context, text, indent=""):
    # Panel labels do not wrap, so estimate a width from the sidebar size.
    width = max(24, int(context.region.width / 7.5))
    for line in textwrap.wrap(text, width):
        layout.label(text=indent + line)


class _HandoffPanel:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Handoff"


class HANDOFF_PT_main(_HandoffPanel, bpy.types.Panel):
    bl_label = "Handoff"

    def draw(self, context):
        settings = context.scene.handoff
        prefs = context.preferences.addons[__package__].preferences
        layout = self.layout
        layout.prop(settings, "asset_name")
        layout.row().prop(settings, "scope", expand=True)
        row = layout.row()
        row.enabled = False
        row.label(text=prefs.endpoint or "No endpoint set", icon="URL")


class HANDOFF_PT_preflight(_HandoffPanel, bpy.types.Panel):
    bl_label = "Preflight"
    bl_parent_id = "HANDOFF_PT_main"

    def draw(self, context):
        layout = self.layout
        layout.operator("handoff.preflight", icon="CHECKMARK")
        if not state.preflight_ran:
            layout.label(text="Not run yet")
            return
        if not state.findings:
            layout.label(text="No issues found", icon="CHECKMARK")
            return
        for finding in state.findings:
            box = layout.box()
            row = box.row(align=True)
            row.label(text=finding.target, icon=SEVERITY_ICON[finding.severity])
            if finding.object:
                row.operator("handoff.select_object", text="", icon="RESTRICT_SELECT_OFF").name = finding.object
            row.operator("handoff.open_docs", text="", icon="HELP").code = finding.code
            col = box.column(align=True)
            col.scale_y = 0.8
            _wrapped(col, context, finding.message)
            _wrapped(col, context, "Fix: " + finding.fix)


class HANDOFF_PT_publish(_HandoffPanel, bpy.types.Panel):
    bl_label = "Publish"
    bl_parent_id = "HANDOFF_PT_main"

    def draw(self, context):
        layout = self.layout
        publish = state.publish
        if publish.running:
            layout.operator("handoff.cancel", icon="X")
        else:
            layout.operator("handoff.publish", icon="EXPORT")

        status_icon = {"done": "CHECKMARK", "failed": "CANCEL", "cancelled": "X", "running": "SORTTIME"}
        if publish.status != "idle":
            layout.label(text=publish.status.capitalize(), icon=status_icon.get(publish.status, "NONE"))
        if publish.result:
            layout.label(text=f"Asset {publish.result.get('asset_id')}", icon="PACKAGE")

        lines = publish.lines()
        if lines:
            col = layout.box().column(align=True)
            col.scale_y = 0.75
            for line in lines[-8:]:
                _wrapped(col, context, line)
