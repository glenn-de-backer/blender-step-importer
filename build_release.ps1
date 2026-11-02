# Run from PowerShell after copying the four source files into step_importer.
$ErrorActionPreference = "Stop"
$project = "C:\projects\blender-step-importer"
Set-Location $project

$addon = Join-Path $project "step_importer"
$worker = Join-Path $addon "worker.py"
$version = Select-String -Path $worker -Pattern '^WORKER_VERSION = "1\.5\.2"$' -Quiet
if (-not $version) { throw "Expected STEP worker 1.5.2 in $worker" }
$addonVersion = Select-String -Path (Join-Path $addon "__init__.py") `
    -Pattern '"version": \(1, 5, 2\)' -Quiet
if (-not $addonVersion) { throw "Expected SimpleStep addon 1.5.2" }

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\package_runtime.ps1"
if ($LASTEXITCODE -ne 0) { throw "Runtime staging failed (exit $LASTEXITCODE)" }

$reader = Join-Path $addon "runtime\windows-x64\step-reader\step-to-brep.exe"
$required = @(
    $reader,
    (Join-Path $addon "runtime\windows-x64\python\python.exe"),
    (Join-Path $addon "runtime\windows-x64\nvidia\occt_to_usd.exe"),
    (Join-Path $addon "runtime\windows-x64\nvidia\omniSolid\resources\plugInfo.json")
)
foreach ($path in $required) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing: $path" }
}

$readerVersion = (& $reader --version | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $readerVersion -notmatch '^step-to-brep 1\.5\.\d+$') {
    throw "Expected step-to-brep 1.5.x, got: $readerVersion"
}

New-Item -ItemType Directory -Force ".\dist" | Out-Null
$archive = Join-Path $project "dist\SimpleStep-importer-1.5.2-windows-x64.zip"
Compress-Archive -Path ".\step_importer" -DestinationPath $archive -Force

Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::OpenRead($archive)
try {
    $entries = @($zip.Entries | ForEach-Object { $_.FullName.Replace('\', '/') })
    foreach ($name in @(
        "__init__.py", "core.py", "ui.py", "worker.py",
        "runtime/windows-x64/python/python.exe",
        "runtime/windows-x64/step-reader/step-to-brep.exe",
        "runtime/windows-x64/nvidia/occt_to_usd.exe"
    )) {
        if ($entries -notcontains "step_importer/$name") { throw "Archive missing: $name" }
    }
}
finally {
    $zip.Dispose()
}

Write-Host "Reader: $readerVersion"
Write-Host "Release candidate: $archive"
Write-Host "Install in Blender, restart Blender, then test STEP colors and Adaptive quality."
