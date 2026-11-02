import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time
import sys
import shutil
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

# Write Unicode part names safely to the redirected worker log.
for stream in (sys.stdout, sys.stderr):
    if stream is not None and hasattr(stream, "reconfigure"):
        stream.reconfigure(
            encoding="utf-8",
            errors="backslashreplace",
        )

WORKER_VERSION = "1.5.2"

import usd_brep as sm


def run_tool(command, env=None):
    """Run a native converter and stop if it fails."""
    options = {}

    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW

    subprocess.run(
        [str(argument) for argument in command],
        env=env,
        check=True,
        timeout=600,
        **options,
    )


def publish_progress(args, stage, message):
    """Best-effort atomic status file; reporting must not fail conversion."""
    path = getattr(args, "progress_file", None)
    if path is None:
        return
    path = Path(path)
    pending = path.with_suffix(path.suffix + ".tmp")
    packet = {
        "stage": stage,
        "message": message,
        "body_index": getattr(args, "_body_index", 0),
        "body_total": getattr(args, "_body_total", 0),
        "completed": getattr(args, "_completed", 0),
        "active_workers": getattr(args, "_active_workers", 0),
        "body_name": getattr(args, "_body_name", ""),
    }
    try:
        pending.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
        pending.replace(path)
    except OSError:
        # Blender may briefly hold the file open on Windows. The next update
        # retries; conversion and its output remain independent of progress.
        try:
            pending.unlink(missing_ok=True)
        except OSError:
            pass


def error_details(error):
    """Copy diagnostics out of a native exception for JSON reporting."""
    return {
        "error": str(error),
        "status": getattr(error, "status", None),
        "status_name": getattr(error, "status_name", None),
        "report": getattr(error, "report", None),
        "failures": getattr(error, "failures", None),
    }


def preserve_failure(usd_file, name, index, args, error, initial, report):
    diagnostics = Path(tempfile.mkdtemp(prefix="step_tessellation_failure_"))
    shutil.copy2(usd_file, diagnostics / "failed.usdc")
    details = {
        "body_name": name,
        "brep_index": index,
        "chord_mm": args.chord_mm,
        "angle_deg": args.angle_deg,
        "repair_enabled": args.repair_failed,
        "allow_non_manifold_repair": getattr(args, "allow_non_manifold_repair", False),
        "initial_failure": initial,
        "retry_report": report,
        **error_details(error),
    }
    report_text = json.dumps(details, indent=2, ensure_ascii=False, default=str)
    (diagnostics / "report.json").write_text(report_text, encoding="utf-8")
    print(report_text, flush=True)
    print(f"Diagnostics saved to: {diagnostics}", flush=True)
    return diagnostics


