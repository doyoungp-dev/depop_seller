# Put a "Depop Seller" icon on the Desktop.
# The icon starts the app with no console window at all (pythonw.exe); if you ever need to see
# what it is doing, run "Depop Seller.cmd" in this folder instead, or read the log file the
# Settings tab points at.
# Run it once: right-click this file and choose "Run with PowerShell". Safe to run again.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$pythonw = Join-Path $PSScriptRoot ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) {
    Write-Host "The virtual environment is missing - run setup.ps1 first."
    exit 1
}

$link = Join-Path ([Environment]::GetFolderPath("Desktop")) "Depop Seller.lnk"
$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($link)
$sc.TargetPath = $pythonw                      # pythonw = no console window
$sc.Arguments = "-m depop_seller hub"
$sc.WorkingDirectory = $PSScriptRoot
$sc.Description = "Depop Seller - batches, photo review and selling"
$sc.IconLocation = Join-Path $PSScriptRoot "depop_seller\static\icon.ico"
$sc.Save()

Write-Host "Done: $link"
Write-Host "Double-click it to open the app - no console window appears."
Write-Host "To stop the app: Settings tab -> Close Depop Seller (or just restart the computer)."
