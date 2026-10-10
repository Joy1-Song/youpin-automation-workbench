param(
    [string]$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$PythonExe = "python",
    [switch]$Live
)

$ErrorActionPreference = "Stop"
Set-Location $ProjectDir

New-Item -ItemType Directory -Force -Path (Join-Path $ProjectDir "logs") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $ProjectDir "state") | Out-Null

$runner = Join-Path $ProjectDir "youpin_auto_runner.py"
$config = Join-Path $ProjectDir "config.local.json"
$envFile = Join-Path $ProjectDir ".env"
$logFile = Join-Path $ProjectDir "logs\auto_runner.log"
$statusFile = Join-Path $ProjectDir "state\auto_runner_status.json"

$argsList = @(
    $runner,
    "--config", $config,
    "--env", $envFile,
    "--log-file", $logFile,
    "--status-file", $statusFile
)

if ($Live) {
    $argsList += "--live"
}

& $PythonExe @argsList