def tessellate_body(usd_file, name, args):
    """Try strict tessellation; optionally heal a copy after a failure."""
    breps = sm.usd.import_breps(str(usd_file), heal=False)
    if not breps:
        raise RuntimeError(f"No NVIDIA BRep imported for {name}.")

    def tessellate(shape):
        return sm.tessellate(
            shape,
            chord_height_tolerance=args.chord_mm,
            curve_angle_tolerance_deg=args.angle_deg,
            surface_angle_tolerance_deg=args.angle_deg,
            return_report=True,
        )

    meshes = []
    for index, brep in enumerate(breps, start=1):
        repair = None
        initial = None
        retry_report = None
        working = brep
        try:
            try:
                mesh, report = tessellate(brep)
            except sm.TessellationError as original_error:
                initial = error_details(original_error)
                if not args.repair_failed:
                    raise

                publish_progress(args, "repairing", f"Repairing: {name}")
                print(f"Attempting repair on a copy: {name}", flush=True)
                working = brep.copy()
                faces_before = brep.face_count()
                sm.heal_brep(working)
                mesh, report = tessellate(working)
                retry_report = report

                # Closed inputs stay strict unless the user explicitly opts
                # into inspecting a non-manifold repaired mesh. Failed faces
                # are rejected in either mode below.
                original_report = initial.get("report") or {}
                non_manifold = report.get("output_manifold") is not True
                allow_non_manifold = getattr(args, "allow_non_manifold_repair", False)
                if (original_report.get("input_manifold") is True
                        and non_manifold and not allow_non_manifold):
                    del mesh
                    raise RuntimeError("Repair produced an open/non-manifold mesh.")

                repair = {
                    "faces_before": faces_before,
                    "faces_after": working.face_count(),
                    "initial_failure": initial,
                    "tessellation_report": report,
                    "cad_validation": "not_performed",
                    "accepted_non_manifold": non_manifold and allow_non_manifold,
                }
                if repair["accepted_non_manifold"]:
                    print(
                        f"WARNING: {name} accepted for visual inspection with "
                        "non-manifold connectivity. See the import report.",
                        flush=True,
                    )
                print(
                    f"WARNING: repaired {name}: {faces_before} -> "
                    f"{working.face_count()} CAD faces; "
                    "full CAD validity has not been established.",
                    flush=True,
                )

            if report.get("failures"):
                del mesh
                raise RuntimeError("Tessellation returned failed faces.")

            arrays = mesh.to_mesh_arrays(face_varying_normals=True)
            del mesh  # Release while both source and repaired BReps remain alive.
            if not arrays["points"] or not arrays["faces"]:
                raise RuntimeError(f"Body {name} produced an empty mesh.")
        except (MemoryError, OSError):
            raise
        except Exception as error:
            diagnostics = preserve_failure(
                usd_file, name, index, args, error, initial, retry_report,
            )
            raise RuntimeError(
                f"Tessellation failed for {name}. Diagnostics: {diagnostics}"
            ) from error

        mesh_name = name if len(breps) == 1 else f"{name} [{index}]"
        meshes.append({
            "name": mesh_name,
            "points": arrays["points"],
            "faces": arrays["faces"],
            "normals": arrays["normals"],
            "repair": repair,
        })
        print(
            f"  {mesh_name}: {len(arrays['points'])} vertices, "
            f"{len(arrays['face_normals'])} triangles",
            flush=True,
        )
    return meshes


def validate_mesh(item):
    """Reject invalid body data before it enters the combined result."""
    points, stream, normals = item["points"], item["faces"], item["normals"]
    def vector_ok(vector):
        return (len(vector) == 3 and all(
            type(v) in (int, float) and math.isfinite(v) for v in vector))
    if not points or not all(vector_ok(p) for p in points):
        raise ValueError("Invalid mesh coordinates")
    cursor = corners = 0
    while cursor < len(stream):
        count = stream[cursor]
        if type(count) is not int or count < 3:
            raise ValueError("Invalid polygon size")
        end = cursor + count + 1
        face = stream[cursor + 1:end]
        if end > len(stream) or any(type(i) is not int or i < 0 or i >= len(points) for i in face):
            raise ValueError("Invalid polygon indices")
        if len(set(face)) != count:
            raise ValueError("Repeated polygon vertices")
        corners += count
        cursor = end
    if not corners or len(normals) != corners:
        raise ValueError("Invalid corner-normal count")
    if not all(vector_ok(n) and 1e-10 <= math.hypot(*n) < math.inf for n in normals):
        raise ValueError("Invalid surface normals")
    json.dumps(item, allow_nan=False)


def best_effort_diagnostics(function):
    """Diagnostic storage must not replace the conversion error."""
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (OSError, ValueError, TypeError) as error:
            print(f"WARNING: could not save diagnostics: {error}", flush=True)
            return None
    return wrapped

preserve_failure = best_effort_diagnostics(preserve_failure)


def body_chord_mm(args, part):
    """Size-aware chord tolerance with a cap for rounded details."""
    if not getattr(args, "adaptive", False):
        return args.chord_mm
    percent = getattr(args, "adaptive_percent", 0.1)
    minimum = getattr(args, "adaptive_min_chord_mm", 0.01)
    maximum = getattr(args, "adaptive_max_chord_mm", 0.02)
    if (not all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0
                for v in (percent, minimum, maximum)) or minimum > maximum):
        raise ValueError("Invalid adaptive detail settings: minimum must not exceed maximum.")
    diagonal = part.get("diagonal_mm")
    if (type(diagonal) not in (int, float) or not math.isfinite(diagonal)
            or diagonal <= 0):
        raise ValueError(
            "Adaptive needs diagonal_mm for each body; update step-to-brep "
            "to 1.2.0 and rebuild the bundled STEP reader."
        )
    return min(maximum, max(minimum, diagonal * percent / 100.0))


