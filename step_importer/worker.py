import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile

import usd_brep as sm


def run_tool(command, env=None):
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


def convert(args):
    if os.name != "nt":
        raise RuntimeError(
            "This runtime package supports Windows x64."
        )

    source = args.input.resolve(strict=True)
    output = args.output.resolve()

    # Find bundled tools relative to this script.
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
        raise ValueError(
            "Input and output paths must be different."
        )

    for value in (args.chord_mm, args.angle_deg):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(
                "Tolerances must be finite and positive."
            )

    if args.angle_deg > 180:
        raise ValueError(
            "Angular tolerance must not exceed 180 degrees."
        )

    output.parent.mkdir(parents=True, exist_ok=True)

    # These settings apply only to the NVIDIA converter.
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
        brep_file = folder / "model.brep"
        usd_file = folder / "model.usdc"

        print("[1/3] Reading STEP...", flush=True)
        run_tool([reader, source, brep_file])

        print(
            "[2/3] Converting OCCT BRep to USD BRep...",
            flush=True,
        )
        run_tool(
            [converter, brep_file, usd_file],
            env=environment,
        )

        print("[3/3] Tessellating with NVIDIA...", flush=True)

        breps = sm.usd.import_breps(str(usd_file), heal=False)

        if not breps:
            raise RuntimeError("No BReps were imported.")

        meshes = []

        for index, brep in enumerate(breps, 1):
            mesh = sm.tessellate(
                brep,
                chord_height_tolerance=args.chord_mm,
                curve_angle_tolerance_deg=args.angle_deg,
                surface_angle_tolerance_deg=args.angle_deg,
            )

            arrays = mesh.to_mesh_arrays(
                face_varying_normals=True
            )

            if not arrays["points"] or not arrays["faces"]:
                raise RuntimeError(
                    f"BRep {index} produced an empty mesh."
                )

            meshes.append({
                "name": f"{source.stem}_{index:03d}",
                "points": arrays["points"],
                "faces": arrays["faces"],
                "normals": arrays["normals"],
            })

            print(
                f"  BRep {index}: "
                f"{len(arrays['points'])} vertices, "
                f"{len(arrays['face_normals'])} triangles",
                flush=True,
            )

        payload = json.dumps(
            {
                "units": "mm",
                "source": str(source),
                "meshes": meshes,
            },
            allow_nan=False,
        )

        # Replace output only after successful conversion.
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

    print(f"Saved: {output}", flush=True)


def main():
    parser = argparse.ArgumentParser(
        description="Convert STEP to mesh JSON."
    )

    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--chord-mm", type=float, default=0.1)
    parser.add_argument("--angle-deg", type=float, default=15.0)

    convert(parser.parse_args())


if __name__ == "__main__":
    main()