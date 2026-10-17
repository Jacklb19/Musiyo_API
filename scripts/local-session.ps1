param(
    [Parameter(Mandatory = $true)][ValidateSet('validate', 'start', 'health', 'stop')][string]$Action,
    [Parameter(Mandatory = $true)][string]$Config
)
$ErrorActionPreference = 'Stop'
$apiRoot = (Resolve-Path (Split-Path $PSScriptRoot -Parent)).Path
$configPath = (Resolve-Path -LiteralPath $Config).Path
$settings = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$fields = @('schema_version', 'database_path', 'storage_root', 'guide_config', 'api_origin', 'web_origin', 'web_root', 'state_directory', 'reuse_existing_api')
foreach ($field in $settings.PSObject.Properties.Name) {
    if ($field -notin $fields) { throw "Unknown session field: $field" }
}
foreach ($field in $fields) {
    if ($field -notin $settings.PSObject.Properties.Name) { throw "Missing session field: $field" }
}
if ($settings.schema_version -ne 1 -or $settings.reuse_existing_api -isnot [bool]) { throw 'Invalid session version or reuse flag.' }
function Resolve-SelectedPath([string]$Value) {
    if (-not [IO.Path]::IsPathRooted($Value)) { $Value = Join-Path (Split-Path $configPath -Parent) $Value }
    return [IO.Path]::GetFullPath($Value)
}
$webRoot = (Resolve-Path -LiteralPath (Resolve-SelectedPath $settings.web_root)).Path
$database = (Resolve-Path -LiteralPath (Resolve-SelectedPath $settings.database_path)).Path
$storage = (Resolve-Path -LiteralPath (Resolve-SelectedPath $settings.storage_root)).Path
$guide = (Resolve-Path -LiteralPath (Resolve-SelectedPath $settings.guide_config)).Path
$stateDirectory = Resolve-SelectedPath $settings.state_directory
foreach ($path in @($configPath, $database, $storage, $guide, $stateDirectory)) {
    foreach ($repo in @($apiRoot, $webRoot)) {
        if ($path.Equals($repo, [StringComparison]::OrdinalIgnoreCase) -or $path.StartsWith($repo + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Session settings, data, resources and process state must stay outside repositories.'
        }
    }
}
if (-not (Test-Path -LiteralPath $database -PathType Leaf) -or -not (Test-Path -LiteralPath $storage -PathType Container)) { throw 'Select an existing database and private storage directory.' }
$apiUri = [Uri]$settings.api_origin
$webUri = [Uri]$settings.web_origin
foreach ($uri in @($apiUri, $webUri)) {
    if (-not $uri.IsAbsoluteUri -or $uri.Scheme -ne 'http' -or $uri.Host -notin @('localhost', '127.0.0.1') -or $uri.Port -lt 1024 -or $uri.AbsolutePath -ne '/' -or $uri.Query -or $uri.Fragment -or $uri.UserInfo) {
        throw 'Local session origins must have an explicit loopback HTTP port and no path or credentials.'
    }
}
if ($apiUri.Port -eq $webUri.Port) { throw 'API and Web require distinct ports.' }
$apiOrigin = $apiUri.GetLeftPart([UriPartial]::Authority)
$webOrigin = $webUri.GetLeftPart([UriPartial]::Authority)
$python = Join-Path $apiRoot '.venv/Scripts/python.exe'
$node = (Get-Command node -ErrorAction Stop).Source
# Pass source as a module, preserving arguments in Windows PowerShell 5.1.
Push-Location -LiteralPath $apiRoot
try {
    & $python -m scripts.inspect_session guide $guide
    if ($LASTEXITCODE -ne 0) { throw 'Invalid selected guide configuration.' }
    $selectedSlugs = @(& $python -m scripts.inspect_session database $database | ConvertFrom-Json | ForEach-Object { $_ })
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read the selected publication database.' }
} finally { Pop-Location }
$vite = Join-Path $webRoot 'node_modules/vite/bin/vite.js'
if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath $vite)) { throw 'Install the existing repository dependencies first.' }
$guideSettings = Get-Content -LiteralPath $guide -Raw | ConvertFrom-Json
if ($guideSettings.generation -ne 'local') { throw 'This session requires the accepted extractive generation adapter.' }
$statePath = Join-Path $stateDirectory 'session-state.json'
# Bind state to the selected configuration, without storing secrets or question text.
$hasher = [Security.Cryptography.SHA256]::Create()
try { $configHash = [BitConverter]::ToString($hasher.ComputeHash([IO.File]::ReadAllBytes($configPath))).Replace('-', '') }
finally { $hasher.Dispose() }
function Get-Listener([int]$Port) {
    return @([Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners() | Where-Object { $_.Port -eq $Port })
}
function Get-Identity([int]$ProcessId) {
    for ($identityAttempt = 0; $identityAttempt -lt 20; $identityAttempt++) {
        $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
        if (-not $process) { return $null }
        if ($process.Path) { break }
        Start-Sleep -Milliseconds 50
    }
    if (-not $process.Path) { throw 'Cannot record the executable identity of a launched process.' }
    return [pscustomobject]@{ process_id = $process.Id; executable = $process.Path; started_at = $process.StartTime.ToUniversalTime().ToString('o') }
}
function Assert-Identity($Identity) {
    $actual = Get-Identity $Identity.process_id
    if ($actual -and ($actual.executable -ne $Identity.executable -or ([DateTimeOffset]$actual.started_at).UtcDateTime -ne ([DateTimeOffset]$Identity.started_at).UtcDateTime)) {
        throw 'Process identity changed; no process was stopped.'
    }
    return $actual
}
function Get-OwnedTree([int]$RootId) {
    $ids = @($RootId)
    $processes = @(Get-CimInstance Win32_Process)
    for ($i = 0; $i -lt $ids.Count; $i++) {
        $ids += @($processes | Where-Object { $_.ParentProcessId -eq $ids[$i] } | ForEach-Object { [int]$_.ProcessId })
    }
    return @($ids | ForEach-Object { Get-Identity $_ } | Where-Object { $_ })
}
function Read-State {
    $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    if ($state.config_sha256 -ne $configHash) { throw 'The configuration differs from the recorded session. Use its original configuration.' }
    return $state
}
function Stop-Owned($State) {
    # Validate every saved identity before terminating anything. Reused API is never owned.
    foreach ($identity in @($State.owned_processes)) { Assert-Identity $identity | Out-Null }
    $identities = @($State.owned_processes)
    [Array]::Reverse($identities)
    foreach ($identity in $identities) {
        if (Assert-Identity $identity) {
            try { Stop-Process -Id $identity.process_id -ErrorAction Stop }
            catch {
                # A child may exit when its owned parent is stopped between these checks.
                if (Get-Process -Id $identity.process_id -ErrorAction SilentlyContinue) { throw }
            }
        }
    }
}
function Test-SessionHealth {
    foreach ($origin in @($apiOrigin, $webOrigin)) {
        foreach ($endpoint in @('health', 'ready')) {
            $result = Invoke-RestMethod -Uri "$origin/api/v1/$endpoint" -TimeoutSec 5
            if ($result.status -ne 'ok') { throw "Readiness failed: $origin" }
        }
    }
    $direct = @(Invoke-RestMethod -Uri "$apiOrigin/api/v1/elements" -TimeoutSec 5 | ForEach-Object { $_ })
    $proxied = @(Invoke-RestMethod -Uri "$webOrigin/api/v1/elements" -TimeoutSec 5 | ForEach-Object { $_ })
    if ((($direct.slug | Sort-Object) -join ',') -cne (($proxied.slug | Sort-Object) -join ',')) { throw 'Web proxy and API catalogue differ.' }
    if ((($direct.slug | Sort-Object) -join ',') -cne (($selectedSlugs | Sort-Object) -join ',')) { throw 'API catalogue differs from the selected database. Check the existing API environment and current publication permissions.' }
    $tour = Invoke-RestMethod -Uri "$webOrigin/api/v1/tours/museum-main?schema_version=1" -TimeoutSec 5
    [pscustomobject]@{ api_base = "$apiOrigin/api/v1"; web = $webOrigin; elements = $direct.Count; tour = $tour.tour.key; guide_health = 'Not inferred from guide.available; submit a question separately.' } | ConvertTo-Json
}
if ($Action -eq 'validate') {
    [pscustomobject]@{ api_base = "$apiOrigin/api/v1"; web = $webOrigin; database = $database; storage = $storage; generation = $guideSettings.generation; embeddings = $guideSettings.embeddings; reuse_existing_api = $settings.reuse_existing_api } | ConvertTo-Json
    exit
}
if ($Action -eq 'stop') {
    $state = Read-State
    Stop-Owned $state
    Remove-Item -LiteralPath $statePath
    Write-Output 'Owned session processes stopped. Reused API, models and all data retained.'
    exit
}
if ($Action -eq 'health') {
    $state = Read-State
    foreach ($identity in @($state.owned_processes)) {
        if (-not (Assert-Identity $identity)) { throw 'An owned process exited; inspect the session logs.' }
    }
    Test-SessionHealth
    exit
}
if (Test-Path -LiteralPath $statePath) { throw 'Session state exists. Run health or stop before starting again.' }
if (@(Get-Listener $webUri.Port).Count) { throw 'Web port is occupied; no service was changed.' }
$apiBusy = @(Get-Listener $apiUri.Port).Count -gt 0
if ($apiBusy -and -not $settings.reuse_existing_api) { throw 'API port is occupied. Reuse requires explicit configuration after verifying the existing service.' }
if ($settings.reuse_existing_api -and -not $apiBusy) { throw 'Expected existing API is absent. Review configuration before starting an owned API.' }
if ($apiBusy) {
    if ((Invoke-RestMethod "$apiOrigin/api/v1/ready" -TimeoutSec 5).status -ne 'ok') { throw 'Existing API is not ready.' }
}
# Match the Web package's existing predev step without copying a Unity player.
& $node (Join-Path $webRoot 'scripts/sync-model-decoders.mjs')
if ($LASTEXITCODE -ne 0) { throw 'Web decoder preparation failed.' }
New-Item -ItemType Directory -Path $stateDirectory -Force | Out-Null
$owned = @()
$roots = @()
$state = [pscustomobject]@{ config_sha256 = $configHash; api_reused = $apiBusy; owned_processes = @() }
try {
    if (-not $apiBusy) {
        # Reuse the established foreground API launcher; no migration, import or indexing.
        $launcher = Join-Path $PSScriptRoot 'start-local-api.ps1'
        $command = "& '$($launcher.Replace("'", "''"))' -DatabasePath '$($database.Replace("'", "''"))' -StorageRoot '$($storage.Replace("'", "''"))' -GuideConfig '$($guide.Replace("'", "''"))' -WebOrigin '$webOrigin' -Port $($apiUri.Port)"
        $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
        $shell = (Get-Process -Id $PID).Path
        $process = Start-Process $shell -ArgumentList @('-NoProfile', '-EncodedCommand', $encoded) -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $stateDirectory 'api.log') -RedirectStandardError (Join-Path $stateDirectory 'api-error.log')
        $roots += $process.Id
        $owned += Get-Identity $process.Id
    }
    $previousTarget = $env:MUSIYO_DEV_API_TARGET
    try {
        # Vite process environment wins over stale .env.local; nothing is baked into clients.
        $env:MUSIYO_DEV_API_TARGET = $apiOrigin
        $process = Start-Process $node -ArgumentList @(('"' + $vite + '"'), '--host', $webUri.Host, '--port', $webUri.Port, '--strictPort') -WorkingDirectory $webRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $stateDirectory 'web.log') -RedirectStandardError (Join-Path $stateDirectory 'web-error.log')
        $roots += $process.Id
        $owned += Get-Identity $process.Id
    } finally { $env:MUSIYO_DEV_API_TARGET = $previousTarget }
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        if (@(Get-Listener $apiUri.Port).Count -and @(Get-Listener $webUri.Port).Count) { break }
        Start-Sleep -Milliseconds 500
    }
    foreach ($root in $roots) { $owned += Get-OwnedTree $root }
    $state.owned_processes = @($owned | Group-Object process_id | ForEach-Object { $_.Group[-1] })
    $state | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $statePath -Encoding utf8
    Test-SessionHealth
} catch {
    foreach ($root in $roots) { $owned += Get-OwnedTree $root }
    $state.owned_processes = @($owned | Group-Object process_id | ForEach-Object { $_.Group[-1] })
    $state | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $statePath -Encoding utf8
    Stop-Owned $state
    Remove-Item -LiteralPath $statePath
    throw
}
