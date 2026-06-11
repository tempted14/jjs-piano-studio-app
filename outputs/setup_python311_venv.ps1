$ErrorActionPreference = "Stop"

Write-Host "JJS Piano Studio setup"
Write-Host "This project needs Python 3.11 for Basic Pitch / audio-to-MIDI support."
Write-Host ""

$python = $null

try {
    $python = (py -3.11 -c "import sys; print(sys.executable)") 2>$null
} catch {
    $python = $null
}

if (-not $python) {
    $commonPaths = @(
        "$env:LocalAppData\Programs\Python\Python311\python.exe",
        "C:\Program Files\Python311\python.exe",
        "C:\Program Files (x86)\Python311\python.exe"
    )
    foreach ($path in $commonPaths) {
        if (Test-Path $path) {
            $python = $path
            break
        }
    }
}

if (-not $python) {
    Write-Host "Python 3.11 was not found."
    Write-Host "Install it from https://www.python.org/downloads/release/python-3119/"
    Write-Host "During install, check 'Add python.exe to PATH'."
    Write-Host "Then run this script again."
    exit 1
}

Write-Host "Using Python: $python"

if (Test-Path ".\.venv\Scripts\python.exe") {
    try {
        & .\.venv\Scripts\python.exe -c "import sys; print(sys.executable)" *> $null
    } catch {
        Write-Host "Existing .venv is stale or points to a missing Python install. Rebuilding it."
        Remove-Item -LiteralPath ".\.venv" -Recurse -Force
    }
}

& $python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

Write-Host ""
Write-Host "Setup complete."
Write-Host "Start the studio with:"
Write-Host ".\.venv\Scripts\python.exe jjs_piano_studio.py"
