import colorsys
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


active_import = None
viewport_notice = None
notice_draw_handle = None
notice_timer_running = False
NOTICE_SECONDS = 6.0


def current_scene_key():
    scene = getattr(bpy.context, "scene", None)
    if scene is None:
        return None
    as_pointer = getattr(scene, "as_pointer", None)
    return as_pointer() if as_pointer is not None else id(scene)


def redraw_viewports():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def clear_viewport_notice(*, cancel_timer=True):
    global viewport_notice, notice_draw_handle, notice_timer_running
    viewport_notice = None
    timers = bpy.app.timers
    is_registered = getattr(timers, "is_registered", None)
    if cancel_timer and is_registered is not None and is_registered(expire_viewport_notice):
        timers.unregister(expire_viewport_notice)
    notice_timer_running = False
    if notice_draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(notice_draw_handle, "WINDOW")
        notice_draw_handle = None
    redraw_viewports()


def expire_viewport_notice():
    global notice_timer_running
    if viewport_notice is None:
        notice_timer_running = False
        return None
    if (viewport_notice.get("scene_key") is not None
            and viewport_notice["scene_key"] != current_scene_key()):
        clear_viewport_notice(cancel_timer=False)
        return None
    remaining = viewport_notice["until"] - time.monotonic()
    if remaining > 0:
        return max(0.1, remaining)
    clear_viewport_notice(cancel_timer=False)
    return None


def show_viewport_notice(*lines, seconds=NOTICE_SECONDS):
    """Show the import result in the 3D view after the modal job ends."""
    global viewport_notice, notice_draw_handle, notice_timer_running
    viewport_notice = {
        "lines": lines,
        "until": time.monotonic() + seconds,
        "scene_key": current_scene_key(),
    }
    if notice_draw_handle is None:
        notice_draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            draw_import_progress, (), "WINDOW", "POST_PIXEL",
        )
    is_registered = getattr(bpy.app.timers, "is_registered", None)
    if not (is_registered(expire_viewport_notice) if is_registered else notice_timer_running):
        notice_timer_running = True
        bpy.app.timers.register(expire_viewport_notice, first_interval=seconds)
    redraw_viewports()


def clear_on_file_load(_filepath):
    """Drop stale draw handlers when Blender loads a file or startup scene."""
    if active_import is not None:
        active_import.cleanup()
    clear_viewport_notice()


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


def step_color_material(rgba, cache, created):
    """Share materials by color while allowing different colors per occurrence."""
    color = tuple(float(value) for value in rgba)
    if len(color) != 4 or any(not math.isfinite(v) or v < 0 or v > 1 for v in color):
        raise ValueError("Invalid STEP part color.")
    key = tuple(round(value, 6) for value in color)
    if key not in cache:
        material = bpy.data.materials.new(
            "STEP #{:02X}{:02X}{:02X}".format(
                *(round(value * 255) for value in color[:3])))
        created.append(material)
        material.diffuse_color = color
        material.use_nodes = True
        principled = material.node_tree.nodes.get("Principled BSDF")
        if principled is not None:
            principled.inputs["Base Color"].default_value = color
            principled.inputs["Alpha"].default_value = color[3]
        cache[key] = material
    return cache[key]


def generated_part_color(identifier):
    """Give each uncolored prototype a stable, visually distinct color."""
    hue = (identifier * 0.6180339887498949) % 1.0
    return (*colorsys.hsv_to_rgb(hue, 0.55, 0.85), 1.0)


def read_worker_timings(log_path):
    """Read the final timing packet before temporary job files are removed."""
    try:
        # The marker is at the end; large native logs need not be read in full.
        with log_path.open("rb") as file:
            file.seek(0, os.SEEK_END)
            file.seek(max(0, file.tell() - 16384))
            tail = file.read().decode("utf-8", errors="replace")
        for line in reversed(tail.splitlines()):
            if line.startswith("STEP_TIMING "):
                return json.loads(line[len("STEP_TIMING "):])
    except (OSError, ValueError):
        pass
    return None


