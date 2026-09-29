# One-time setup on Windows: create the virtual environment and install the project.
# Right-click this file and choose "Run with PowerShell", or from a terminal in this folder:
#   powershell -ExecutionPolicy Bypass -File setup.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Say($text) { Write-Host "`n== $text" }

# Windows ships a stub called python.exe that only prints "Python was not found" and offers the
# Store. It looks like Python to Get-Command, so every candidate is judged by whether it can
# actually run, with its own noise suppressed - a real Store install lives in the same folder.
$py = $null
foreach ($candidate in @("py", "python", "python3")) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    & $cmd.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" *> $null
    if ($LASTEXITCODE -eq 0) { $py = $cmd.Source; break }
}
if (-not $py) {
    Write-Host ""
    Write-Host "  Depop Seller needs Python, which this computer does not have yet."
    Write-Host ""
    Write-Host "  I am opening the download page. On the installer's FIRST screen, tick"
    Write-Host "     [x] Add python.exe to PATH"
    Write-Host "  then click Install Now. When it finishes, double-click Depop Seller again."
    Write-Host ""
    Write-Host "  (If Windows offers you Python from the Microsoft Store instead, that works too.)"
    Write-Host ""
    try { Start-Process "https://www.python.org/downloads/windows/" } catch { }
    exit 1
}
Say "using $(& $py --version) at $py"

Say "creating .venv"
if (-not (Test-Path ".venv\Scripts\python.exe")) { & $py -m venv .venv }
& ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
Say "installing depop_seller and its dependencies"
& ".venv\Scripts\python.exe" -m pip install --quiet -e .

& (Join-Path $PSScriptRoot "make_desktop_icon.ps1")

Write-Host @"

== Setup finished.

Open the app with the "Depop Seller" icon now on your Desktop - no console window
appears - and finish in its Settings tab:
  * your Anthropic API key   (pays for sorting photos into items)
  * the Claude sign-in       (writes descriptions, on your own subscription)
  * the Chrome helper        (puts the photos into Depop's form)

To stop the app: Settings -> Close Depop Seller.
"@
