import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import sys
import shutil

# Write Unicode part names safely to the redirected worker log.
for stream in (sys.stdout, sys.stderr):
    if stream is not None and hasattr(stream, "reconfigure"):
        stream.reconfigure(
            encoding="utf-8",
            errors="backslashreplace",
        )

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


def convert(args):
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
        str(nvidia),
        str(nvidia / "extraLibs"),
        environment.get("PATH", ""),
    ])

    with tempfile.TemporaryDirectory(prefix="step_import_") as folder:
        folder = Path(folder)
        parts_folder = folder / "parts"

        publish_progress(args, "reading", "Reading STEP and extracting named bodies...")
        print("[1/3] Reading named STEP bodies...", flush=True)

        # Split mode exports individual BReps and a name manifest.
        run_tool([
            reader,
            "--split",
            source,
            parts_folder,
        ])

        with (parts_folder / "manifest.json").open(
            encoding="utf-8"
        ) as file:
            manifest = json.load(file)

        if manifest.get("version") != 2:
            raise ValueError("Expected body manifest version 2.")

        if manifest.get("units") != "mm":
            raise ValueError("Expected millimeter geometry.")

        parts = manifest.get("parts", [])

        if not parts:
            raise RuntimeError("The STEP reader exported no bodies.")

        print(f"STEP bodies: {len(parts)}", flush=True)

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
            usd_file = folder / f"body_{index:06d}.usdc"
            args._body_index = index
            args._body_name = name
            publish_progress(args, "converting", f"Converting: {name}")

            print(
                f"[2/3] Converting body {index}/{len(parts)}: {name}",
                flush=True,
            )

            try:
                run_tool(
                    [converter, brep_file, usd_file],
                    env=environment,
                )
                publish_progress(args, "tessellating", f"Tessellating: {name}")
                print(f"[3/3] Tessellating: {name}", flush=True)
                meshes.extend(tessellate_body(usd_file, name, args))
            except (RuntimeError, subprocess.CalledProcessError,
                    subprocess.TimeoutExpired) as error:
                if not getattr(args, "skip_failed", False):
                    raise
                # Keep source geometry for skipped parts, even though this
                # import may succeed and remove its normal temporary files.
                diagnostics = Path(tempfile.mkdtemp(prefix="step_skipped_part_"))
                shutil.copy2(brep_file, diagnostics / "source.brep")
                if usd_file.is_file():
                    shutil.copy2(usd_file, diagnostics / "converted.usdc")
                failure = {
                    "part_index": index,
                    "name": name,
                    "error": str(error),
                    "diagnostics": str(diagnostics),
                }
                (diagnostics / "report.json").write_text(
                    json.dumps(failure, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                failed_parts.append(failure)
                print(
                    f"WARNING: skipped body {index}/{len(parts)}: {name}. "
                    f"Diagnostics: {diagnostics}", flush=True,
                )
                publish_progress(args, "skipped", f"Skipped: {name}")

            args._completed = index
            publish_progress(args, "processed", f"Processed: {name}")

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
        payload = json.dumps(
            {
                "units": "mm",
                "source": str(source),
                "meshes": meshes,
                "repair_enabled": args.repair_failed,
                "failed_parts": failed_parts,
                "source_body_count": len(parts),
                "partial_import": bool(failed_parts),
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

    publish_progress(args, "done", "Conversion complete")
    print(f"Completed: {len(meshes)} mesh objects; {len(failed_parts)} bodies skipped", flush=True)
    print(f"Saved: {output}", flush=True)


def main():
    parser = argparse.ArgumentParser(
        description="Convert named STEP bodies to mesh JSON."
    )

    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--chord-mm", type=float, default=0.1)
    parser.add_argument("--angle-deg", type=float, default=15.0)

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
    args = parser.parse_args()
    try:
        convert(args)
    except Exception:
        publish_progress(args, "failed", "Conversion failed; see worker log")
        raise


if __name__ == "__main__":
    main()