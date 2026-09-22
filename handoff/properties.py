# SPDX-License-Identifier: GPL-3.0-or-later
import bpy
from bpy.props import EnumProperty, FloatProperty, IntProperty, StringProperty


class HandoffPreferences(bpy.types.AddonPreferences):
    # __package__ is "handoff" as a legacy add-on and "bl_ext.<repo>.handoff" as an extension.
    bl_idname = __package__

    endpoint: StringProperty(
        name="Ingest endpoint",
        description="Base URL of the ingest API",
        default="http://127.0.0.1:8765",
    )
    # Lives in user preferences, not in the scene, so it never travels inside a .blend
    # someone emails to a vendor.
    token: StringProperty(
        name="API token",
        description="Bearer token for the ingest API. Stored in Blender preferences, never in the .blend file",
        subtype="PASSWORD",
    )
    timeout: FloatProperty(
        name="Request timeout (s)",
        default=30.0, min=1.0, max=300.0,
    )
    triangle_budget: IntProperty(
        name="Triangle budget",
        description="Warn when the export exceeds this many triangles after modifiers",
        default=500_000, min=1_000,
    )

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "endpoint")
        col.prop(self, "token")
        col.prop(self, "timeout")
        col.prop(self, "triangle_budget")


class HandoffSceneSettings(bpy.types.PropertyGroup):
    asset_name: StringProperty(
        name="Asset name",
        description="Name sent to the ingest API. Defaults to the .blend file name",
    )
    scope: EnumProperty(
        name="Export",
        items=[
            ("SELECTED", "Selected", "Export only the selected objects"),
            ("VISIBLE", "Visible", "Export every visible object in the scene"),
        ],
        default="SELECTED",
    )
