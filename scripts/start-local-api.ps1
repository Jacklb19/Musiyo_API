param(
    [Parameter(Mandatory = $true)][string]$DatabasePath,
    [Parameter(Mandatory = $true)][string]$StorageRoot,
    [string]$WebOrigin = 'http://localhost:5174',
    [string]$GuideConfig,
    [string]$QuizBundle,
    [string]$VoiceConfig,
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

$variableNames = @('MUSIYO_DATABASE_URL', 'MUSIYO_STORAGE_ROOT', 'MUSIYO_STORAGE_BACKEND', 'MUSIYO_WEB_ORIGIN', 'MUSIYO_GUIDE_CONFIG', 'MUSIYO_QUIZ_BUNDLE', 'MUSIYO_VOICE_CONFIG')
$variableNames += @(Get-ChildItem Env:MUSIYO_GUIDE_* | ForEach-Object Name)
$previousValues = @{}
foreach ($name in ($variableNames | Select-Object -Unique)) {
    $previousValues[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
Push-Location -LiteralPath $apiRoot
try {
    # A selected file is authoritative; inherited adapter overrides must not silently change it.
    foreach ($name in $previousValues.Keys) {
        if ($name -like 'MUSIYO_GUIDE_*') { [Environment]::SetEnvironmentVariable($name, $null, 'Process') }
    }
    if ($GuideConfig) { $env:MUSIYO_GUIDE_CONFIG = (Resolve-Path -LiteralPath $GuideConfig).Path }
    $env:MUSIYO_QUIZ_BUNDLE = $null
    if ($QuizBundle) {
        $env:MUSIYO_QUIZ_BUNDLE = (Resolve-Path -LiteralPath $QuizBundle).Path
        & $pythonPath -m scripts.inspect_session quiz $env:MUSIYO_QUIZ_BUNDLE
        if ($LASTEXITCODE -ne 0) { throw 'Invalid selected quiz bundle.' }
    }
    # Voice stays disabled unless this session selects an external voice file.
    $env:MUSIYO_VOICE_CONFIG = $null
    if ($VoiceConfig) {
        $env:MUSIYO_VOICE_CONFIG = (Resolve-Path -LiteralPath $VoiceConfig).Path
        & $pythonPath -m scripts.inspect_session voice $env:MUSIYO_VOICE_CONFIG
        if ($LASTEXITCODE -ne 0) { throw 'Invalid selected voice configuration.' }
    }
    $env:MUSIYO_DATABASE_URL = 'sqlite:///' + $databaseFile.Replace('\', '/')
    $env:MUSIYO_STORAGE_ROOT = $resourceDirectory
    $env:MUSIYO_STORAGE_BACKEND = 'local'
    $env:MUSIYO_WEB_ORIGIN = $WebOrigin
    & $pythonPath -c 'from app.guide_config import GuideSettings; GuideSettings.from_environment()'
    if ($LASTEXITCODE -ne 0) { throw 'Invalid selected guide configuration.' }
    if (@(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue).Count) {
        throw 'The selected API port is occupied; no service was changed.'
    }
    Write-Output "Starting the local API on port $Port with the selected database and private storage."
    & $pythonPath -m uvicorn app.main:app --host 127.0.0.1 --port $Port
    if ($LASTEXITCODE -ne 0) { throw "The API exited with code $LASTEXITCODE." }
} finally {
    foreach ($name in $previousValues.Keys) { [Environment]::SetEnvironmentVariable($name, $previousValues[$name], 'Process') }
    Pop-Location
}
