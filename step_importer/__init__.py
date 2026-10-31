bl_info = {
    "name": "STEP Importer — NVIDIA USD BRep",
    "author": "STEP Importer contributors",
    "version": (0, 2, 0),
    "blender": (5, 2, 0),
    "location": "File > Import",
    "description": "Import STEP using Open CASCADE and NVIDIA USD BRep",
    "category": "Import-Export",
}

import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

import bpy
from bpy.props import BoolProperty, FloatProperty, StringProperty
from bpy_extras.io_utils import ImportHelper


active_import = None


def prepare_meshes(payload, scene_scale):
    """Validate worker data before creating Blender objects."""
    if payload.get("units") != "mm":
        raise ValueError("Expected millimeter coordinates.")

    if not math.isfinite(scene_scale) or scene_scale <= 0:
        raise ValueError("Invalid Blender scene unit scale.")

    scale = 0.001 / scene_scale
    prepared = []

    for item in payload["meshes"]:
        points = [
            tuple(component * scale for component in point)
            for point in item["points"]
        ]

        if not points or any(
            len(point) != 3
            or not all(math.isfinite(value) for value in point)
            for point in points
        ):
            raise ValueError("Invalid mesh coordinates.")

        stream = item["faces"]
        faces = []
        cursor = 0

        while cursor < len(stream):
            count = stream[cursor]

            if type(count) is not int or count < 3:
                raise ValueError("Invalid polygon size.")

            end = cursor + count + 1

            if end > len(stream):
                raise ValueError("Truncated polygon stream.")

            face = stream[cursor + 1:end]

            if any(
                type(index) is not int
                or index < 0
                or index >= len(points)
                for index in face
            ):
                raise ValueError("Polygon index out of range.")

            if len(set(face)) != count:
                raise ValueError("Polygon contains repeated vertices.")

            faces.append(face)
            cursor = end

        if not faces:
            raise ValueError("Mesh contains no polygons.")

        normals = item["normals"]

        if len(normals) != sum(map(len, faces)):
            raise ValueError("Corner-normal count does not match polygons.")

        if any(
            len(normal) != 3
            or not all(math.isfinite(value) for value in normal)
            or sum(value * value for value in normal) < 1e-20
            for normal in normals
        ):
            raise ValueError("Invalid surface normals.")

        prepared.append((item["name"], points, faces, normals))

    if not prepared:
        raise ValueError("Worker returned no meshes.")

    return prepared

def create_objects(context, payload, source, use_normals):
    prepared = prepare_meshes(
        payload,
        context.scene.unit_settings.scale_length,
    )

    collection = bpy.data.collections.new(source.stem)
    objects = []
    meshes = []

    try:
        context.scene.collection.children.link(collection)

        for name, points, faces, normals in prepared:
            # Find this body's bounding-box center in scene coordinates.
            center = tuple(
                (
                    min(point[axis] for point in points)
                    + max(point[axis] for point in points)
                ) * 0.5
                for axis in range(3)
            )

            # Store vertices relative to the new object origin.
            local_points = [
                tuple(
                    point[axis] - center[axis]
                    for axis in range(3)
                )
                for point in points
            ]

            mesh = bpy.data.meshes.new(name)
            meshes.append(mesh)

            mesh.from_pydata(local_points, [], faces)
            mesh.update()

            # Translation doesn't change the surface normals.
            if use_normals:
                for polygon in mesh.polygons:
                    polygon.use_smooth = True

                mesh.normals_split_custom_set(normals)

            obj = bpy.data.objects.new(name, mesh)
            objects.append(obj)

            # Restore the assembly placement through the object transform.
            # World position = local vertex position + object location.
            obj.location = center

            collection.objects.link(obj)
            obj["step_source"] = str(source)

    except Exception:
        for obj in objects:
            bpy.data.objects.remove(obj, do_unlink=True)

        for mesh in meshes:
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)

        bpy.data.collections.remove(collection)
        raise

    for obj in context.selected_objects:
        obj.select_set(False)

    for obj in objects:
        obj.select_set(True)

    context.view_layer.objects.active = objects[0]
    return len(objects)

