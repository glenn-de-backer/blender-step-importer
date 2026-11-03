# Building and porting SimpleStep importer

This guide explains how to make SimpleStep run on Linux and macOS, and how to turn a working development build into an installable Blender ZIP.

**Current status:** the Windows version has been tested in this project. The Linux and macOS instructions below are a development guide. They have not been executed and validated as a complete SimpleStep release. Examples of new Python code describe changes still needed in the add-on; downloading this document does not implement them.

Start with **Linux x64 on Ubuntu 24.04**. It has the shortest route to a first working build. Then tackle **macOS on an Apple Silicon Mac**. Intel Macs and Linux ARM64 need separate builds and tests.

## 1. What actually needs to be ported?

Most of the Blender interface is Python and can be reused. The difficult work is getting the external programs and their libraries to run on the new operating system.

| Component | What it does | What another OS needs |
| --- | --- | --- |
| Blender add-on (`__init__.py`, `ui.py`, `core.py`) | Shows settings and progress, starts the worker, creates Blender objects | Changes to program paths and process handling; native Blender testing |
| `worker.py` | Organizes conversion, parallel jobs, repairs, fallback, and output files | Platform-aware paths and subprocess environment |
| `step-to-brep` | Reads STEP with Open CASCADE; writes parts, metadata, and fallback meshes | A native C++ build and any shared libraries it requires |
| `occt_to_usd` | Converts an OCCT BRep file into NVIDIA's USD BRep representation | A native NVIDIA build, its libraries, and schema files |
| Python 3.12 + `usd_brep` | Calls NVIDIA's native geometry and tessellation code | A matching Python interpreter and compiled extension |

The orchestration worker is **Python**. It launches C++ tools and calls native geometry libraries through Python bindings. Keeping this work in separate processes is what allows Blender's interface to remain responsive. It is not necessary to rewrite all Python code in C++.

The recovery paths also differ: the NVIDIA repair option heals a copy before retrying NVIDIA tessellation. The OCCT fallback meshes the original exported BRep through `step-to-brep`. A fallback may still fail; it does not guarantee that every face can be repaired.

### Words used in this guide

- **Build / compile:** turn C++ source code into a program your OS can run.
- **Runtime:** the programs, Python installation, libraries, and data files needed to run the add-on after building it.
- **Shared library:** a file containing code used by another program: usually `.dll` on Windows, `.so` on Linux, or `.dylib` on macOS.
- **Stage:** copy the finished files into the layout the add-on expects.
- **Architecture:** the CPU family, such as x64 or ARM64. All native components in one worker must agree on this.
- **Schema:** NVIDIA's files that teach OpenUSD how to read its BRep data. They are required even though they are not executables.

Changing `.exe` to another filename does not convert a Windows program into a Linux or Mac program. Each platform needs its own build.

## 2. Why a source download is not a working installer

The repository contains the add-on's source. The release ZIP must also include three runtime folders:

| Folder under `step_importer/runtime/<platform>/` | Contents |
| --- | --- |
| `step-reader/` | `step-to-brep`, its required libraries, and notices |
| `nvidia/` | `occt_to_usd`, required libraries, USD plugins/schema, and notices |
| `python/` | A complete Python 3.12 installation with `usd_brep` and its dependencies |

The existing platform directory is `windows-x64`. This guide proposes `linux-x64` and `macos-arm64` for new packages. These folder names are a **SimpleStep convention**, not NVIDIA's build directory names.

A user installing the release ZIP should not need a compiler, vcpkg, or a separate CAD application. The person building that ZIP does need those tools.

There are two milestones:

1. **Development build:** conversion works on your machine, even if it uses libraries from your build directories.
2. **Release package:** the copied runtime works on a second machine without those directories or development tools.

Finish the first milestone before trying to solve every packaging detail.

## 3. Choose and record the versions

Begin with the versions used by the Windows release. Record the actual commit from each checkout:

```bash
git -C ~/src/blender-step-importer rev-parse HEAD
git -C ~/src/step-to-brep rev-parse HEAD
git -C ~/src/usd-brep rev-parse HEAD
```

The NVIDIA revision previously used for Windows in this project was `3fd5b9548d19aa8c12c29bc9637a96d42a06f8a5`, associated with usd-brep 1.0.1. Keep the Python package at `usd-brep==1.0.1` when reproducing that setup. Keep the OCCT version selected by the project's `vcpkg.json`; do not silently update its baseline during the port.

