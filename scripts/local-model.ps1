param(
    [Parameter(Mandatory = $true)][ValidateSet('start', 'stop', 'health')][string]$Action,
    [Parameter(Mandatory = $true)][string]$RuntimeDirectory,
    [Parameter(Mandatory = $true)][string]$ModelsDirectory,
    [ValidateRange(1024, 65535)][int]$Port = 11434
)

$ErrorActionPreference = 'Stop'
$apiRoot = (Resolve-Path -LiteralPath (Split-Path $PSScriptRoot -Parent)).Path
$runtimeRoot = (Resolve-Path -LiteralPath $RuntimeDirectory).Path
$modelRoot = [IO.Path]::GetFullPath($ModelsDirectory)
foreach ($selectedRoot in @($runtimeRoot, $modelRoot)) {
    if ($selectedRoot.Equals($apiRoot, [StringComparison]::OrdinalIgnoreCase) -or
        $selectedRoot.StartsWith($apiRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Model files and runtime state must stay outside the repository.'
    }
}
$executable = Join-Path $runtimeRoot 'ollama.exe'
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) { throw 'Select an installed official portable Ollama runtime.' }
$statePath = Join-Path $runtimeRoot "musiyo-model-$Port.json"
$origin = "http://127.0.0.1:$Port"

if ($Action -eq 'health') {
    $version = Invoke-RestMethod -Uri "$origin/api/version" -TimeoutSec 3
    $models = Invoke-RestMethod -Uri "$origin/api/tags" -TimeoutSec 3
    [pscustomobject]@{ version = $version.version; models = @($models.models | Select-Object name, digest) } | ConvertTo-Json -Depth 4
    exit
}
if ($Action -eq 'stop') {
    $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    $ownedProcess = Get-Process -Id $state.process_id -ErrorAction SilentlyContinue
    if ($ownedProcess) {
        if ($ownedProcess.Path -ne $executable -or $ownedProcess.StartTime.ToUniversalTime().ToString('o') -ne $state.started_at) {
            throw 'Process identity changed; no process was stopped.'
        }
        try {
            $loaded = Invoke-RestMethod -Uri "$origin/api/ps" -TimeoutSec 3
            foreach ($model in $loaded.models) {
                $unload = @{ model = $model.name; keep_alive = 0; stream = $false } | ConvertTo-Json
                Invoke-RestMethod -Method Post -Uri "$origin/api/generate" -ContentType 'application/json' -Body $unload -TimeoutSec 10 | Out-Null
            }
        } catch { Write-Output 'Model unload unavailable; stopping the owned runtime.' }
        $ownedChildren = Get-CimInstance Win32_Process -Filter "ParentProcessId=$($ownedProcess.Id)"
        Stop-Process -Id $ownedProcess.Id
        foreach ($child in $ownedChildren) {
            if ($child.ExecutablePath -and $child.ExecutablePath.StartsWith($runtimeRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
                Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
            }
        }
    }
    Write-Output 'Selected local model server stopped; downloaded models are retained.'
    exit
}
try {
    Invoke-RestMethod -Uri "$origin/api/version" -TimeoutSec 2 | Out-Null
    throw 'The selected port already runs a service; leave it intact and select another port.'
} catch {
    if ($_.Exception.Message -like 'The selected port*') { throw }
}
New-Item -ItemType Directory -Path $modelRoot -Force | Out-Null
$variables = @{
    OLLAMA_HOST = "127.0.0.1:$Port"
    OLLAMA_MODELS = $modelRoot
    OLLAMA_NO_CLOUD = '1'
    OLLAMA_DEBUG = 'false'
    OLLAMA_NUM_PARALLEL = '1'
    OLLAMA_MAX_LOADED_MODELS = '2'
    OLLAMA_CONTEXT_LENGTH = '4096'
    OLLAMA_VULKAN = '1'
}
$previousValues = @{}
try {
    foreach ($name in $variables.Keys) {
        $previousValues[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        [Environment]::SetEnvironmentVariable($name, $variables[$name], 'Process')
    }
    $startedProcess = Start-Process -FilePath $executable -ArgumentList 'serve' -WindowStyle Hidden -PassThru
    $state = [pscustomobject]@{ process_id = $startedProcess.Id; executable = $executable;
        started_at = $startedProcess.StartTime.ToUniversalTime().ToString('o'); origin = $origin; models_directory = $modelRoot }
    $state | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding utf8
    $state | ConvertTo-Json
} finally {
    foreach ($name in $variables.Keys) { [Environment]::SetEnvironmentVariable($name, $previousValues[$name], 'Process') }
}
