$ErrorActionPreference = "Stop"

$nvidiaRoot = "C:\projects\tools\usd-brep"
$release = Join-Path $nvidiaRoot "_build\windows-x86_64\release"
$destination = Join-Path $PSScriptRoot "step_importer\runtime\windows-x64\nvidia"

New-Item -ItemType Directory -Force $destination | Out-Null

# The converter and its NVIDIA data library.
foreach ($name in @("occt_to_usd.exe", "BREP_USD_DATA.dll")) {
    Copy-Item (Join-Path $release $name) $destination -Force
}

# Preserve the staged USD runtime layout, including plugin resources.
foreach ($name in @("extraLibs", "plugin")) {
    $source = Join-Path $release $name
    $target = Join-Path $destination $name

    New-Item -ItemType Directory -Force $target | Out-Null
    Copy-Item "$source\*" $target -Recurse -Force
}

# NVIDIA's BRep schema.
$schemaSource = Join-Path $nvidiaRoot "_build\schema\omniSolid\resources"
$schemaTarget = Join-Path $destination "omniSolid\resources"

New-Item -ItemType Directory -Force $schemaTarget | Out-Null
Copy-Item "$schemaSource\*" $schemaTarget -Recurse -Force

# Preserve supplied license notices.
$licenses = Join-Path $destination "licenses"
New-Item -ItemType Directory -Force $licenses | Out-Null

Copy-Item (Join-Path $nvidiaRoot "LICENSE") $licenses -Force
Copy-Item (Join-Path $nvidiaRoot "THIRD_PARTY_NOTICES.md") $licenses -Force

$usdLicenses = Join-Path $nvidiaRoot "_build\target-deps\usd\release\PACKAGE-LICENSES"
$usdLicenseTarget = Join-Path $licenses "OpenUSD"

New-Item -ItemType Directory -Force $usdLicenseTarget | Out-Null
Copy-Item "$usdLicenses\*" $usdLicenseTarget -Recurse -Force

$pythonLicenses = Join-Path $nvidiaRoot "_build\target-deps\python\PACKAGE-LICENSES"

if (Test-Path $pythonLicenses) {
    $pythonLicenseTarget = Join-Path $licenses "Python"
    New-Item -ItemType Directory -Force $pythonLicenseTarget | Out-Null
    Copy-Item "$pythonLicenses\*" $pythonLicenseTarget -Recurse -Force
}

Write-Host "NVIDIA runtime staged at:"
Write-Host $destination

# Bundle the standalone STEP reader and its runtime DLLs.
$readerSource = "C:\projects\step-to-brep\dist\windows-x64"
$readerTarget = Join-Path $PSScriptRoot "step_importer\runtime\windows-x64\step-reader"

New-Item -ItemType Directory -Force $readerTarget | Out-Null

Copy-Item "$readerSource\step-to-brep.exe" $readerTarget -Force
Copy-Item "$readerSource\*.dll" $readerTarget -Force

$readerLicenses = Join-Path $readerTarget "licenses"
New-Item -ItemType Directory -Force $readerLicenses | Out-Null

Copy-Item "$readerSource\licenses\*" $readerLicenses -Recurse -Force

Write-Host "STEP reader staged at:"
Write-Host $readerTarget

# Bundle Python 3.12 and the NVIDIA Python package.
$pythonRuntime = Join-Path $PSScriptRoot "step_importer\runtime\windows-x64\python"
$downloadFolder = Join-Path $PSScriptRoot "build\downloads"
$pythonArchive = Join-Path $downloadFolder "python-3.12.10-embed-amd64.zip"

New-Item -ItemType Directory -Force $downloadFolder | Out-Null
New-Item -ItemType Directory -Force $pythonRuntime | Out-Null

if (-not (Test-Path $pythonArchive)) {
    Write-Host "Downloading portable Python..."

    Invoke-WebRequest `
        -Uri "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip" `
        -OutFile $pythonArchive `
        -UseBasicParsing
}

Expand-Archive -Path $pythonArchive -DestinationPath $pythonRuntime -Force

# Use explicit package paths, independent of the user's Python setup.
@(
    "python312.zip"
    "."
    "Lib\site-packages"
) | Set-Content `
    -Path (Join-Path $pythonRuntime "python312._pth") `
    -Encoding ascii

# Use development pip to populate the portable runtime.
$developmentPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$packageFolder = Join-Path $pythonRuntime "Lib\site-packages"

& $developmentPython -m pip install `
    --only-binary=:all: `
    --upgrade `
    --target $packageFolder `
    "usd-brep==1.0.1"

if ($LASTEXITCODE -ne 0) {
    throw "Failed to install the NVIDIA Python package."
}

Write-Host "Testing bundled Python..."

& "$pythonRuntime\python.exe" -c "import usd_brep as sm; box = sm.primitives.create_box((0., 0., 0.), 10., 20., 30.); print('Bundled Python: box faces =', box.face_count())"

if ($LASTEXITCODE -ne 0) {
    throw "Bundled Python test failed."
}

Write-Host "Portable Python runtime staged successfully."