Newer NVIDIA build instructions may describe features that are absent from that pinned revision. If macOS requires a newer revision, record the change and test the converter and Python bindings together. Do not copy arbitrary libraries from several NVIDIA releases into one folder.

## 4. Linux: prepare the machine

### 4.1 Native Ubuntu or WSL?

You can compile and run command-line tests under WSL. Keep the checkout under Linux's home folder, for example `~/src`, rather than reusing a Windows build directory under `/mnt/c`.

WSL produces Linux programs. A Windows Blender installation cannot use the Linux runtime package. Final testing must use Linux Blender, preferably on a native Linux machine representative of your users.

On Ubuntu 24.04, install the starting set of build tools:

```bash
sudo apt update
sudo apt install build-essential git git-lfs cmake ninja-build pkg-config \
  curl zip unzip tar python3.12 python3.12-venv python3.12-dev \
  autoconf automake libtool bison flex patchelf
cmake --version
python3.12 --version
uname -m
```

Expected: CMake 3.25 or newer, Python 3.12, and `x86_64` for this guide's Linux x64 examples. OCCT's selected vcpkg features may request additional system development packages during configuration; install the specifically named missing package and rerun configuration.

An initial Ubuntu 24.04 build is not automatically compatible with older Ubuntu releases. For wider distribution, build all native components against the oldest Linux environment you intend to support, and test that environment. The minimum requirement is determined by the strictest component in the package, not just the Python wheel.

### 4.2 Download the projects

Run these once in a fresh development folder:

```bash
mkdir -p ~/src
cd ~/src
git clone https://github.com/glenn-de-backer/blender-step-importer.git
git clone https://github.com/glenn-de-backer/step-to-brep.git
git clone https://github.com/microsoft/vcpkg.git
git clone https://github.com/NVIDIA-Omniverse/usd-brep.git
git -C usd-brep checkout 3fd5b9548d19aa8c12c29bc9637a96d42a06f8a5
```

Download NVIDIA's large test assets inside its repository, then prepare vcpkg:

```bash
cd ~/src/usd-brep
git lfs install --local
git lfs pull
cd ~/src/vcpkg
./bootstrap-vcpkg.sh
```

Create a port branch in both SimpleStep repositories before changing code:

```bash
git -C ~/src/blender-step-importer switch -c port-linux
git -C ~/src/step-to-brep switch -c port-linux
```

### 4.3 Build and test the STEP reader

```bash
cd ~/src/step-to-brep
cmake -S . -B build/linux-x64 -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_TOOLCHAIN_FILE="$HOME/src/vcpkg/scripts/buildsystems/vcpkg.cmake" \
  -DVCPKG_TARGET_TRIPLET=x64-linux
cmake --build build/linux-x64 --parallel 4
./build/linux-x64/step-to-brep --version
```

Four build jobs are a starting point; reduce them if the machine runs out of memory. This number is independent of the add-on's “Parallel bodies” setting.

Put a small known STEP file at `~/src/test-model.step`, then run:

```bash
./build/linux-x64/step-to-brep --split "$HOME/src/test-model.step" /tmp/simplestep-parts
python3.12 -m json.tool /tmp/simplestep-parts/manifest.json
```

Use a new output folder for each test. You should see exported part/prototype files and a readable manifest. Check the expected part names and number of occurrences. Choose one `.brep` file listed in that manifest and test the fallback:

```bash
./build/linux-x64/step-to-brep --mesh \
  /tmp/simplestep-parts/prototype_000001.brep /tmp/simplestep-test.mesh.json 0.1 15
```

Replace the sample BRep filename if the manifest uses a different one. The goal is a successful conversion with nonempty mesh data, not merely an executable that prints its version.

If compilation fails, first inspect `CMakeLists.txt` for Windows-only settings: MSVC flags such as `/W4`, `.dll` copying, `TARGET_RUNTIME_DLLS`, and Windows API calls. Put Windows-specific logic inside `if(WIN32)` and supply separate Unix installation rules. Do not remove a required geometry library just to make linking succeed.

### 4.4 Build NVIDIA's converter