class IMPORT_OT_step_nvidia(bpy.types.Operator, ImportHelper):
    bl_idname = "import_scene.step_nvidia"
    bl_label = "Import STEP"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".step"

    filter_glob: StringProperty(
        default="*.step;*.stp;*.STEP;*.STP",
        options={"HIDDEN"},
    )

    chord_mm: FloatProperty(
        name="Chord tolerance (mm)",
        default=0.1,
        min=0.000001,
        precision=4,
    )

    angle_deg: FloatProperty(
        name="Angular tolerance (degrees)",
        default=15.0,
        min=0.1,
        max=180.0,
    )

    use_normals: BoolProperty(
        name="Use CAD surface normals",
        default=True,
    )

    _process = None
    _timer = None
    _folder = None
    _log = None
    _wm = None

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and active_import is None

    def execute(self, context):
        global active_import

        if os.name != "nt":
            self.report(
                {"ERROR"},
                "This runtime package supports Windows x64.",
            )
            return {"CANCELLED"}

        # Everything is located relative to the installed add-on.
        folder = Path(__file__).resolve().parent
        runtime = folder / "runtime" / "windows-x64"

        python = runtime / "python" / "python.exe"
        worker = folder / "worker.py"

        self._source = Path(
            bpy.path.abspath(self.filepath)
        ).resolve()

        required = (
            python,
            worker,
            runtime / "step-reader" / "step-to-brep.exe",
            runtime / "nvidia" / "occt_to_usd.exe",
            runtime / "nvidia" / "omniSolid"
            / "resources" / "plugInfo.json",
        )

        missing = [
            path.name for path in required
            if not path.is_file()
        ]

        if missing:
            self.report(
                {"ERROR"},
                "Incomplete runtime package: " + ", ".join(missing),
            )
            return {"CANCELLED"}

        if not self._source.is_file():
            self.report(
                {"ERROR"},
                "The selected STEP file does not exist.",
            )
            return {"CANCELLED"}

        try:
            self._folder = Path(
                tempfile.mkdtemp(prefix="blender_step_")
            )
            self._output = self._folder / "mesh.json"
            self._log_path = self._folder / "worker.log"
            self._log = self._log_path.open(
                "w",
                encoding="utf-8",
            )

            command = [
                str(python),
                "-u",
                str(worker),
                str(self._source),
                str(self._output),
                "--chord-mm", str(self.chord_mm),
                "--angle-deg", str(self.angle_deg),
            ]

            # Isolate the worker from Blender's Python/USD settings.
            environment = os.environ.copy()

            for key in (
                "PYTHONHOME",
                "PYTHONPATH",
                "PXR_PLUGINPATH_NAME",
                "PXR_USD_WINDOWS_DLL_PATH",
                "OMNISOLID_PLUGIN_PATH",
            ):
                environment.pop(key, None)

            environment["PYTHONNOUSERSITE"] = "1"

            self._process = subprocess.Popen(
                command,
                stdout=self._log,
                stderr=subprocess.STDOUT,
                env=environment,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW
                    | subprocess.CREATE_NEW_PROCESS_GROUP
                ),
            )

            self._started = time.monotonic()
            self._wm = context.window_manager

            self._timer = self._wm.event_timer_add(
                0.25,
                window=context.window,
            )

            active_import = self
            self._wm.modal_handler_add(self)

            self.report(
                {"INFO"},
                "Converting STEP; press Esc to cancel.",
            )
            return {"RUNNING_MODAL"}

        except Exception as error:
            log_path = self.cleanup(keep_log=True)
            self.report(
                {"ERROR"},
                f"{error}. Log: {log_path}",
            )
            return {"CANCELLED"}

    def modal(self, context, event):
        if event.type == "ESC":
            self.cleanup()
            self.report({"INFO"}, "STEP import cancelled.")
            return {"CANCELLED"}

        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        # Overall import limit: 30 minutes.
        if time.monotonic() - self._started > 1800:
            log_path = self.cleanup(keep_log=True)
            self.report(
                {"ERROR"},
                f"Import timed out. Log: {log_path}",
            )
            return {"CANCELLED"}

        status = self._process.poll()

        if status is None:
            return {"PASS_THROUGH"}

        try:
            self._log.close()
            self._log = None

            if status != 0:
                tail = self._log_path.read_text(
                    encoding="utf-8",
                    errors="replace",
                )[-6000:]

                print("STEP worker error:\n" + tail)
                raise RuntimeError(
                    f"Worker exited with code {status}"
                )

            if context.mode != "OBJECT":
                raise RuntimeError(
                    "Return to Object Mode and retry."
                )

            with self._output.open(encoding="utf-8") as file:
                payload = json.load(file)

            count = create_objects(
                context,
                payload,
                self._source,
                self.use_normals,
            )

            self.cleanup()
            self.report(
                {"INFO"},
                f"Imported {count} mesh object(s).",
            )
            return {"FINISHED"}

        except Exception as error:
            log_path = self.cleanup(keep_log=True)
            self.report(
                {"ERROR"},
                f"{error}. Log: {log_path}",
            )
            return {"CANCELLED"}

    def cleanup(self, keep_log=False):
        global active_import

        process = self._process

        if process is not None and process.poll() is None:
            try:
                subprocess.run(
                    [
                        "taskkill",
                        "/PID", str(process.pid),
                        "/T",
                        "/F",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    timeout=10,
                    check=False,
                )
                process.wait(timeout=10)

            except (OSError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    process.kill()

        self._process = None

        if self._timer is not None:
            self._wm.event_timer_remove(self._timer)
            self._timer = None

        if self._log is not None:
            self._log.close()
            self._log = None

        log_path = (
            str(self._folder / "worker.log")
            if self._folder else "not created"
        )

        if self._folder is not None and not keep_log:
            shutil.rmtree(self._folder, ignore_errors=True)

        self._folder = None

        if active_import is self:
            active_import = None

        return log_path

    def cancel(self, context):
        self.cleanup()


def import_menu(self, context):
    self.layout.operator(
        IMPORT_OT_step_nvidia.bl_idname,
        text="STEP — NVIDIA USD BRep (.step/.stp)",
    )


def register():
    bpy.utils.register_class(IMPORT_OT_step_nvidia)
    bpy.types.TOPBAR_MT_file_import.append(import_menu)


def unregister():
    if active_import is not None:
        active_import.cleanup()

    bpy.types.TOPBAR_MT_file_import.remove(import_menu)
    bpy.utils.unregister_class(IMPORT_OT_step_nvidia)