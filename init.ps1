param(
    [switch]$ForceInstall
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectRoot

Write-Host "=== Turtle Investment Framework - GPT/Codex Setup ==="
Write-Host "Project root: $ProjectRoot"

$VenvDir = Join-Path $ProjectRoot ".venv"
$PythonBin = Join-Path $VenvDir "Scripts\python.exe"

Write-Host "[1/5] Setting up Python environment..."
$PythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $PythonCommand) {
    throw "Python >= 3.10 was not found. Install Python and ensure 'python' is on PATH."
}

$VersionText = & $PythonCommand.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
$VersionParts = $VersionText.Split('.')
if ([int]$VersionParts[0] -lt 3 -or ([int]$VersionParts[0] -eq 3 -and [int]$VersionParts[1] -lt 10)) {
    throw "Python >= 3.10 is required; found $VersionText."
}

$VenvCreated = $false
if (-not (Test-Path -LiteralPath $PythonBin)) {
    & $PythonCommand.Source -m venv $VenvDir
    $VenvCreated = $true
}
Write-Host "  Python: $(& $PythonBin --version)"

Write-Host "[2/5] Installing Python dependencies..."
if ($VenvCreated -or $ForceInstall) {
    & $PythonBin -m pip install -q -r requirements.txt
    Write-Host "  Dependencies installed."
} else {
    Write-Host "  Skipped (venv exists). Use .\init.ps1 -ForceInstall to reinstall."
}

Write-Host "[3/5] Checking environment..."
$EnvFile = Join-Path $ProjectRoot ".env"
if (Test-Path -LiteralPath $EnvFile) {
    foreach ($Line in Get-Content -LiteralPath $EnvFile) {
        $Trimmed = $Line.Trim()
        if (-not $Trimmed -or $Trimmed.StartsWith('#') -or -not $Trimmed.Contains('=')) { continue }
        $Name, $Value = $Trimmed.Split('=', 2)
        if (-not [Environment]::GetEnvironmentVariable($Name.Trim(), 'Process')) {
            [Environment]::SetEnvironmentVariable($Name.Trim(), $Value.Trim().Trim('"').Trim("'"), 'Process')
        }
    }
    Write-Host "  Loaded .env file"
}

Write-Host "  Market data: AKShare + Yahoo Finance (no data token required)"

if (-not $env:OPENAI_API_KEY) {
    Write-Host "  OPENAI_API_KEY: not set (normal for authenticated Codex sessions)"
} else {
    $Model = if ($env:OPENAI_MODEL) { $env:OPENAI_MODEL } else { "gpt-5.6" }
    $Effort = if ($env:OPENAI_REASONING_EFFORT) { $env:OPENAI_REASONING_EFFORT } else { "high" }
    Write-Host "  OPENAI_API_KEY: set ($($env:OPENAI_API_KEY.Length) chars)"
    Write-Host "  GPT model: $Model; reasoning effort: $Effort"
}

Write-Host "[4/5] Ensuring output directory..."
New-Item -ItemType Directory -Path (Join-Path $ProjectRoot "output") -Force | Out-Null

Write-Host "[5/5] Running verification tests..."
& $PythonBin -m pytest tests/ -x -q --tb=short --basetemp .test-tmp\pytest-setup
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "=== Setup complete ==="
Write-Host "In Codex, run: `$business-analysis 600887"
