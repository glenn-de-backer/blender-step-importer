import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile

import usd_brep as sm


def run_tool(command, env=None):
    """Run a converter and stop if it fails."""
    subprocess.run(
        [str(argument) for argument in command],
        env=env,
        check=True,
        timeout=600,
    )


def convert(args):
    source = args.input.resolve(strict=True)
    output = args.output.resolve()

    reader = args.step_reader.resolve(strict=True)
    nvidia_root = args.nvidia_root.resolve(strict=True)
    nvidia_bin = nvidia_root / "_build/windows-x86_64/release"
    converter = nvidia_bin / "occt_to_usd.exe"
    schema = nvidia_root / "_build/schema/omniSolid/resources"

    if not converter.is_file():
        raise FileNotFoundError(f"Missing NVIDIA converter: {converter}")

    if not (schema / "plugInfo.json").is_file():
        raise FileNotFoundError(f"Missing NVIDIA schema: {schema}")

    if source == output:
        raise ValueError("Input and output paths must be different.")

    for name, value in (
        ("Chord tolerance", args.chord_mm),
        ("Angular tolerance", args.angle_deg),
    ):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and positive.")

    if args.angle_deg > 180:
        raise ValueError("Angular tolerance must not exceed 180 degrees.")

    output.parent.mkdir(parents=True, exist_ok=True)

    # Configure only the NVIDIA converter's child process.
    # Leave this Python process using the wheel's bundled runtime.
    converter_env = os.environ.copy()
    converter_env["OMNISOLID_PLUGIN_PATH"] = str(schema)

    runtime_dirs = [
        nvidia_bin,
        nvidia_bin / "extraLibs",
        nvidia_root / "_build/target-deps/usd/release/bin",
        nvidia_root / "_build/target-deps/usd/release/lib",
    ]

    converter_env["PATH"] = os.pathsep.join(
        [str(path) for path in runtime_dirs]
        + [converter_env.get("PATH", "")]
    )

    with tempfile.TemporaryDirectory(prefix="step_import_") as temporary:
        temporary = Path(temporary)
        brep_file = temporary / "model.brep"
        usd_file = temporary / "model.usdc"

        print("[1/3] Reading STEP...", flush=True)
        run_tool([reader, source, brep_file])

        print("[2/3] Converting OCCT BRep to USD BRep...", flush=True)
        run_tool(
            [converter, brep_file, usd_file],
            env=converter_env,
        )

        print("[3/3] Tessellating with NVIDIA...", flush=True)
        breps = sm.usd.import_breps(str(usd_file), heal=False)

        if not breps:
            raise RuntimeError("No BReps were imported.")

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
                raise RuntimeError(f"BRep {index} produced an empty mesh.")

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

        # Write beside the destination, then replace it atomically.
        # A failed conversion leaves any previous output untouched.
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
        description="Convert STEP to Blender mesh JSON using OCCT and NVIDIA."
    )

    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--step-reader", type=Path, required=True)
    parser.add_argument("--nvidia-root", type=Path, required=True)
    parser.add_argument("--chord-mm", type=float, default=0.1)
    parser.add_argument("--angle-deg", type=float, default=15.0)

    args = parser.parse_args()
    convert(args)


if __name__ == "__main__":
    main()