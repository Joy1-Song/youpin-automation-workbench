param(
    [string]$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
Set-Location $ProjectDir

New-Item -ItemType Directory -Force -Path (Join-Path $ProjectDir "logs") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $ProjectDir "state") | Out-Null

if (-not $PythonExe) {
    $pythonw = Get-Command "pythonw.exe" -ErrorAction SilentlyContinue
    if ($pythonw) {
        $PythonExe = $pythonw.Source
    } else {
        $python = Get-Command "python.exe" -ErrorAction SilentlyContinue
        if ($python) {
            $PythonExe = $python.Source
        } else {
            $PythonExe = "python"
        }
    }
}

$env:PYTHONIOENCODING = "utf-8"
& $PythonExe (Join-Path $ProjectDir "youpin_desktop_app.py")