def validate_assembly_manifest(manifest):
    """Accept the old flat format and a preorder tree of assembly occurrences."""
    version = manifest.get("version")
    if version not in (2, 3, 4):
        raise ValueError("Expected body manifest version 2, 3 or 4.")
    assemblies = manifest.get("assemblies", []) if version >= 3 else []
    if not isinstance(assemblies, list):
        raise ValueError("Invalid assembly list.")
    known = {0}
    for node in assemblies:
        if not isinstance(node, dict):
            raise ValueError("Invalid assembly node.")
        node_id, parent_id = node.get("id"), node.get("parent_id")
        if (type(node_id) is not int or node_id <= 0 or node_id in known
                or type(parent_id) is not int or parent_id not in known
                or not isinstance(node.get("name"), str) or not node["name"]):
            raise ValueError("Invalid assembly hierarchy or name.")
        known.add(node_id)
    for part in manifest.get("parts", []):
        if type(part.get("assembly_id", 0)) is not int or part.get("assembly_id", 0) not in known:
            raise ValueError("Part references an unknown assembly.")
    if version == 4:
        prototypes = manifest.get("prototypes")
        if not isinstance(prototypes, list) or not prototypes:
            raise ValueError("Missing prototype definitions.")
        ids = set()
        for prototype in prototypes:
            identifier = prototype.get("id")
            if (type(identifier) is not int or identifier <= 0 or identifier in ids
                    or not isinstance(prototype.get("file"), str)):
                raise ValueError("Invalid prototype definition.")
            ids.add(identifier)
        for part in manifest.get("parts", []):
            values = part.get("transform")
            if (part.get("prototype_id") not in ids
                    or not isinstance(part.get("name"), str)
                    or not isinstance(values, list) or len(values) != 16
                    or any(type(v) not in (float, int) or not math.isfinite(v) for v in values)
                    or values[12:] != [0, 0, 0, 1]):
                raise ValueError("Invalid prototype instance or transform.")
            color = part.get("color")
            if color is not None and (
                not isinstance(color, list) or len(color) != 4
                or any(type(v) not in (float, int) or not math.isfinite(v)
                       or v < 0 or v > 1 for v in color)
            ):
                raise ValueError("Invalid STEP part color.")
    return assemblies


def occt_fallback_attempts(chord_mm, angle_deg):
    """Explore both directions relative to this body's requested tolerance."""
    return [
        (chord_mm, angle_deg, None),
        (chord_mm * 3.0, min(180.0, angle_deg * 2.0), "coarser"),
        (chord_mm * 0.5, angle_deg * 0.75, "finer"),
    ]


