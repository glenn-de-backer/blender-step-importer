bl_info = {
    "name": "SimpleStep importer",
    "author": "SimpleStep contributors",
    "version": (1, 5, 3),
    "blender": (5, 2, 0),
    "location": "File > Import",
    "description": "Import STEP using Open CASCADE and NVIDIA USD BRep",
    "category": "Import-Export",
}

import bpy
from . import core
from .ui import IMPORT_OT_step_nvidia

_file_load_handler = None


def import_menu(self, context):
    self.layout.operator(
        IMPORT_OT_step_nvidia.bl_idname,
        text="SimpleStep importer (.step/.stp)",
    )


def register():
    global _file_load_handler
    bpy.utils.register_class(IMPORT_OT_step_nvidia)
    bpy.types.TOPBAR_MT_file_import.append(import_menu)
    if _file_load_handler is None:
        _file_load_handler = bpy.app.handlers.persistent(core.clear_on_file_load)
    for handlers in (bpy.app.handlers.load_pre, bpy.app.handlers.load_post,
                     bpy.app.handlers.load_factory_startup_post):
        if _file_load_handler not in handlers:
            handlers.append(_file_load_handler)


def unregister():
    if _file_load_handler is not None:
        for handlers in (bpy.app.handlers.load_pre, bpy.app.handlers.load_post,
                         bpy.app.handlers.load_factory_startup_post):
            if _file_load_handler in handlers:
                handlers.remove(_file_load_handler)
    if core.active_import is not None:
        core.active_import.cleanup()
    core.clear_viewport_notice()
    bpy.types.TOPBAR_MT_file_import.remove(import_menu)
    bpy.utils.unregister_class(IMPORT_OT_step_nvidia)
