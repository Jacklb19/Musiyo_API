param(
    [Parameter(Mandatory = $true)][string]$DatabasePath,
    [Parameter(Mandatory = $true)][string]$StorageRoot,
    [string]$WebOrigin = 'http://localhost:5174',
    [ValidateRange(1, 65535)][int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
$apiRoot = Split-Path $PSScriptRoot -Parent
$databaseFile = (Resolve-Path -LiteralPath $DatabasePath).Path
$resourceDirectory = (Resolve-Path -LiteralPath $StorageRoot).Path
if (-not (Test-Path -LiteralPath $databaseFile -PathType Leaf)) { throw 'DatabasePath must name an existing SQLite database.' }
if (-not (Test-Path -LiteralPath $resourceDirectory -PathType Container)) { throw 'StorageRoot must name the matching private resource directory.' }
$originUri = [Uri]$WebOrigin
if (-not $originUri.IsAbsoluteUri -or $originUri.Scheme -notin 'http', 'https' -or
    $originUri.AbsolutePath -ne '/' -or $originUri.Query -or $originUri.Fragment -or $originUri.UserInfo) {
    throw 'WebOrigin must be an absolute HTTP or HTTPS origin without a path or credentials.'
}
$pythonPath = Join-Path $apiRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) { throw 'Install the API development virtual environment first.' }

$env:MUSIYO_DATABASE_URL = 'sqlite:///' + $databaseFile.Replace('\', '/')
$env:MUSIYO_STORAGE_ROOT = $resourceDirectory
$env:MUSIYO_STORAGE_BACKEND = 'local'
$env:MUSIYO_WEB_ORIGIN = $WebOrigin
Push-Location -LiteralPath $apiRoot
try {
    Write-Output "Starting the local API on port $Port with the selected database and private storage."
    & $pythonPath -m uvicorn app.main:app --host 127.0.0.1 --port $Port
    if ($LASTEXITCODE -ne 0) { throw "The API exited with code $LASTEXITCODE." }
} finally { Pop-Location }