def mesh_with_occt_retry(reader, brep_file, fallback_file, name,
                         nvidia_error, chord_mm, angle_deg):
    """Only accept a complete validated mesh; record the successful tolerance."""
    errors = []
    for trial_chord, trial_angle, direction in occt_fallback_attempts(chord_mm, angle_deg):
        try:
            # An earlier failed converter could have left a partial JSON file.
            fallback_file.unlink(missing_ok=True)
            if direction:
                print(f"Retrying OCCT with {direction} tolerance: {name} "
                      f"({trial_chord:g} mm, {trial_angle:g} degrees)", flush=True)
            run_tool([reader, "--mesh", brep_file, fallback_file,
                      str(trial_chord), str(trial_angle)])
            with fallback_file.open(encoding="utf-8") as file:
                fallback = json.load(file)
            if (fallback.get("version") != 1 or fallback.get("units") != "mm"
                    or fallback.get("mesher") != "occt"
                    or fallback.get("missing_faces") != 0
                    or fallback.get("status_flags") != 0
                    or not fallback.get("face_count")):
                raise ValueError("OCCT returned an incomplete or incompatible mesh")
            item = {
                "name": name, "points": fallback["points"],
                "faces": fallback["faces"], "normals": fallback["normals"],
                "repair": None,
                "fallback": {
                    "mesher": "occt", "nvidia_error": nvidia_error,
                    "chord_mm": fallback.get("chord_mm"),
                    "angle_deg": fallback.get("angle_deg"),
                    "requested_chord_mm": chord_mm,
                    "requested_angle_deg": angle_deg,
                    "retry_direction": direction,
                    "coarser_retry": direction == "coarser",
                    "finer_retry": direction == "finer",
                    "face_count": fallback["face_count"],
                    "degenerate_triangles_skipped": fallback.get("degenerate_triangles_skipped", 0),
                    "normals": "flat triangle normals",
                    "nurbs_face_count": fallback.get("nurbs_face_count", 0),
                    "original_status_flags": fallback.get("original_status_flags", 0),
                    "seam_continuity_verified": False,
                },
            }
            validate_mesh(item)
            return item
        except (RuntimeError, ValueError, TypeError, KeyError, OSError,
                subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            errors.append(f"{trial_chord:g} mm/{trial_angle:g} degrees: {error}")
    raise RuntimeError("; ".join(errors))


def schedule_part_indices(parts, parts_folder):
    """Start likely expensive bodies early; keep result indices in STEP order."""
    root = parts_folder.resolve()
    sizes = []
    for index, part in enumerate(parts, 1):
        path = (root / part["file"]).resolve(strict=True)
        if path.parent != root:
            raise ValueError("Invalid body path in manifest.")
        sizes.append((index, path.stat().st_size))
    return [index for index, _ in sorted(sizes, key=lambda item: (-item[1], item[0]))]


def convert_parts_parallel(args, parts, parts_folder, folder, reader, output):
    """Run bounded isolated Python workers; return results in STEP order."""
    count = len(parts)
    work_order = schedule_part_indices(parts, parts_folder)
    results = [None] * count
    args._body_total = count
    args._completed = 0
    args._phase_seconds = {"usd_conversion": 0.0, "nvidia_tessellation": 0.0,
                           "occt_fallback": 0.0}
    print(f"Processing {count} bodies with {args.jobs} separate workers.", flush=True)

    def one_body(index, part):
        brep_file = (parts_folder / part["file"]).resolve(strict=True)
        if brep_file.parent != parts_folder.resolve():
            raise ValueError("Invalid body path in manifest.")
        name = part.get("name") or f"Body_{index:03d}"
        chord = body_chord_mm(args, part)
        result_path = folder / f"result_{index:06d}.json"
        log_path = output.parent / f"job_{index:06d}.log"
        command = [
            sys.executable, "-u", str(Path(__file__).resolve()),
            str(brep_file), str(result_path), "--body-job", "--body-name", name,
            "--chord-mm", str(chord), "--angle-deg", str(args.angle_deg),
            "--jobs", "1", "--skip-failed",
        ]
        if args.repair_failed:
            command.append("--repair-failed")
        if args.allow_non_manifold_repair:
            command.append("--allow-non-manifold-repair")
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        with log_path.open("w", encoding="utf-8") as log:
            try:
                result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                        check=False, timeout=1200, **options)
            except (OSError, subprocess.TimeoutExpired) as error:
                return index, name, None, f"Worker failed: {error}", log_path
        if result.returncode == 0:
            try:
                with result_path.open(encoding="utf-8") as file:
                    data = json.load(file)
                if data.get("units") != "mm" or not data.get("meshes"):
                    raise ValueError("Body worker returned no mesh")
                return index, name, data, None, log_path
            except (OSError, ValueError) as error:
                return index, name, None, f"Invalid body result: {error}", log_path
        failures_path = result_path.with_suffix(".failures.json")
        if failures_path.is_file():
            try:
                with failures_path.open(encoding="utf-8") as file:
                    failures = json.load(file).get("failed_parts", [])
                if failures:
                    return index, name, None, failures[0].get("error", "Body failed"), log_path
            except (OSError, ValueError):
                pass
        try:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-1500:]
        except OSError:
            tail = ""
        return index, name, None, f"Worker exit {result.returncode}: {tail}", log_path

    # Submit only bounded work. A strict failure cannot leave 214 jobs queued.
    pool = ThreadPoolExecutor(max_workers=args.jobs)
    futures = {}
    next_position = 0
    last_completed = time.monotonic()
    try:
        while next_position < count and len(futures) < args.jobs:
            index = work_order[next_position]
            future = pool.submit(one_body, index, parts[index - 1])
            futures[future] = index
            next_position += 1
        while futures:
            # Keep the viewport alive while a large body occupies every worker.
            done, _ = wait(futures, timeout=1.0, return_when=FIRST_COMPLETED)
            if not done:
                indices = ", ".join(str(i) for i in sorted(futures.values()))
                idle_seconds = int(time.monotonic() - last_completed)
                args._active_workers = len(futures)
                publish_progress(
                    args, "working",
                    f"Working on bodies {indices} ({len(futures)} active); "
                    f"last completion {idle_seconds // 60:02d}:{idle_seconds % 60:02d} ago",
                )
                continue
            for future in done:
                index = futures.pop(future)
                try:
                    part_index, name, data, error, log_path = future.result()
                except Exception as error:
                    # Missing paths or malformed manifest are fatal, not skips.
                    raise RuntimeError(f"Body {index} setup failed: {error}") from error
                if data is None and not args.skip_failed:
                    raise RuntimeError(f"Body {part_index}/{count} ({name}) failed: {error}. Log: {log_path}")
                if data is None:
                    print(f"WARNING: skipped body {part_index}/{count}: {name}: {error}. Log: {log_path}", flush=True)
                    # The failed child keeps the original BRep in its diagnostics
                    # when conversion, tessellation and OCCT meshing all fail.
                    diagnostics = None
                    failures_path = (folder / f"result_{part_index:06d}.json").with_suffix(".failures.json")
                    if failures_path.is_file():
                        try:
                            failures = json.loads(failures_path.read_text(encoding="utf-8"))["failed_parts"]
                            diagnostics = failures[0].get("diagnostics")
                        except (OSError, ValueError, KeyError, IndexError):
                            pass
                    results[part_index - 1] = ([], [{"part_index": part_index,
                        "name": name, "assembly_id": parts[part_index - 1].get("assembly_id", 0),
                        "error": error, "diagnostics": diagnostics,
                        "worker_log": str(log_path)}])
                else:
                    meshes = data["meshes"]
                    for item in meshes:
                        item["assembly_id"] = parts[part_index - 1].get("assembly_id", 0)
                        if "id" in parts[part_index - 1]:
                            item["prototype_id"] = parts[part_index - 1]["id"]
                    for phase in args._phase_seconds:
                        args._phase_seconds[phase] += data.get("phase_seconds", {}).get(phase, 0.0)
                    failures = data.get("failed_parts", [])
                    for failure in failures:
                        failure["part_index"] = part_index
                        failure["assembly_id"] = parts[part_index - 1].get("assembly_id", 0)
                    results[part_index - 1] = (meshes, failures)
                    recovered = sum(bool(mesh.get("fallback")) for mesh in meshes)
                    print(f"Body {part_index}/{count}: {name}: {len(meshes)} mesh(es), "
                          f"{recovered} OCCT fallback(s). Log: {log_path}", flush=True)
                args._completed += 1
                last_completed = time.monotonic()
                args._body_index = part_index
                args._body_name = name
                publish_progress(args, "processed", f"Processed: {name}")
                if next_position < count:
                    next_index = work_order[next_position]
                    pending = pool.submit(one_body, next_index, parts[next_index - 1])
                    futures[pending] = next_index
                    next_position += 1
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    meshes = []
    failed_parts = []
    for body_meshes, failures in results:
        meshes.extend(body_meshes)
        failed_parts.extend(failures)
    return meshes, failed_parts