```bash
cd ~/src/usd-brep
./prebuild.sh
./repo.sh build
```

`prebuild.sh` prepares the project and schema resources; `repo.sh build` compiles it. Packman is NVIDIA's dependency downloader and may take time on the first run.

Check for these outputs:

```bash
test -x _build/linux-x86_64/release/occt_to_usd
test -f _build/schema/omniSolid/resources/plugInfo.json
ldd _build/linux-x86_64/release/occt_to_usd
```

`test` prints nothing on success. `ldd` lists the libraries the program needs. A line ending in **not found** means a library is missing from the loader's search path.

For a development test, set the library paths in a subshell so they do not alter the rest of your terminal session:

```bash
(
  cd ~/src/usd-brep
  nv_bin="$PWD/_build/linux-x86_64/release"
  export OMNISOLID_PLUGIN_PATH="$PWD/_build/schema/omniSolid/resources"
  export LD_LIBRARY_PATH="$nv_bin:$nv_bin/extraLibs:$PWD/_build/target-deps/usd/release/lib:$PWD/_build/target-deps/python/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  "$nv_bin/occt_to_usd" /tmp/simplestep-parts/prototype_000001.brep /tmp/simplestep-test.usdc
)
```

Expected: a nonempty `/tmp/simplestep-test.usdc`. These environment paths deliberately refer to the build tree for this first test; they must be replaced with package-relative paths for release.

Run the upstream test runner if present in your pinned checkout:

```bash
cd ~/src/usd-brep
./tests.sh linux-x86_64
```

A failure caused by a missing test model may mean Git LFS files were not downloaded. A converter test failure on valid fixture data must be investigated before packaging.

### 4.5 Test Python tessellation separately

Use a temporary development environment first:

```bash
python3.12 -m venv ~/src/simplestep-dev-python
~/src/simplestep-dev-python/bin/python -m pip install --only-binary=:all: "usd-brep==1.0.1"
~/src/simplestep-dev-python/bin/python - <<'PY'
import usd_brep as sm
box = sm.primitives.create_box((0.0, 0.0, 0.0), 10.0, 20.0, 30.0)
print("Box faces:", box.face_count())
mesh = sm.tessellate(box, chord_height_tolerance=0.1,
                     curve_angle_tolerance_deg=15.0,
                     surface_angle_tolerance_deg=15.0)
print("Vertices:", len(mesh.to_mesh_arrays()["points"]))
PY
```

Expected: six box faces and a positive vertex count. Then run the same tessellation against `sm.usd.import_breps("/tmp/simplestep-test.usdc", heal=False)` to test the converter output, not just a generated box.

The 1.0.1 wheels are for CPython 3.12 on Windows and Linux. `pip` reporting “No matching distribution” usually means the Python version, architecture, or Linux platform does not match. Do not install the wheel in Blender's Python 3.13 environment.

## 5. Changes needed in the add-on itself

Build success does not make the add-on cross-platform. Both **the launcher in `core.py`** and **the worker in `worker.py`** must find the right programs. Updating only the worker leaves Blender trying to launch `python.exe`.

Create one small shared module, for example `runtime_paths.py`, to hold runtime selection. This illustrative helper uses the proposed folder layout:

```python
from pathlib import Path
import platform
import sys

def runtime_paths():
    arch = platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64"}.get(arch, arch)
    os_name = {"win32": "windows", "linux": "linux", "darwin": "macos"}.get(sys.platform)
    if os_name is None:
        raise RuntimeError(f"Unsupported operating system: {sys.platform}")
    root = Path(__file__).resolve().parent / "runtime" / f"{os_name}-{arch}"
    suffix = ".exe" if os_name == "windows" else ""
    paths = {
        "root": root,
        "python": root / "python" / ("python.exe" if os_name == "windows" else "bin/python3.12"),
        "reader": root / "step-reader" / f"step-to-brep{suffix}",
        "converter": root / "nvidia" / f"occt_to_usd{suffix}",
    }
    for name in ("python", "reader", "converter"):
        if not paths[name].is_file():
            raise RuntimeError(f"Missing {os_name}-{arch} runtime file: {paths[name]}")
    return paths
```

This detects and locates files; it does not validate their CPU architecture. Check that with `file` during packaging. Only ship platforms that have passed the release tests.

