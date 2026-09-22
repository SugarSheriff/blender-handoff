# SPDX-License-Identifier: GPL-3.0-or-later
"""Blender-free core: preflight rules, GLB inspection, manifest, and the ingest client.

Nothing in this package imports bpy, so all of it runs (and is tested) in plain Python.
The add-on modules one level up are the only code that talks to Blender.
"""
