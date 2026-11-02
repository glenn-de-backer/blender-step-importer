# SimpleStep importer

Import STEP (`.step`, `.stp`) CAD assemblies into Blender as meshes. SimpleStep preserves part names, assembly collections, repeated part placements, and available STEP part colors.

## Requirements

- Blender 5.2 or newer on Windows x64.
- The **Windows x64 release ZIP**, which includes the STEP reader, NVIDIA USD BRep runtime, and a separate Python runtime. Blender's Python does not need extra packages.

The source folder alone is not a complete install: it needs the bundled `step_importer/runtime/windows-x64` files. Linux and macOS packages are not available yet.

## Install

1. Download `SimpleStep-importer-<version>-windows-x64.zip` from the GitHub release.
2. In Blender, open **Edit → Preferences → Add-ons**, choose **Install from Disk**, and select the ZIP. Enable **SimpleStep importer**.
3. Restart Blender after upgrading an existing installation so it loads the new Python files.

The ZIP must contain the top-level `step_importer` folder, including its `runtime` directory. Do not unpack and repackage only the Python files.

## Import a model

Choose **File → Import → SimpleStep importer (.step/.stp)** and select the STEP file. Progress appears in the viewport while the converter runs. When it finishes, the mesh objects and assembly collections appear in the scene.

Useful options:

| Option | What it does |
| --- | --- |
| Mesh quality | Choose a preset or **Adaptive** to set detail per part. Smaller chord tolerance produces smoother rounded features but takes longer and uses more memory. |
| Source up axis | Rotate the imported assembly so the STEP file's chosen up direction becomes Blender's Z axis. |
| Parallel bodies | Process up to eight distinct parts concurrently; the default is four. |
| Share repeated mesh data | Reuse one Blender mesh for repeated parts while preserving their separate locations. |
| Import STEP colors | Create materials from available part colors. **Color parts without STEP colors** assigns optional colors where none exist. |
| Repair failed bodies | Retry failed NVIDIA tessellation after healing a copy of the CAD geometry. |
| Skip failed parts | Continue with other parts and list any missing geometry in the import report. |
| Follow model while importing | Optionally frame the model as Blender creates objects. |

For detailed results and timings, open **SimpleStep Import Report** and **SimpleStep Timing Report** in Blender's Text Editor. Skipped parts are missing from the imported model; repaired or non-manifold meshes should be checked before uses that require valid solids.

## How it works

The bundled [step-to-brep](https://github.com/glenn-de-backer/step-to-brep) reader extracts named OCCT BRep parts. NVIDIA's [USD BRep tools](https://github.com/NVIDIA-Omniverse/usd-brep) convert and tessellate those parts. An OCCT mesh fallback handles some conversion or tessellation failures. SimpleStep then creates Blender meshes, collections, materials, and repeated instances. The Blender result is a mesh representation, not editable CAD surfaces.

## Build the Windows package

From a Windows PowerShell prompt in the project root, after building and installing `step-to-brep` and staging the NVIDIA runtime:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\package_runtime.ps1"
New-Item -ItemType Directory -Force ".\dist" | Out-Null
Compress-Archive -Path ".\step_importer" -DestinationPath ".\dist\SimpleStep-importer-windows-x64.zip" -Force
```

Install and test that ZIP in Blender before publishing it. Keep bundled dependency license notices with the runtime.
