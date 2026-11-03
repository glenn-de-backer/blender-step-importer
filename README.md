 
# SimpleStep Importer

Import STEP (`.step`, `.stp`) CAD files into Blender as clean 3D meshes. SimpleStep keeps your part names, assembly hierarchy, repeated parts (instances), and CAD colors without freezing Blender.

## Origin & Motivation

This tool was created out of a practical challenge: handling massive, complex STEP assemblies that typically make Blender freeze or stop responding. While teaching Blender in higher education, students frequently ran into imports that completely locked the interface, leading them to believe Blender had crashed and force-quitting their work. SimpleStep was designed from the ground up to eliminate that friction by keeping the software completely usable during heavy CAD imports.

## Features

- **No Frozen UI:** Heavy CAD conversion runs in a separate C++ background program. Blender stays responsive, so you can keep working or navigating scenes while your file imports.
- **Built for Massive Assemblies:** Designed specifically to handle large, complex STEP files that normally choke traditional importer workflows.
- **Multi-Core Speed:** Uses multiple CPU processes to convert distinct parts concurrently, significantly reducing wait times on multi-core workstations.
- **Adaptive Meshing:** Detail automatically adjusts to the size of each part. Tiny parts like screws and nuts do not get too many polygons, keeping your viewport fast.
- **Reliable Fallback (NVIDIA + OCCT):** Uses NVIDIA USD BRep for fast meshing. If a difficult surface fails, it automatically falls back to Open CASCADE (OCCT) to repair and finish the mesh.
- **Shared Mesh Data:** Repeated parts share the same mesh data in Blender, saving memory while preserving their individual locations.
- **Organized Hierarchy:** Automatically rebuilds CAD assemblies into Blender collections and turns CAD part colors into Blender materials.

## Requirements & Platform Support

- Blender 5.2 or newer on Windows x64.
- The **Windows x64 release ZIP**, which includes the C++ importer, the STEP reader, the NVIDIA runtime, and Python files. You do not need to install extra packages in Blender.

> **Platform Note:** Linux and macOS support should be technically doable, but right now I don't have the time and hardware/testing resources to port and maintain builds for them. Contributions and help with cross-platform packaging are welcome.

The repository source folder alone is not enough to run the add-on: it requires the pre-staged binaries in `step_importer/runtime/windows-x64`.

## Install

1. Download `SimpleStep-importer-<version>-windows-x64.zip` from GitHub Releases.
2. In Blender, go to **Edit → Preferences → Add-ons**, click **Install from Disk**, and select the ZIP file.
3. Enable **SimpleStep importer**.
4. Restart Blender if you are updating an existing installation.

Ensure the ZIP contains the top-level `step_importer` directory along with its internal `runtime` folder. Do not unpack and repackage only the Python scripts.

## How to Import

Go to **File → Import → SimpleStep importer (.step/.stp)** and select your file.

Progress shows in the viewport status bar while the background worker processes the model across your CPU cores. When finished, your parts and collections will appear in the scene.

### Settings

| Setting | What it does |
| --- | --- |
| **Mesh quality** | Choose a fixed quality preset or **Adaptive**. Adaptive changes detail based on part size, preventing dense meshes on small fasteners while preserving smooth curvature on large bodies. |
| **Source up axis** | Rotates the model so the CAD file's up direction matches Blender's Z axis. |
| **Parallel bodies** | How many CPU processes to run at the same time (1 to 8; default is 4) to speed up conversion. |
| **Share repeated mesh data** | Uses linked duplicates for identical parts to cut memory overhead. |
| **Import STEP colors** | Turns CAD part colors into Blender materials. *Color parts without STEP colors* assigns distinctive shades to uncolored geometry. |
| **Repair failed bodies** | Tries to heal and re-mesh broken surfaces using OCCT if NVIDIA meshing fails. |
| **Skip failed parts** | Skips broken components instead of aborting the entire import. Omitted parts are listed in the report. |
| **Follow model while importing** | Centers the 3D view on parts as they load into the scene. |

For part breakdowns, skipped items, and conversion timings, open the **SimpleStep Import Report** and **SimpleStep Timing Report** inside Blender's Text Editor.

## How It Works

```
STEP File (.step/.stp)
       │
       ▼
[step-to-brep C++ Worker] ──► Multi-process background worker (Blender stays usable)
       │
       ├─► 1. NVIDIA USD BRep (Multi-core meshing + adaptive quality)
       │         │
       │         └─► [If a part fails] ──► OCCT Fallback Mesher
       │
       ▼
[Blender Python Add-on] ────► Creates collections, meshes, and materials
```

1. **Read:** The [step-to-brep](https://github.com/glenn-de-backer/step-to-brep) C++ tool parses and extracts topological parts from the STEP file.
2. **Parallel Meshing:** Multiple parts are processed simultaneously across CPU cores using NVIDIA USD BRep, dynamically tuning polygon density to avoid over-meshing smaller parts.
3. **Fallback:** If a surface fails, OCCT automatically heals the geometry and creates the mesh.
4. **Build Scene:** SimpleStep imports the cached geometry into Blender, organizing parts into collections with their materials. The output is an optimized polygon mesh, not editable CAD surfaces.

## AI Disclaimer

AI coding tools were used during development to accelerate boilerplate implementation, testing iterations, and documentation. However, the architectural design, problem identification, and engineering decisions were made entirely by me:

- **Rejected Python-Only Conversion:** AI originally proposed doing the entire conversion inside Python. That approach was rejected because running heavy STEP translation inside Blender blocks the main thread, freezes the interface, and gives students and users the false impression that Blender has crashed.
- **Dedicated Multi-Process C++ Tool:** All compute-heavy operations were moved to an independent C++ program ([step-to-brep](https://github.com/glenn-de-backer/step-to-brep)). This offloads the workload to multiple CPU processes outside Blender, preventing UI deadlocks and drastically speeding up imports for large CAD assemblies. Although very big step files still need a lot of time to process in some cases. 

## Build the Windows Package

Open a PowerShell prompt in the project root (after building `step-to-brep` and staging the runtime binaries):

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\package_runtime.ps1"
New-Item -ItemType Directory -Force ".\dist" | Out-Null
Compress-Archive -Path ".\step_importer" -DestinationPath ".\dist\SimpleStep-importer-windows-x64.zip" -Force
```

Test the generated archive in Blender before tagging a release. Keep all license notices inside the `runtime` directory.
