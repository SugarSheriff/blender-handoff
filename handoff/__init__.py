# SPDX-License-Identifier: GPL-3.0-or-later
"""Handoff: preflight a Blender scene, export it as GLB, and publish it to an ingest API.

Blender 4.2+ loads this as an extension (blender_manifest.toml). bl_info below is only
read when installed as a legacy add-on on 3.6-4.1.
"""

bl_info = {
    "name": "Handoff",
    "author": "Liam Hulsey",
    "version": (0, 1, 0),
    "blender": (3, 6, 0),
    "location": "3D Viewport > Sidebar (N) > Handoff",
    "description": "Preflight a scene, export glTF, and publish it to an ingest API",
    "category": "Import-Export",
}

import bpy

from . import operators, panels, properties

_classes = (
    properties.HandoffPreferences,
    properties.HandoffSceneSettings,
    operators.HANDOFF_OT_preflight,
    operators.HANDOFF_OT_publish,
    operators.HANDOFF_OT_cancel,
    operators.HANDOFF_OT_select_object,
    operators.HANDOFF_OT_open_docs,
    panels.HANDOFF_PT_main,
    panels.HANDOFF_PT_preflight,
    panels.HANDOFF_PT_publish,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.handoff = bpy.props.PointerProperty(type=properties.HandoffSceneSettings)


def unregister():
    operators.shutdown()
    del bpy.types.Scene.handoff
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
