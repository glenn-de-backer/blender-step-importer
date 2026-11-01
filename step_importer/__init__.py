bl_info = {
    "name": "SimpleStep importer",
    "author": "SimpleStep contributors",
    "version": (1, 5, 0),
    "blender": (5, 2, 0),
    "location": "File > Import",
    "description": "Import STEP using Open CASCADE and NVIDIA USD BRep",
    "category": "Import-Export",
}

import bpy
from . import core
from .ui import IMPORT_OT_step_nvidia


def import_menu(self, context):
    self.layout.operator(
        IMPORT_OT_step_nvidia.bl_idname,
        text="SimpleStep importer (.step/.stp)",
    )


def register():
    bpy.utils.register_class(IMPORT_OT_step_nvidia)
    bpy.types.TOPBAR_MT_file_import.append(import_menu)


def unregister():
    if core.active_import is not None:
        core.active_import.cleanup()
    core.clear_viewport_notice()
    bpy.types.TOPBAR_MT_file_import.remove(import_menu)
    bpy.utils.unregister_class(IMPORT_OT_step_nvidia)