def follow_import_view(context, obj, state, *, force=False):
    """Frame accumulated world bounds without changing the viewport rotation."""
    if obj is not None:
        from mathutils import Vector

        bounds = state.setdefault("bounds", [[math.inf] * 3, [-math.inf] * 3])
        for corner in obj.bound_box:
            point = obj.matrix_world @ Vector(corner)
            for axis in range(3):
                bounds[0][axis] = min(bounds[0][axis], point[axis])
                bounds[1][axis] = max(bounds[1][axis], point[axis])

    bounds = state.get("bounds")
    if bounds is None:
        return
    now = time.monotonic()
    if not force and now - state.get("last_update", -math.inf) < 0.4:
        return

    windows = list(context.window_manager.windows)
    preferred = getattr(context, "window", None)
    ordered = ([preferred] if preferred is not None else [])
    ordered.extend(window for window in windows if window != preferred)
    for window in ordered:
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            region = area.spaces.active.region_3d
            if region is None or region.view_perspective == "CAMERA":
                continue
            center = [(bounds[0][i] + bounds[1][i]) * 0.5 for i in range(3)]
            diagonal = math.sqrt(sum((bounds[1][i] - bounds[0][i]) ** 2
                                     for i in range(3)))
            region.view_location = center
            region.view_distance = max(0.05, diagonal * 1.5)
            area.tag_redraw()
            state["last_update"] = now
            return