Because `worker.py` runs as a standalone script, avoid making it import the `step_importer` package just to get this helper: the package initializer imports `bpy`, which is unavailable in ordinary Python. Use a relative import from Blender and a direct sibling import from the standalone worker.

Make the following changes alongside the helper:

| Location | Required change | Why |
| --- | --- | --- |
| `core.py` | Launch the helper's Python path; keep `worker.py` as a separate process | Blender must remain responsive |
| `worker.py` | Replace the Windows-only rejection and hard-coded program paths | Allow the new packaged platform |
| Both | Use `os.pathsep` when joining search paths | Windows uses `;`; Unix uses `:` |
| Both | Guard Windows-only `STARTUPINFO`, creation flags, and `taskkill` logic | Those do not run on Linux/macOS |
| Worker launch | Pass a copied environment with explicit UTF-8 output settings | Part names and logs may contain non-ASCII characters |
| Converter launch | Pass NVIDIA library and schema paths only to the subprocess that needs them | Avoid loading conflicting USD libraries into Blender |
| Cancellation | Track and stop the worker and all its child converters | Closing the progress display must not leave jobs running |

Use argument lists with `subprocess.Popen`/`run`, rather than building shell command strings. Keep all `bpy` calls on Blender's main thread. For Unix cancellation, one design is to start the top-level worker with `start_new_session=True`, let its children inherit the process group, and terminate that group on cancellation; test cleanup before integrating this design.

Search for the old assumptions with:

```bash
cd ~/src/blender-step-importer
rg -n 'windows-x64|python.exe|\.exe|STARTUPINFO|CREATE_NO_WINDOW|taskkill|os.name|sys.platform' step_importer
```

If `rg` is not installed, use your editor's project search. Keep the existing Windows behavior working while adding the other branches.

## 6. Linux: assemble a runtime users can download

### 6.1 Choose a fixed layout

For this guide, the proposed Unix layout puts the reader and converter directly in their component folders, their libraries in `lib/`, and Python at `python/bin/python3.12`. Configure the helper and packaging script consistently with that choice.

```bash
cd ~/src/blender-step-importer
runtime="$PWD/step_importer/runtime/linux-x64"
mkdir -p "$runtime/step-reader/lib" "$runtime/nvidia/lib" "$runtime/nvidia/omniSolid/resources"
cp ~/src/step-to-brep/build/linux-x64/step-to-brep "$runtime/step-reader/"
cp ~/src/usd-brep/_build/linux-x86_64/release/occt_to_usd "$runtime/nvidia/"
cp -a ~/src/usd-brep/_build/schema/omniSolid/resources/. "$runtime/nvidia/omniSolid/resources/"
```

These commands copy the main programs and schema only. Complete the library and Python steps below before running the add-on.

### 6.2 Include the libraries those programs need

Run `ldd` on each original executable with the working development environment from section 4. For each project/dependency library it lists:

1. Copy the real library file to the appropriate component's `lib/` folder, preserving the filename the executable requests. Preserve its versioned filename links if they are used.
2. Run `ldd` on that copied library too. It may depend on another library.
3. Repeat until every dependency is either in the bundle or part of the documented base Linux system.

Do not copy the build machine's entire system library directory. In particular, treat the system C library and loader as part of the target Linux requirements. Keep the reader's OCCT libraries and the converter's NVIDIA/OpenUSD libraries in separate folders.

For development, pass `LD_LIBRARY_PATH` containing the relevant component's `lib`, its own folder, and any retained `extraLibs` folder. For a relocatable release, an alternative is to set relative library search paths while linking (or with `patchelf`), using `$ORIGIN` to mean “the directory containing this executable or library.” This must be correct for dependent libraries as well as the executable.

USD plugins may be loaded at runtime and will not all appear in `ldd`. Retain the plugin directories used by the NVIDIA build, preserve their relative paths, and inspect `plugInfo.json` for references to files you have moved. Do not guess a fixed DLL-to-SO replacement list from Windows.

### 6.3 Bundle a real Python installation

Do not copy the development venv as the final runtime: it can depend on a Python installation outside the folder.