def convert(args):
    started = time.perf_counter()
    phase_seconds = {"usd_conversion": 0.0, "nvidia_tessellation": 0.0,
                     "occt_fallback": 0.0}
    split_seconds = 0.0
    if os.name != "nt":
        raise RuntimeError("This runtime package supports Windows x64.")

    source = args.input.resolve(strict=True)
    output = args.output.resolve()

    # Discover all tools relative to the installed add-on.
    runtime = (
        Path(__file__).resolve().parent
        / "runtime"
        / "windows-x64"
    )

    reader = runtime / "step-reader" / "step-to-brep.exe"
    nvidia = runtime / "nvidia"
    converter = nvidia / "occt_to_usd.exe"
    schema = nvidia / "omniSolid" / "resources"

    for path in (reader, converter, schema / "plugInfo.json"):
        if not path.is_file():
            raise FileNotFoundError(
                f"Missing bundled runtime file: {path}"
            )

    if source == output:
        raise ValueError("Input and output paths must be different.")

    for value in (args.chord_mm, args.angle_deg):
        if not math.isfinite(value) or value <= 0:
            raise ValueError("Tolerances must be finite and positive.")

    if args.angle_deg > 180:
        raise ValueError("Angular tolerance must not exceed 180 degrees.")

    output.parent.mkdir(parents=True, exist_ok=True)

    # These settings apply only to the NVIDIA converter subprocess.
    environment = os.environ.copy()
    environment["OMNISOLID_PLUGIN_PATH"] = str(schema)
    environment["PXR_PLUGINPATH_NAME"] = str(nvidia / "plugin")
    environment["PATH"] = os.pathsep.join([
        str(nvidia), str(nvidia / "extraLibs"), environment.get("PATH", "")
    ])

    with tempfile.TemporaryDirectory(prefix="step_import_") as folder:
        folder = Path(folder)
        occurrences = None
        if getattr(args, "body_job", False):
            parts_folder = source.parent
            parts = [{"file": source.name, "name": args.body_name or source.stem}]
        else:
            parts_folder = folder / "parts"

            publish_progress(args, "reading", "Reading STEP and extracting named bodies...")
            print("[1/3] Reading named STEP bodies...", flush=True)

            # Split mode exports individual BReps and a name manifest.
            split_started = time.perf_counter()
            run_tool([
                reader,
                "--split",
                source,
                parts_folder,
            ])
            split_seconds = time.perf_counter() - split_started

            with (parts_folder / "manifest.json").open(
                encoding="utf-8"
            ) as file:
                manifest = json.load(file)

            assemblies = validate_assembly_manifest(manifest)
            if manifest["version"] == 2:
                print("WARNING: STEP reader returned flat manifest v2; "
                      "install step-to-brep 1.3.0 to preserve assembly hierarchy.",
                      flush=True)

            if manifest.get("units") != "mm":
                raise ValueError("Expected millimeter geometry.")

            occurrences = manifest.get("parts", [])
            parts = manifest["prototypes"] if manifest["version"] == 4 else occurrences
            if manifest["version"] == 4:
                parts = [{**p, "name": f"Prototype_{p['id']}"} for p in parts]

            if not parts:
                raise RuntimeError("The STEP reader exported no bodies.")

            if getattr(args, "adaptive", False):
                # Fail before launching workers when the bundled reader is old.
                for part in parts:
                    body_chord_mm(args, part)

            print(f"STEP bodies: {len(occurrences)}; unique prototypes: {len(parts)}", flush=True)

        processing_started = time.perf_counter()
        if not getattr(args, "body_job", False) and getattr(args, "jobs", 1) > 1:
            meshes, failed_parts = convert_parts_parallel(
                args, parts, parts_folder, folder, reader, output)
            phase_seconds = args._phase_seconds
        else:
            meshes = []
            failed_parts = []
            args._body_total = len(parts)
            args._completed = 0

            for index, part in enumerate(parts, start=1):
                brep_file = (
                    parts_folder / part["file"]
                ).resolve(strict=True)

                if brep_file.parent != parts_folder.resolve():
                    raise ValueError("Invalid body path in manifest.")

                name = part.get("name") or f"Body_{index:03d}"
                body_args = argparse.Namespace(**vars(args))
                body_args.chord_mm = body_chord_mm(args, part)
                usd_file = folder / f"body_{index:06d}.usdc"
                args._body_index = index
                args._body_name = name
                publish_progress(args, "converting", f"Converting: {name}")

                print(
                    f"[2/3] Converting body {index}/{len(parts)}: {name}",
                    flush=True,
                )

                try:
                    conversion_started = time.perf_counter()
                    try:
                        run_tool(
                            [converter, brep_file, usd_file],
                            env=environment,
                        )
                    finally:
                        phase_seconds["usd_conversion"] += time.perf_counter() - conversion_started
                    publish_progress(args, "tessellating", f"Tessellating: {name}")
                    print(f"[3/3] Tessellating: {name}", flush=True)
                    tessellation_started = time.perf_counter()
                    try:
                        body_meshes = tessellate_body(usd_file, name, body_args)
                    finally:
                        phase_seconds["nvidia_tessellation"] += time.perf_counter() - tessellation_started
                    if not body_meshes:
                        raise ValueError("Body produced no meshes")
                    for item in body_meshes:
                        validate_mesh(item)
                        item["assembly_id"] = part.get("assembly_id", 0)
                        if "id" in part:
                            item["prototype_id"] = part["id"]
                    meshes.extend(body_meshes)
                except (RuntimeError, ValueError, TypeError, subprocess.CalledProcessError,
                        subprocess.TimeoutExpired) as error:
                    # Recover from either USD conversion or NVIDIA tessellation
                    # failure using the original OCCT BRep, never the healed USD.
                    original_error = str(error)
                    fallback_file = folder / f"occt_{index:06d}.mesh.json"
                    publish_progress(args, "fallback", f"OCCT meshing: {name}")
                    print(f"Trying OCCT fallback: {name}", flush=True)
                    fallback_started = time.perf_counter()
                    try:
                        item = mesh_with_occt_retry(
                            reader, brep_file, fallback_file, name,
                            original_error, body_args.chord_mm, body_args.angle_deg,
                        )
                        item["assembly_id"] = part.get("assembly_id", 0)
                        if "id" in part:
                            item["prototype_id"] = part["id"]
                        meshes.append(item)
                        args._completed = index
                        publish_progress(args, "processed", f"Imported with OCCT: {name}")
                        print(f"OCCT fallback succeeded: {name}", flush=True)
                        continue
                    except RuntimeError as fallback_error:
                        error = RuntimeError(
                            f"{name}: NVIDIA failed: {original_error}; "
                            f"OCCT fallback failed: {fallback_error}")
                    finally:
                        phase_seconds["occt_fallback"] += time.perf_counter() - fallback_started
                    if not getattr(args, "skip_failed", False):
                        raise error
                    # Keep source geometry for skipped parts, even though this
                    # import may succeed and remove its normal temporary files.
                    failure = {"part_index": index, "name": name,
                               "assembly_id": part.get("assembly_id", 0),
                               "error": str(error), "diagnostics": None}
                    try:
                        diagnostics = Path(tempfile.mkdtemp(prefix="step_skipped_part_"))
                        failure["diagnostics"] = str(diagnostics)
                        shutil.copy2(brep_file, diagnostics / "source.brep")
                        if usd_file.is_file():
                            shutil.copy2(usd_file, diagnostics / "converted.usdc")
                        (diagnostics / "report.json").write_text(
                            json.dumps(failure, ensure_ascii=False, indent=2), encoding="utf-8")
                    except OSError as diagnostic_error:
                        print(f"WARNING: could not save skipped-body diagnostics: {diagnostic_error}", flush=True)
                    diagnostics = failure["diagnostics"] or "unavailable"
                    failed_parts.append(failure)
                    print(
                        f"WARNING: skipped body {index}/{len(parts)}: {name}. "
                        f"Diagnostics: {diagnostics}", flush=True,
                    )
                    publish_progress(args, "skipped", f"Skipped: {name}")

                args._completed = index
                publish_progress(args, "processed", f"Processed: {name}")

        processing_seconds = time.perf_counter() - processing_started

        if occurrences is not None and manifest["version"] == 4:
            successful = {item["prototype_id"] for item in meshes}
            missing = {part["id"] for part in parts} - successful
            failures_by_id = {parts[f["part_index"] - 1]["id"]: f for f in failed_parts}
            failed_parts = [
                {"part_index": index, "name": part["name"],
                 "assembly_id": part["assembly_id"],
                 "prototype_id": part["prototype_id"],
                 "error": failures_by_id.get(part["prototype_id"], {}).get("error", "Prototype unavailable"),
                 "diagnostics": failures_by_id.get(part["prototype_id"], {}).get("diagnostics")}
                for index, part in enumerate(occurrences, 1)
                if part["prototype_id"] in missing
            ]

        if not meshes:
            failures_file = output.with_suffix(".failures.json")
            failures_file.write_text(
                json.dumps({"source": str(source), "failed_parts": failed_parts},
                           indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            raise RuntimeError(
                f"No bodies could be imported; {len(failed_parts)} failed. "
                f"Failure report: {failures_file}"
            )

        publish_progress(args, "writing", "Writing converted mesh data...")
        writing_started = time.perf_counter()
        # Embed these measurements in the successful result so Blender can
        # report them even if the separate log's final line is unavailable.
        timing_summary = {
            "step_split": round(split_seconds, 3),
            "body_processing_wall": round(processing_seconds, 3),
            "usd_conversion_sum": round(phase_seconds["usd_conversion"], 3),
            "nvidia_tessellation_sum": round(phase_seconds["nvidia_tessellation"], 3),
            "occt_fallback_sum": round(phase_seconds["occt_fallback"], 3),
            "worker_before_write_wall": round(writing_started - started, 3),
            "jobs": args.jobs,
            "bodies": len(occurrences) if occurrences is not None else len(parts),
            "prototypes": len(parts),
        }
        payload = json.dumps(
            {
                "units": "mm",
                "source": str(source),
                "meshes": meshes,
                "repair_enabled": args.repair_failed,
                "failed_parts": failed_parts,
                "source_body_count": len(occurrences) if occurrences is not None else len(parts),
                "source_prototype_count": len(parts),
                "instances": occurrences if occurrences is not None and manifest["version"] == 4 else [],
                "partial_import": bool(failed_parts),
                "assemblies": assemblies if not getattr(args, "body_job", False) else [],
                "manifest_version": 2 if getattr(args, "body_job", False) else manifest["version"],
                "phase_seconds": phase_seconds,
                "timings": timing_summary,
            },
            allow_nan=False,
        )

        # Publish successful meshes and an explicit list of any skipped bodies.
        pending = None

        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=output.parent,
                suffix=".tmp",
                delete=False,
            ) as file:
                pending = Path(file.name)
                file.write(payload)

            pending.replace(output)

        finally:
            if pending is not None:
                pending.unlink(missing_ok=True)

        writing_seconds = time.perf_counter() - writing_started

    publish_progress(args, "done", "Conversion complete")
    print("STEP_TIMING " + json.dumps({
        **timing_summary,
        "json_write": round(writing_seconds, 3),
        "total_worker_wall": round(time.perf_counter() - started, 3),
    }), flush=True)
    print(f"Completed: {len(meshes)} unique mesh definitions; "
          f"{len(failed_parts)} source occurrences skipped", flush=True)
    print(f"Saved: {output}", flush=True)


def main():
    parser = argparse.ArgumentParser(
        description="Convert named STEP bodies to mesh JSON."
    )

    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--chord-mm", type=float, default=0.1)
    parser.add_argument("--angle-deg", type=float, default=15.0)
    parser.add_argument("--adaptive", action="store_true",
                        help="Set each body's chord from its diagonal and adaptive detail controls.")
    parser.add_argument("--adaptive-percent", type=float, default=0.1,
                        help="Chord as percentage of the body's diagonal (default: 0.1).")
    parser.add_argument("--adaptive-min-chord-mm", type=float, default=0.01)
    parser.add_argument("--adaptive-max-chord-mm", type=float, default=0.02)

    parser.add_argument(
        "--repair-failed", action="store_true",
        help="Heal copies of bodies that fail tessellation; changes are reported.",
    )

    parser.add_argument(
        "--allow-non-manifold-repair", action="store_true",
        help="Accept non-manifold repaired meshes for inspection; failed faces still fail.",
    )
    parser.add_argument(
        "--skip-failed", action="store_true",
        help="Continue after body conversion failures and report every skipped body.",
    )
    parser.add_argument("--progress-file", type=Path, help="Live status JSON for Blender.")
    parser.add_argument("--jobs", type=int, default=4,
                        help="Concurrent isolated body workers (1 to 8).")
    parser.add_argument("--body-job", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--body-name", default="", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.jobs <= 8:
        parser.error("--jobs must be between 1 and 8")
    print(f"STEP worker {WORKER_VERSION}: {Path(__file__).resolve()}", flush=True)
    print(f"Options: repair={args.repair_failed}, allow_non_manifold={args.allow_non_manifold_repair}, skip={args.skip_failed}, jobs={args.jobs}", flush=True)
    try:
        convert(args)
    except Exception as error:
        packet = {"version": WORKER_VERSION, "status": "failed",
                  "message": str(error), "error_type": type(error).__name__}
        print("STEP_RESULT " + json.dumps(packet, ensure_ascii=False), flush=True)
        publish_progress(args, "failed", "Conversion failed; see worker log")
        raise


if __name__ == "__main__":
    main()
