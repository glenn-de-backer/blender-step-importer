import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile

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


def tessellate_body(usd_file, name, args):
    """Convert one body's USD BReps into named mesh entries."""
    breps = sm.usd.import_breps(str(usd_file), heal=False)

    if not breps:
        raise RuntimeError(f"No NVIDIA BRep imported for {name}.")

    meshes = []

    for index, brep in enumerate(breps, start=1):
        mesh = sm.tessellate(
            brep,
            chord_height_tolerance=args.chord_mm,
            curve_angle_tolerance_deg=args.angle_deg,
            surface_angle_tolerance_deg=args.angle_deg,
        )

        arrays = mesh.to_mesh_arrays(face_varying_normals=True)

        if not arrays["points"] or not arrays["faces"]:
            raise RuntimeError(f"Body {name} produced an empty mesh.")

        mesh_name = name

        if len(breps) > 1:
            mesh_name = f"{name} [{index}]"

        meshes.append({
            "name": mesh_name,
            "points": arrays["points"],
            "faces": arrays["faces"],
            "normals": arrays["normals"],
        })

        print(
            f"  {mesh_name}: "
            f"{len(arrays['points'])} vertices, "
            f"{len(arrays['face_normals'])} triangles",
            flush=True,
        )

        # Finish using this mesh while its source BRep is still available.
        del mesh

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

        for index, part in enumerate(parts, start=1):
            brep_file = (
                parts_folder / part["file"]
            ).resolve(strict=True)

            if brep_file.parent != parts_folder.resolve():
                raise ValueError("Invalid body path in manifest.")

            name = part.get("name") or f"Body_{index:03d}"
            usd_file = folder / f"body_{index:06d}.usdc"

            print(
                f"[2/3] Converting body {index}/{len(parts)}: {name}",
                flush=True,
            )

            run_tool(
                [converter, brep_file, usd_file],
                env=environment,
            )

            print(f"[3/3] Tessellating: {name}", flush=True)

            meshes.extend(
                tessellate_body(usd_file, name, args)
            )

        payload = json.dumps(
            {
                "units": "mm",
                "source": str(source),
                "meshes": meshes,
            },
            allow_nan=False,
        )

        # Replace the output only after every body succeeds.
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

    print(f"Completed: {len(meshes)} mesh objects", flush=True)
    print(f"Saved: {output}", flush=True)


def main():
    parser = argparse.ArgumentParser(
        description="Convert named STEP bodies to mesh JSON."
    )

    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--chord-mm", type=float, default=0.1)
    parser.add_argument("--angle-deg", type=float, default=15.0)

    convert(parser.parse_args())


if __name__ == "__main__":
    main()