A candidate is [python-build-standalone](https://github.com/astral-sh/python-build-standalone/releases). Download a **CPython 3.12**, **x86_64-unknown-linux-gnu**, **install_only** archive. Select and record an exact release filename and its published checksum. Do not choose a musl or another-architecture build for the Linux wheel used here.

Extract that trusted archive into a temporary folder. It contains a `python` directory. Copy that complete directory into `runtime/linux-x64/python`, then install with that interpreter:

```bash
runtime="$HOME/src/blender-step-importer/step_importer/runtime/linux-x64"
"$runtime/python/bin/python3.12" --version
"$runtime/python/bin/python3.12" -m pip install --only-binary=:all: "usd-brep==1.0.1"
"$runtime/python/bin/python3.12" -c 'import usd_brep as sm; print(sm.primitives.create_box((0,0,0),10,20,30).face_count())'
```

Expected: Python 3.12 and `6`. Move the complete runtime to a different folder and repeat the test. This checks that it does not rely on its original installation path. Include Python's supplied notices and the wheel's license files.

The wheel includes its own USD libraries. Keep them with the wheel instead of replacing them with the converter's build-tree libraries. Start its worker with an environment that does not force a different copy of OpenUSD into the process.

## 7. macOS: what is different?

The STEP reader can be built with a Mac C++ compiler. The harder part is NVIDIA's complete USD BRep stack: the published 1.0.1 Python wheels do not include macOS, so a Mac build also needs the Python binding compiled from source.

Start with one Apple Silicon target. NVIDIA calls one build configuration `macos-universal`, but that name alone is not evidence that every resulting library supports Intel Macs. Verify with `file` or `lipo -info` before claiming support for either architecture.

### 7.1 Prepare a Mac

Install Apple's command-line developer tools:

```bash
xcode-select --install
```

After that installer finishes, use an existing [Homebrew](https://brew.sh/) installation to get the development tools:

```bash
brew install cmake ninja git git-lfs python@3.12 pkg-config
uname -m
"$(brew --prefix python@3.12)/bin/python3.12" --version
```

Expected for this route: `arm64` and Python 3.12. Clone the same four repositories as in section 4, bootstrap vcpkg, and make port branches. Use fresh build directories; never reuse the Windows or Linux CMake cache.

### 7.2 Build the Mac STEP reader

```bash
cd ~/src/step-to-brep
cmake -S . -B build/macos-arm64 -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_OSX_ARCHITECTURES=arm64 \
  -DCMAKE_TOOLCHAIN_FILE="$HOME/src/vcpkg/scripts/buildsystems/vcpkg.cmake" \
  -DVCPKG_TARGET_TRIPLET=arm64-osx
cmake --build build/macos-arm64 --parallel 4
./build/macos-arm64/step-to-brep --version
otool -L ./build/macos-arm64/step-to-brep
```

Repeat the STEP splitting and `--mesh` tests from section 4 using this executable. The manifest and geometry should agree with the Windows test model within the chosen mesh tolerances.

### 7.3 Resolve the NVIDIA build prerequisites first

NVIDIA's documented Mac setup has two concrete obstacles:

- Its basic Mac build does not include the full USD converter and Python binding needed by SimpleStep. OpenUSD must be built locally and connected to NVIDIA's build.
- The documented NVIDIA-specific Mac premake package is not available on the public Packman server. Premake generates the native build files. If Packman cannot fetch it, installing an arbitrary Homebrew premake version is not a proven substitute.

Check the pinned checkout for `tools/scripts/setup-macos-local-deps.sh`. If that helper or its documented configuration is absent, the pinned Windows revision does not provide that route: select a documented newer source revision on the Mac port branch and record it. Rebuild and test the entire NVIDIA stack from that revision.

If the required premake package cannot be obtained, this is a real build-system task: obtain a supported package from upstream, or adapt and validate the project generator against an available premake build. Record that patch in the port branch. The port cannot progress through NVIDIA compilation until this is resolved. There is no confirmed one-line install command for this missing package in the evidence used for this guide.

### 7.4 Build OpenUSD for Python 3.12

OpenUSD is the library that reads and writes the intermediate `.usdc` file. It is separate from the `usd_brep` geometry library. Installing a Python `usd-core` wheel is not a substitute for the C++ headers and libraries needed for this source build.

Use the OpenUSD version required by the selected NVIDIA revision. The documented local example uses **25.11 with Python 3.12**. For that combination, the following is a starting build command:

```bash
cd ~/src
git clone --branch v25.11 https://github.com/PixarAnimationStudios/OpenUSD.git OpenUSD-25.11
mac_python="$(brew --prefix python@3.12)/bin/python3.12"
"$mac_python" ~/src/OpenUSD-25.11/build_scripts/build_usd.py \
  "$HOME/opt/usd-25.11-py312" --no-usdview
```

The build script downloads and builds its dependencies. Keep Python support enabled, and use the architecture and library configuration expected by the NVIDIA helper. If its build output reports another Python version, correct the Python selection before proceeding.

After the build, check that `~/opt/usd-25.11-py312` contains `include`, `lib`, and the Python bindings (normally under `lib/python`). Then connect it to NVIDIA's source build:

```bash
cd ~/src/usd-brep
export SMLIB_USD_ROOT="$HOME/opt/usd-25.11-py312"
export SMLIB_PYTHON_ROOT="$(brew --prefix python@3.12)"
./tools/scripts/setup-macos-local-deps.sh
./prebuild.sh -p macos-universal
./repo.sh build -p macos-universal
```

These commands depend on section 7.3 being resolved. The helper's accepted Python layout must match the interpreter you selected; if it rejects the Homebrew prefix, use the full Python installation root it requests. This command sequence has not been validated on a Mac for SimpleStep.

### 7.5 Prove the full stack exists

Check for both outputs:

```bash
cd ~/src/usd-brep
test -x _build/macos-universal/release/occt_to_usd
find _build/macos-universal/release -name '_omni_solid*.so'
otool -L _build/macos-universal/release/occt_to_usd
```

A successful basic build without `_omni_solid` is insufficient. Run a conversion and the Python box/tessellation tests with this build's Python bindings and library paths. Next, import and tessellate the converted `.usdc` file.

For the final bundle, build the Python extension against the Python 3.12 distribution you intend to ship, or explicitly validate compatibility and library paths with that distribution. Copying a Homebrew Python prefix into a ZIP is not a reliable portable runtime.

Mac library references can point to absolute paths such as `/opt/homebrew/...`. Such a bundle may only work on your development machine. Use `otool -L` on each shipped native file to find these references. Copy required non-system libraries and fix references at build time or with `install_name_tool` to use the package's own folders. `@loader_path` means the folder of the loading binary; `@rpath` uses its configured search paths. Apply fixes before signing the final binaries.

Finally test the downloaded ZIP on a second Mac, including executable permissions and macOS security checks. Do not instruct users to disable system security globally. A distributable Mac package may need signing/notarization work in addition to a successful local compile.

## 8. Package and test the add-on

Use a fresh staging directory containing **only the target platform's runtime**, the add-on files, and required licenses. Do not ship all three operating systems in one archive initially.

Before creating the ZIP:

1. Confirm both Blender and worker launch paths use the platform helper.
2. Confirm the reader, converter, and Python tests work using only the staged folders.
3. Check that executable files have execute permission (`chmod +x` for the reader, converter, and real Python executable if needed).
4. Include `LICENSE` and all dependency notices. Keep the package and source commit versions recorded together.

For example, once `~/src/simplestep-stage/step_importer` has been fully prepared:

```bash
cd ~/src/simplestep-stage
zip -r ../SimpleStep-importer-linux-x64.zip step_importer
```

Use a macOS-specific filename for the Mac build. Include the release version when publishing. Check Python symlinks in particular: they must resolve inside the package, or the archive must contain the real target files. A symlink back into `~/src` does not make a self-contained runtime.

Extract to a new location and test again. Installation through Blender can affect executable permissions differently from shell extraction, so also test the exact install route a user will follow. If Blender extraction removes executable permission, the launcher should restore it on the known bundled executables before launching them, and report a useful error if the installation location forbids execution.

Run these Blender tests on the target OS:

| Test | Expected result |
| --- | --- |
| Small assembly with repeated parts | Correct count, placement, names, colors, and collection hierarchy |
| Known dimensioned test model | Correct size after scene-unit conversion |
| NVIDIA failure recovered by OCCT | Fallback succeeds and is reported |
| Unrecoverable part with skipping enabled | Other parts load; missing part is listed |
| Cancel during conversion with several workers | All child processes stop; Blender stays usable |
| Large assembly | Progress updates, bounded memory use, useful timing report |
| Paths containing spaces and accented letters | Successful import and readable logs |
| New Blender file after completion | Old progress overlay is cleared |
| Fresh machine without build tools | No references to developer directories are required |

A terminal-only WSL test covers the Linux helper programs, not all Blender integration or desktop behavior.

## 9. What to commit for a port

The finished port should add:

- A shared runtime-path helper used by Blender and the standalone worker.
- Platform-specific process startup and cancellation where necessary.
- CMake changes for native compilation and runtime installation.
- A repeatable `package_runtime_linux.sh` or `package_runtime_macos.sh` that copies the verified dependency set and checks required files.
- A record of exact dependency versions, minimum tested OS, CPU architecture, and test results.

Those shell scripts do not exist merely because they are named here. Create them from the manual process once it works. They should fail when a required file is absent, rather than create an incomplete ZIP.

Upload compiled packages to GitHub Releases. Keep source code, build scripts, and instructions in Git. Verify the Windows package again after merging shared Python changes.

## 10. Rebuilding the existing Windows runtime

For Windows contributors, the missing runtime is assembled from the existing builds. In PowerShell:

```powershell
Set-Location "C:\projects\step-to-brep"
cmake --build build --config Release --parallel
cmake --install build --config Release --prefix "C:/projects/step-to-brep/dist/windows-x64"
Set-Location "C:\projects\blender-step-importer"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\package_runtime.ps1"
.\step_importer\runtime\windows-x64\step-reader\step-to-brep.exe --version
```

This assumes the STEP reader's CMake project has already been configured and NVIDIA's `occt_to_usd` build exists. The staging script may contain the original developer's paths: adjust those inputs to your own checkout locations. It copies the reader, NVIDIA runtime, and Python package into the add-on; it is not a Linux or Mac build script.

## 11. Troubleshooting

| Message or symptom | Meaning and next action |
| --- | --- |
| `Exec format error` | Wrong OS/CPU binary. Run `file <program>` and compare with `uname -m`. |
| `Permission denied` | Check execute permission and whether the filesystem permits execution. |
| Linux: a `.so` is `not found` | Use `ldd`, copy the missing non-system dependency, and correct the subprocess search path. |
| macOS: `Library not loaded` | Inspect `otool -L`; repair the library location/reference and re-sign changed binaries when required. |
| `No module named usd_brep` | Install/stage the package in the interpreter that actually launches the worker. |
| A `cp312` extension fails in Blender | It requires Python 3.12. Run it in the separate worker, not Blender's Python 3.13. |
| BRep schema is unknown | Check `OMNISOLID_PLUGIN_PATH` and the copied `plugInfo.json`/schema resources. |
| Packman fails fetching macOS premake | Resolve the upstream build dependency described in section 7.3; copying more add-on Python files cannot fix it. |
| Works in Terminal, fails in Blender | Blender does not necessarily inherit your shell setup. Construct the required subprocess environment explicitly. |
| Works on the build machine only | A library, Python installation, or plugin still comes from outside the bundle. Repeat the relocation and clean-machine tests. |

## License and sources

The original SimpleStep importer code is licensed under **GPL-3.0-only**; see [LICENSE](LICENSE). Bundled dependencies retain their own license terms and notices. The separate `step-to-brep` repository must state its own license.

Upstream information was checked on 3 November 2026. Commands against a pinned source revision take precedence over instructions for a newer upstream `main` branch.

- [NVIDIA build instructions and macOS prerequisites](https://github.com/NVIDIA-Omniverse/usd-brep/blob/main/README.md)
- [NVIDIA build project: conditional USD components](https://github.com/NVIDIA-Omniverse/usd-brep/blob/main/premake5.lua)
- [NVIDIA Python API installation](https://nvidia-omniverse.github.io/usd-brep/latest/docs/python.html)
- [usd-brep 1.0.1 wheel files](https://pypi.org/project/usd-brep/1.0.1/#files)
- [OpenUSD build instructions](https://github.com/PixarAnimationStudios/OpenUSD/blob/release/README.md)
- [Standalone Python distributions](https://gregoryszorc.com/docs/python-build-standalone/main/running.html)
- [vcpkg and CMake](https://learn.microsoft.com/en-us/vcpkg/users/buildsystems/cmake-integration)
