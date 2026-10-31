bl_info = {
    "name": "STEP Importer — NVIDIA USD BRep",
    "author": "STEP Importer contributors",
    "version": (1, 0, 4),
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
import blf
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

def create_objects_iter(context, payload, source, use_normals):
    """Create bodies incrementally; closing the iterator rolls back."""
    total = len(payload["meshes"])
    if not total:
        raise ValueError("Worker returned no meshes.")
    scale = context.scene.unit_settings.scale_length

    collection = bpy.data.collections.new(source.stem)
    objects = []
    meshes = []
    report_text = None

    try:
        context.scene.collection.children.link(collection)

        for index, item in enumerate(payload["meshes"], 1):
            # Validate one body at a time to reduce peak temporary memory.
            name, points, faces, normals = prepare_meshes(
                {"units": payload.get("units"), "meshes": [item]}, scale,
            )[0]
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
            repair = item.get("repair")
            obj["step_repaired"] = bool(repair)
            if repair:
                obj["step_faces_before_repair"] = repair["faces_before"]
                obj["step_faces_after_repair"] = repair["faces_after"]
                obj["step_cad_validation"] = "Not performed after repair"
                obj["step_non_manifold_repair"] = bool(
                    repair.get("accepted_non_manifold")
                )

            # Drop temporary coordinate lists before yielding to the UI.
            del points, local_points, faces, normals
            yield index, total, name

        repaired = [item for item in payload["meshes"] if item.get("repair")]
        skipped = payload.get("failed_parts", [])
        if repaired or skipped:
            report_text = bpy.data.texts.new("STEP Import Report")
            report_text.write(
                "STEP IMPORT REPORT\n"
                f"Source: {source}\n"
                f"Imported mesh objects: {len(objects)}\n"
                f"Skipped source bodies: {len(skipped)}\n"
                f"Repaired mesh objects: {len(repaired)}\n\n"
                "Skipped bodies are absent from the imported model.\n"
                "Repair modifies CAD topology. Successful tessellation and mesh "
                "manifold checks do not establish full CAD validity.\n\n"
                + json.dumps(
                    {
                        "repaired": [
                            {"name": item["name"], **item["repair"]}
                            for item in repaired
                        ],
                        "skipped": skipped,
                    },
                    indent=2, ensure_ascii=False, default=str,
                )
            )
            collection["step_import_report"] = report_text.name
            collection["step_repaired_count"] = len(repaired)
            collection["step_skipped_count"] = len(skipped)
            collection["step_partial_import"] = bool(skipped)

    except BaseException:
        # GeneratorExit is raised by .close() when the user cancels.
        if report_text is not None:
            bpy.data.texts.remove(report_text)
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


def create_objects(context, payload, source, use_normals):
    """Synchronous wrapper retained for tests and scripts."""
    iterator = create_objects_iter(context, payload, source, use_normals)
    while True:
        try:
            next(iterator)
        except StopIteration as finished:
            return finished.value


def draw_import_progress():
    """Draw in every 3D viewport while an import is active."""
    job = active_import
    region = bpy.context.region
    if job is None or region is None:
        return
    elapsed = int(time.monotonic() - job._started)
    lines = (
        "STEP Import",
        job._progress_message,
        f"Elapsed {elapsed // 60:02d}:{elapsed % 60:02d} | Esc to cancel",
    )
    font = 0
    blf.size(font, 16)
    blf.color(font, 1.0, 1.0, 1.0, 1.0)
    blf.enable(font, blf.SHADOW)
    blf.shadow(font, 5, 0.0, 0.0, 0.0, 1.0)
    blf.shadow_offset(font, 1, -1)
    try:
        for index, line in enumerate(lines):
            blf.position(font, 24, region.height - 100 - index * 24, 0)
            blf.draw(font, line[:120])
    finally:
        blf.disable(font, blf.SHADOW)


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

    repair_failed: BoolProperty(
        name="Repair failed bodies",
        description=(
            "After a tessellation failure, heal a copy and retry. "
            "Repair can change features; full CAD validity is not established. "
            "Repaired objects and an import report are marked"
        ),
        default=False,
    )

    allow_non_manifold_repair: BoolProperty(
        name="Allow non-manifold repaired meshes",
        description=(
            "Allow repaired meshes with non-manifold connectivity for visual "
            "inspection. Check the result before using operations that require "
            "manifold solids. Faces that fail tessellation are still rejected"
        ),
        default=False,
    )

    skip_failed: BoolProperty(
        name="Skip failed parts",
        description=(
            "Continue importing other bodies if a part fails conversion or "
            "tessellation. Skipped geometry is absent; names and errors are "
            "saved in STEP Import Report"
        ),
        default=False,
    )

    _process = None
    _timer = None
    _folder = None
    _log = None
    _wm = None
    _workspace = None
    _draw_handle = None
    _progress_active = False
    _progress_message = "Starting geometry worker..."
    _creator = None
    _payload = None
    _phase = "worker"

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
            self._progress_path = self._folder / "progress.json"
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
                "--progress-file", str(self._progress_path),
            ]

            if self.repair_failed:
                command.append("--repair-failed")
                if self.allow_non_manifold_repair:
                    command.append("--allow-non-manifold-repair")

            if self.skip_failed:
                command.append("--skip-failed")

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
            self._workspace = context.workspace
            self._phase = "worker"
            self._wm.progress_begin(0, 100)
            self._progress_active = True
            self._progress_message = "Starting geometry worker..."

            self._timer = self._wm.event_timer_add(
                0.1,
                window=context.window,
            )

            active_import = self
            self._draw_handle = bpy.types.SpaceView3D.draw_handler_add(
                draw_import_progress, (), "WINDOW", "POST_PIXEL",
            )
            self.update_progress(self._progress_message, 0)
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

    def redraw(self):
        if self._wm is not None:
            for window in self._wm.windows:
                for area in window.screen.areas:
                    area.tag_redraw()

    def update_progress(self, message, fraction=None):
        self._progress_message = message
        if self._workspace is not None:
            self._workspace.status_text_set(f"STEP Import: {message} | Esc to cancel")
        if fraction is not None and self._progress_active:
            self._wm.progress_update(max(0.0, min(1.0, fraction)) * 100)
        self.redraw()

    def read_worker_progress(self):
        try:
            with self._progress_path.open(encoding="utf-8") as file:
                packet = json.load(file)
        except (OSError, ValueError):
            return
        total = packet.get("body_total", 0)
        completed = packet.get("completed", 0)
        message = packet.get("message", "Converting STEP...")
        if total:
            message = (
                f"{message} | {completed}/{total} bodies processed "
                f"({completed / total:.0%})"
            )
        self.update_progress(message, completed / total if total else None)

    def finish_import(self, count):
        repaired = sum(bool(item.get("repair")) for item in self._payload["meshes"])
        non_manifold = sum(
            bool((item.get("repair") or {}).get("accepted_non_manifold"))
            for item in self._payload["meshes"]
        )
        skipped = len(self._payload.get("failed_parts", []))
        self.cleanup()
        if skipped or repaired:
            self.report(
                {"WARNING"},
                f"Imported {count} objects; skipped {skipped}, repaired {repaired} "
                f"({non_manifold} non-manifold). See STEP Import Report in Text Editor.",
            )
        else:
            self.report({"INFO"}, f"Imported {count} mesh object(s).")
        return {"FINISHED"}

    def modal(self, context, event):
        if event.type == "ESC":
            self.cleanup()
            self.report({"INFO"}, "STEP import cancelled.")
            return {"CANCELLED"}
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        if time.monotonic() - self._started > 1800:
            log_path = self.cleanup(keep_log=True)
            self.report({"ERROR"}, f"Import timed out. Log: {log_path}")
            return {"CANCELLED"}

        try:
            if self._phase == "creating":
                if context.mode != "OBJECT":
                    raise RuntimeError("Return to Object Mode and retry.")
                tick_started = time.monotonic()
                # Batch small parts, but yield to Blender between batches.
                # A single expensive body can exceed this soft time budget.
                for _ in range(8):
                    try:
                        completed, total, name = next(self._creator)
                    except StopIteration as finished:
                        self._creator = None
                        return self.finish_import(finished.value)
                    if time.monotonic() - tick_started >= 0.03:
                        break
                self.update_progress(
                    f"Creating Blender objects: {completed}/{total} — {name}",
                    completed / total,
                )
                return {"RUNNING_MODAL"}

            if self._phase == "loading":
                # File loading is kept as its own tick so its status can draw.
                with self._output.open(encoding="utf-8") as file:
                    self._payload = json.load(file)
                if context.mode != "OBJECT":
                    raise RuntimeError("Return to Object Mode and retry.")
                self._creator = create_objects_iter(
                    context, self._payload, self._source, self.use_normals,
                )
                self._phase = "creating"
                self.update_progress(
                    f"Creating Blender objects: 0/{len(self._payload['meshes'])}", 0,
                )
                return {"RUNNING_MODAL"}

            self.read_worker_progress()
            status = self._process.poll()
            if status is None:
                self.redraw()  # Keep elapsed time live during native calls.
                return {"RUNNING_MODAL"}
            self._log.close()
            self._log = None
            if status != 0:
                tail = self._log_path.read_text(
                    encoding="utf-8", errors="replace",
                )[-6000:]
                print("STEP worker error:\n" + tail)
                raise RuntimeError(f"Worker exited with code {status}")
            self._phase = "loading"
            self.update_progress("Loading converted mesh data...")
            return {"RUNNING_MODAL"}
        except Exception as error:
            log_path = self.cleanup(keep_log=True)
            self.report({"ERROR"}, f"{error}. Log: {log_path}")
            return {"CANCELLED"}

    def cleanup(self, keep_log=False):
        global active_import

        if self._creator is not None:
            self._creator.close()  # Roll back a partially created import.
            self._creator = None
        self._payload = None
        if self._draw_handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self._draw_handle, "WINDOW")
            self._draw_handle = None
        if self._workspace is not None:
            self._workspace.status_text_set(None)
            self._workspace = None
        if self._progress_active:
            self._wm.progress_end()
            self._progress_active = False

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
        self.redraw()

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