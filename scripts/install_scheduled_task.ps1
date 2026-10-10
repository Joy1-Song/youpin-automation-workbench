param(
    [string]$TaskName = "YouPinMajorAutoBidder",
    [string]$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$PythonExe = "",
    [switch]$Live
)

$ErrorActionPreference = "Stop"
$ProjectDir = (Resolve-Path $ProjectDir).Path

if (-not $PythonExe) {
    $pythonCommand = Get-Command python -ErrorAction Stop
    $PythonExe = $pythonCommand.Source
}

$config = Join-Path $ProjectDir "config.local.json"
$configExample = Join-Path $ProjectDir "config.example.json"
if (-not (Test-Path $config) -and (Test-Path $configExample)) {
    Copy-Item -Path $configExample -Destination $config
    Write-Host "Created config.local.json from config.example.json"
}

$envFile = Join-Path $ProjectDir ".env"
$envExample = Join-Path $ProjectDir ".env.example"
if (-not (Test-Path $envFile) -and (Test-Path $envExample)) {
    Copy-Item -Path $envExample -Destination $envFile
    Write-Host "Created .env from .env.example"
}

New-Item -ItemType Directory -Force -Path (Join-Path $ProjectDir "logs") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $ProjectDir "state") | Out-Null

$runScript = Join-Path $ProjectDir "scripts\run_auto_runner.ps1"
$argumentParts = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", "`"$runScript`"",
    "-ProjectDir", "`"$ProjectDir`"",
    "-PythonExe", "`"$PythonExe`""
)
if ($Live) {
    $argumentParts += "-Live"
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ($argumentParts -join " ") -WorkingDirectory $ProjectDir
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel LeastPrivilege

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "Installed scheduled task: $TaskName"
Write-Host "Project: $ProjectDir"
Write-Host "Python:  $PythonExe"
Write-Host "Live:    $Live"
Write-Host ""
Write-Host "Before live automation, edit config.local.json and .env."
Write-Host "Start manually: Start-ScheduledTask -TaskName `"$TaskName`""
Write-Host "View status:    Get-ScheduledTask -TaskName `"$TaskName`""
