param(
    [string]$TaskName = "YouPinMajorAutoBidder"
)

$ErrorActionPreference = "Stop"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Uninstalled scheduled task: $TaskName"
} else {
    Write-Host "Scheduled task not found: $TaskName"
}