def create_objects_iter(context, payload, source, use_normals,
                        origin_mode="CENTER", share_meshes=True, source_up_axis="Z",
                        use_step_colors=True, color_uncolored_parts=False,
                        follow_import_build=False):
    """Create bodies incrementally; closing the iterator rolls back."""
    if origin_mode not in {"CENTER", "CAD"}:
        raise ValueError("Unknown object origin mode.")
    if source_up_axis not in {"X", "Y", "Z"}:
        raise ValueError("Unknown source up axis.")
    if payload.get("manifest_version") == 4:
        return (yield from create_instances_iter(
            context, payload, source, use_normals, origin_mode, share_meshes,
            source_up_axis, use_step_colors, color_uncolored_parts,
            follow_import_build))
    up_rotation = source_up_rotation(source_up_axis) if source_up_axis != "Z" else None
    total = len(payload["meshes"])
    if not total:
        raise ValueError("Worker returned no meshes.")
    scale = context.scene.unit_settings.scale_length

    collection = bpy.data.collections.new(source.stem)
    collections = {0: collection}
    created_collections = [collection]
    objects = []
    meshes = []
    report_text = None
    color_materials = {}
    created_materials = []
    view_state = {}

    try:
        context.scene.collection.children.link(collection)
        collection["step_manifest_version"] = payload.get("manifest_version", 2)

        # The C++ manifest lists assembly occurrences in parent-first order.
        # Collections organize objects without changing their world geometry.
        for node in payload.get("assemblies", []):
            node_id = node["id"]
            parent = collections.get(node["parent_id"])
            if (parent is None or node_id in collections
                    or not isinstance(node["name"], str)):
                raise ValueError("Invalid assembly hierarchy in mesh result.")
            child = bpy.data.collections.new(node["name"])
            created_collections.append(child)
            parent.children.link(child)
            child["step_assembly_id"] = node_id
            collections[node_id] = child

        for index, item in enumerate(payload["meshes"], 1):
            # Validate one body at a time to reduce peak temporary memory.
            name, points, faces, normals = prepare_meshes(
                {"units": payload.get("units"), "meshes": [item]}, scale,
            )[0]
            # For older flat manifests points are already world-positioned.
            # CAD keeps their original origin; Center changes only the origin.
            center = (
                tuple((min(point[axis] for point in points)
                       + max(point[axis] for point in points)) * 0.5
                      for axis in range(3))
                if origin_mode == "CENTER" else (0.0, 0.0, 0.0)
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

            if use_step_colors:
                color = item.get("color")
                if color is None and color_uncolored_parts:
                    color = generated_part_color(index)
                if color is not None:
                    mesh.materials.append(step_color_material(
                        color, color_materials, created_materials))

            # Keep the STEP axes and the original body placement.
            if up_rotation is None:
                obj.location = center
            else:
                from mathutils import Matrix, Vector
                obj.matrix_world = up_rotation @ Matrix.Translation(Vector(center))

            assembly_id = item.get("assembly_id", 0)
            target_collection = collections.get(assembly_id)
            if target_collection is None:
                raise ValueError(f"Missing assembly for body {name}.")
            target_collection.objects.link(obj)
            obj["step_source"] = str(source)
            obj["step_assembly_id"] = assembly_id
            obj["step_origin_mode"] = origin_mode
            obj["step_source_up_axis"] = source_up_axis
            repair = item.get("repair")
            fallback_info = item.get("fallback")
            obj["step_mesher"] = "OCCT" if fallback_info else "NVIDIA"
            obj["step_coarser_retry"] = bool(fallback_info and fallback_info.get("coarser_retry"))
            obj["step_finer_retry"] = bool(fallback_info and fallback_info.get("finer_retry"))
            obj["step_repaired"] = bool(repair)
            if repair:
                obj["step_faces_before_repair"] = repair["faces_before"]
                obj["step_faces_after_repair"] = repair["faces_after"]
                obj["step_cad_validation"] = "Not performed after repair"
                obj["step_non_manifold_repair"] = bool(
                    repair.get("accepted_non_manifold")
                )

            if follow_import_build:
                follow_import_view(context, obj, view_state)

            # Drop temporary coordinate lists before yielding to the UI.
            del points, local_points, faces, normals
            yield index, total, name

        if follow_import_build:
            follow_import_view(context, None, view_state, force=True)

        repaired = [item for item in payload["meshes"] if item.get("repair")]
        skipped = payload.get("failed_parts", [])
        fallback = [{"name": item["name"], **item["fallback"]}
                    for item in payload["meshes"] if item.get("fallback")]
        coarser = sum(bool(item.get("coarser_retry")) for item in fallback)
        finer = sum(bool(item.get("finer_retry")) for item in fallback)
        if repaired or skipped or fallback:
            report_text = bpy.data.texts.new("SimpleStep Import Report")
            report_text.write(
                "SIMPLESTEP IMPORT REPORT\n"
                f"Source: {source}\n"
                f"Imported mesh objects: {len(objects)}\n"
                f"Skipped source bodies: {len(skipped)}\n"
                f"Repaired mesh objects: {len(repaired)}\n"
                f"Coarser OCCT recoveries: {coarser}\n"
                f"Finer OCCT recoveries: {finer}\n\n"
                "Skipped bodies are absent from the imported model.\n"
                "Coarser recoveries passed OCCT face coverage checks at reduced "
                "detail; inspect the geometry visually.\n"
                "Finer recoveries may contain more triangles and need visual inspection.\n"
                "OCCT fallback: seam continuity is not verified. Check recovered NURBS faces visually.\n"
                "Repair modifies CAD topology. Successful tessellation and mesh "
                "manifold checks do not establish full CAD validity.\n\n"
                + json.dumps(
                    {
                        "repaired": [
                            {"name": item["name"], **item["repair"]}
                            for item in repaired
                        ],
                        "skipped": skipped,
                        "occt_fallback": fallback,
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

        for material in created_materials:
            if material.users == 0:
                bpy.data.materials.remove(material)

        for created in reversed(created_collections):
            bpy.data.collections.remove(created)
        raise

    for obj in context.selected_objects:
        obj.select_set(False)

    for obj in objects:
        obj.select_set(True)

    context.view_layer.objects.active = objects[0]
    return len(objects)


def source_up_rotation(source_up_axis):
    """Rotate source coordinates into Blender Z-up without mirroring them."""
    from mathutils import Matrix

    if source_up_axis == "Z":
        return Matrix.Identity(4)
    if source_up_axis == "Y":
        return Matrix.Rotation(math.pi / 2, 4, "X")
    if source_up_axis == "X":
        return Matrix.Rotation(-math.pi / 2, 4, "Y")
    raise ValueError("Unknown source up axis.")


def create_instances_iter(context, payload, source, use_normals,
                          origin_mode="CENTER", share_meshes=True,
                          source_up_axis="Z", use_step_colors=True,
                          color_uncolored_parts=False, follow_import_build=False):
    """Convert each prototype once, optionally copy its Blender mesh per instance."""
    from mathutils import Matrix, Vector

    if origin_mode not in {"CENTER", "CAD"}:
        raise ValueError("Unknown object origin mode.")
    up_rotation = source_up_rotation(source_up_axis)
    if payload.get("units") != "mm" or not payload.get("instances"):
        raise ValueError("Missing prototype instances or invalid units.")
    scene_scale = context.scene.unit_settings.scale_length
    if not math.isfinite(scene_scale) or scene_scale <= 0:
        raise ValueError("Invalid Blender scene unit scale.")
    unit_scale = 0.001 / scene_scale
    definitions = {}
    for item in payload["meshes"]:
        identifier = item.get("prototype_id")
        if type(identifier) is not int or identifier <= 0 or identifier in definitions:
            raise ValueError("Duplicate or invalid prototype mesh.")
        definitions[identifier] = item

    collection = bpy.data.collections.new(source.stem)
    collections = {0: collection}
    created_collections = [collection]
    created_meshes = []
    created_materials = []
    color_materials = {}
    colored_prototypes = (
        {inst["prototype_id"] for inst in payload["instances"]
         if color_uncolored_parts or inst.get("color") is not None}
        if use_step_colors else set()
    )
    objects = []
    report_text = None
    view_state = {}
    try:
        context.scene.collection.children.link(collection)
        collection["step_manifest_version"] = 4
        for node in payload.get("assemblies", []):
            parent = collections.get(node["parent_id"])
            if parent is None or node["id"] in collections:
                raise ValueError("Invalid assembly hierarchy.")
            child = bpy.data.collections.new(node["name"])
            created_collections.append(child)
            parent.children.link(child)
            child["step_assembly_id"] = node["id"]
            collections[node["id"]] = child

        cached = {}
        used_prototypes = set()
        available = sum(inst["prototype_id"] in definitions for inst in payload["instances"])
        for inst in payload["instances"]:
            identifier = inst["prototype_id"]
            if identifier not in definitions:
                continue  # Failed prototype; recorded for every skipped occurrence.
            if identifier not in cached:
                item = definitions[identifier]
                name, points, faces, normals = prepare_meshes(
                    {"units": "mm", "meshes": [item]}, scene_scale)[0]
                center = Vector(
                    tuple((min(p[axis] for p in points)
                           + max(p[axis] for p in points)) / 2
                          for axis in range(3))
                    if origin_mode == "CENTER" else (0.0, 0.0, 0.0)
                )
                local = [tuple(Vector(p) - center) for p in points]
                mesh = bpy.data.meshes.new(name)
                created_meshes.append(mesh)
                mesh.from_pydata(local, [], faces)
                mesh.update()
                if identifier in colored_prototypes:
                    # A material slot on the mesh allows per-object material
                    # overrides even when instances share the same geometry.
                    mesh.materials.append(step_color_material(
                        (0.8, 0.8, 0.8, 1.0), color_materials,
                        created_materials))
                if use_normals:
                    for polygon in mesh.polygons:
                        polygon.use_smooth = True
                    mesh.normals_split_custom_set(normals)
                cached[identifier] = (mesh, center)
            mesh, center = cached[identifier]
            if not share_meshes and identifier in used_prototypes:
                mesh = mesh.copy()
                created_meshes.append(mesh)
            used_prototypes.add(identifier)
            values = inst["transform"]
            if (len(values) != 16 or not all(math.isfinite(v) for v in values)
                    or list(values[12:]) != [0, 0, 0, 1]):
                raise ValueError("Invalid placement transform.")
            matrix_rows = [list(values[i:i + 4]) for i in range(0, 16, 4)]
            for row in range(3):
                matrix_rows[row][3] *= unit_scale
            target = collections.get(inst["assembly_id"])
            if target is None:
                raise ValueError("Missing assembly for instance.")
            obj = bpy.data.objects.new(inst["name"], mesh)
            objects.append(obj)
            target.objects.link(obj)
            obj.matrix_world = up_rotation @ Matrix(matrix_rows) @ Matrix.Translation(center)
            if identifier in colored_prototypes:
                slot = obj.material_slots[0]
                slot.link = "OBJECT"
                color = inst.get("color")
                if color is None and color_uncolored_parts:
                    color = generated_part_color(identifier)
                if color is not None:
                    slot.material = step_color_material(
                        color, color_materials, created_materials)
                else:
                    slot.material = color_materials[(0.8, 0.8, 0.8, 1.0)]
            obj["step_source"] = str(source)
            obj["step_assembly_id"] = inst["assembly_id"]
            obj["step_prototype_id"] = identifier
            obj["step_origin_mode"] = origin_mode
            obj["step_source_up_axis"] = source_up_axis
            obj["step_shared_mesh"] = bool(share_meshes)
            item = definitions[identifier]
            obj["step_mesher"] = "OCCT" if item.get("fallback") else "NVIDIA"
            obj["step_repaired"] = bool(item.get("repair"))
            if follow_import_build:
                follow_import_view(context, obj, view_state)
            yield len(objects), available, inst["name"]

        if follow_import_build:
            follow_import_view(context, None, view_state, force=True)

        skipped = payload.get("failed_parts", [])
        recovered = [inst for inst in payload["instances"]
                     if definitions.get(inst["prototype_id"], {}).get("fallback")]
        repaired = [inst for inst in payload["instances"]
                    if definitions.get(inst["prototype_id"], {}).get("repair")]
        if skipped or recovered or repaired:
            report_text = bpy.data.texts.new("SimpleStep Import Report")
            report_text.write(
                f"SIMPLESTEP IMPORT REPORT\nSource: {source}\n"
                f"Imported objects: {len(objects)}; unique meshes: {len(cached)}\n"
                f"Skipped occurrences: {len(skipped)}; OCCT fallback occurrences: {len(recovered)}; "
                f"repaired occurrences: {len(repaired)}\n\n"
                + json.dumps({"skipped": skipped, "fallback": recovered,
                              "repaired": repaired}, ensure_ascii=False, indent=2))
            collection["step_import_report"] = report_text.name
            collection["step_skipped_count"] = len(skipped)
            collection["step_partial_import"] = bool(skipped)
    except BaseException:
        if report_text is not None:
            bpy.data.texts.remove(report_text)
        for obj in objects:
            bpy.data.objects.remove(obj, do_unlink=True)
        for mesh in created_meshes:
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        for material in created_materials:
            if material.users == 0:
                bpy.data.materials.remove(material)
        for created in reversed(created_collections):
            bpy.data.collections.remove(created)
        raise
    for obj in context.selected_objects:
        obj.select_set(False)
    for obj in objects:
        obj.select_set(True)
    context.view_layer.objects.active = objects[0]
    return len(objects)


def create_objects(context, payload, source, use_normals,
                   origin_mode="CENTER", share_meshes=True, source_up_axis="Z",
                   use_step_colors=True, color_uncolored_parts=False,
                   follow_import_build=False):
    """Synchronous wrapper retained for tests and scripts."""
    iterator = create_objects_iter(
        context, payload, source, use_normals, origin_mode, share_meshes,
        source_up_axis, use_step_colors, color_uncolored_parts,
        follow_import_build,
    )
    while True:
        try:
            next(iterator)
        except StopIteration as finished:
            return finished.value


def draw_import_progress():
    """Draw import progress or a brief result at the lower left of the view."""
    job = active_import
    region = bpy.context.region
    if region is None:
        return
    if job is not None:
        elapsed = int(time.monotonic() - job._started)
        source = getattr(job, "_source", None)
        title = f"STEP Import: {source.name}" if source else "STEP Import"
        status = job._progress_message
        if " | " in status:
            action, progress = status.rsplit(" | ", 1)
            lines = (title, action, progress)
        else:
            lines = (title, status)
        lines += (f"Elapsed {elapsed // 60:02d}:{elapsed % 60:02d} | Esc to cancel",)
    elif viewport_notice is not None:
        if (time.monotonic() >= viewport_notice.get("until", float("inf"))
                or (viewport_notice.get("scene_key") is not None
                    and viewport_notice["scene_key"] != current_scene_key())):
            clear_viewport_notice()
            return
        lines = viewport_notice["lines"]
    else:
        return
    font = 0
    # BLF text is custom UI, so honor the display scale used by Blender.
    preferences = getattr(bpy.context, "preferences", None)
    system = getattr(preferences, "system", None)
    ui_scale = getattr(system, "ui_scale", 1.0)
    if not isinstance(ui_scale, (int, float)) or not math.isfinite(ui_scale):
        ui_scale = 1.0
    ui_scale = min(2.0, max(1.0, ui_scale))
    title_size = round(16 * ui_scale)
    detail_size = round(12 * ui_scale)
    row_height = round(21 * ui_scale)
    margin = round(18 * ui_scale)
    left_margin = round(12 * ui_scale)
    blf.color(font, 1.0, 1.0, 1.0, 1.0)
    blf.enable(font, blf.SHADOW)
    blf.shadow(font, 5, 0.0, 0.0, 0.0, 1.0)
    blf.shadow_offset(font, 1, -1)
    try:
        max_width = max(20, region.width - left_margin * 2)
        for index, line in enumerate(lines):
            blf.size(font, title_size if index == 0 else detail_size)
            # Cut at a character boundary so even long STEP filenames fit.
            if blf.dimensions(font, line)[0] > max_width:
                low, high = 0, len(line)
                while low < high:
                    middle = (low + high + 1) // 2
                    if blf.dimensions(font, line[:middle] + "…")[0] <= max_width:
                        low = middle
                    else:
                        high = middle - 1
                line = line[:low] + "…"
            blf.position(font, left_margin,
                         margin + (len(lines) - 1 - index) * row_height, 0)
            blf.draw(font, line)
    finally:
        blf.disable(font, blf.SHADOW)


class StepImportEngine:
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

        clear_viewport_notice()

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

            chord_mm, angle_deg = self.effective_tolerances()
            command = [
                str(python),
                "-u",
                str(worker),
                str(self._source),
                str(self._output),
                "--chord-mm", str(chord_mm),
                "--angle-deg", str(angle_deg),
                "--progress-file", str(self._progress_path),
                "--jobs", str(self.parallel_bodies),
            ]

            if self.quality == "ADAPTIVE":
                command.extend([
                    "--adaptive",
                    "--adaptive-percent", str(self.adaptive_percent),
                    "--adaptive-min-chord-mm", str(self.adaptive_min_chord_mm),
                    "--adaptive-max-chord-mm", str(self.adaptive_max_chord_mm),
                ])

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
            # The viewport and status bar already show progress. Blender's
            # progress API adds an unwanted percentage beside the pointer.
            self._progress_active = False
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
            self._workspace.status_text_set(f"SimpleStep importer: {message} | Esc to cancel")
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
        result_items = self._payload["meshes"]
        if self._payload.get("manifest_version") == 4:
            by_id = {item["prototype_id"]: item for item in result_items}
            result_items = [by_id[inst["prototype_id"]]
                            for inst in self._payload["instances"]
                            if inst["prototype_id"] in by_id]
        repaired = sum(bool(item.get("repair")) for item in result_items)
        non_manifold = sum(
            bool((item.get("repair") or {}).get("accepted_non_manifold"))
            for item in result_items
        )
        skipped = len(self._payload.get("failed_parts", []))
        fallback_count = sum(bool(item.get("fallback")) for item in result_items)
        coarser_count = sum(bool((item.get("fallback") or {}).get("coarser_retry"))
                            for item in result_items)
        finer_count = sum(bool((item.get("fallback") or {}).get("finer_retry"))
                          for item in result_items)
        title = f"SimpleStep importer — {self._source.name}"
        log_timings = read_worker_timings(self._log_path)
        timings = dict(log_timings or self._payload.get("timings") or {})
        timings["worker_timing_source"] = (
            "log" if log_timings else
            "mesh_result" if self._payload.get("timings") else "unavailable"
        )
        timings["blender_json_load"] = round(getattr(self, "_json_load_seconds", 0.0), 3)
        timings["blender_create_objects"] = round(
            time.monotonic() - getattr(self, "_creating_started", time.monotonic()), 3,
        )
        report = bpy.data.texts.new("SimpleStep Timing Report")
        report.write(
            f"Source: {self._source}\n"
            "Times in seconds. Conversion and tessellation sums include "
            "overlapping parallel workers and only completed bodies; "
            "compare wall times for total speed.\n"
            "If worker_timing_source is unavailable, the installed worker did "
            "not provide measurements.\n\n"
            + json.dumps(timings, indent=2) + "\n"
        )
        print(f"SimpleStep timing: {timings}")
        worker_wall = timings.get("total_worker_wall", timings.get("worker_before_write_wall"))
        blender_wall = timings["blender_json_load"] + timings["blender_create_objects"]
        time_line = (
            f"Time: worker {worker_wall:.1f}s | Blender {blender_wall:.1f}s"
            if isinstance(worker_wall, (int, float))
            else f"Time: worker unavailable | Blender {blender_wall:.1f}s"
        )
        self.cleanup()
        if skipped or repaired or fallback_count:
            show_viewport_notice(
                title,
                f"Import complete: {count} objects",
                f"OCCT fallback: {fallback_count} ({coarser_count} coarser, "
                f"{finer_count} finer) | "
                f"Skipped: {skipped} | Repaired: {repaired} "
                f"({non_manifold} non-manifold)",
                time_line,
            )
        else:
            show_viewport_notice(title, f"Import complete: {count} mesh object(s)", time_line)
        return {"FINISHED"}

    def modal(self, context, event):
        if event.type == "ESC":
            self.cleanup()
            show_viewport_notice("SimpleStep import cancelled")
            return {"CANCELLED"}
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        if time.monotonic() - self._started > 1800:
            log_path = self.cleanup(keep_log=True)
            show_viewport_notice("SimpleStep import timed out", f"Log: {log_path}")
            return {"CANCELLED"}

        try:
            if self._phase == "creating":
                if context.mode != "OBJECT":
                    raise RuntimeError("Return to Object Mode and retry.")
                tick_started = time.monotonic()
                # Batch small parts, but yield to Blender between batches.
                # A single expensive body can exceed this soft time budget.
                preview_count = len(self._payload.get("instances") or self._payload["meshes"])
                batch_size = 1 if self.follow_import_build and preview_count <= 200 else 8
                for _ in range(batch_size):
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
                loading_started = time.monotonic()
                with self._output.open(encoding="utf-8") as file:
                    self._payload = json.load(file)
                self._json_load_seconds = time.monotonic() - loading_started
                if context.mode != "OBJECT":
                    raise RuntimeError("Return to Object Mode and retry.")
                self._creator = create_objects_iter(
                    context, self._payload, self._source, self.use_normals,
                    self.origin_mode, self.share_meshes, self.source_up_axis,
                    self.use_step_colors, self.color_uncolored_parts,
                    self.follow_import_build,
                )
                self._creating_started = time.monotonic()
                self._phase = "creating"
                object_total = len(self._payload.get("instances") or self._payload["meshes"])
                self.update_progress(
                    f"Creating Blender objects: 0/{object_total}", 0,
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
                message = f"Worker exited with code {status}"
                for line in reversed(tail.splitlines()):
                    if line.startswith("STEP_RESULT "):
                        try:
                            packet = json.loads(line[len("STEP_RESULT "):])
                            message = packet.get("message") or message
                        except (ValueError, AttributeError):
                            pass
                        break
                raise RuntimeError(message)
            self._phase = "loading"
            self.update_progress("Loading converted mesh data...")
            return {"RUNNING_MODAL"}
        except Exception as error:
            log_path = self.cleanup(keep_log=True)
            print(f"SimpleStep import failed: {error}. Log: {log_path}")
            show_viewport_notice("SimpleStep import failed", str(error), f"Log: {log_path}")
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
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
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


