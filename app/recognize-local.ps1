# Installed Windows Spanish dictation only; reads a validated WAV from stdin, keeps it in memory
# and emits the recognised text as base64 UTF-8. Nothing is written to disk. Optional generic
# question frames and published catalogue titles bias recognition; free dictation stays loaded.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
$culture = [Globalization.CultureInfo]::new([string]$payload.culture)
$recognizer = [System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers() |
    Where-Object { $_.Culture.Name -eq $culture.Name } | Select-Object -First 1
if ($null -eq $recognizer) { throw 'Selected Spanish recognizer is not installed' }
$engine = New-Object System.Speech.Recognition.SpeechRecognitionEngine($recognizer)
$stream = New-Object System.IO.MemoryStream(, [Convert]::FromBase64String([string]$payload.audio))
function New-Builder($Content, [int]$Minimum = 1) {
    $builder = New-Object System.Speech.Recognition.GrammarBuilder($Content, $Minimum, 1)
    $builder.Culture = $culture
    return $builder
}
function New-Choices([string[]]$Values) {
    New-Object System.Speech.Recognition.GrammarBuilder((New-Object System.Speech.Recognition.Choices(, $Values)))
}
try {
    $engine.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))
    $free = New-Object System.Speech.Recognition.GrammarBuilder
    $free.AppendDictation()
    $frameValues = @($payload.frames | ForEach-Object { [string]$_ })
    $termValues = @($payload.vocabulary | ForEach-Object { [string]$_ })
    if ($frameValues.Count) {
        $framed = New-Builder (New-Choices $frameValues)
        $framed.Append($free)
        $engine.LoadGrammar((New-Object System.Speech.Recognition.Grammar($framed)))
    }
    if ($termValues.Count) {
        $named = New-Builder $free 0
        if ($frameValues.Count) { $named = New-Builder (New-Choices $frameValues) 0; $named.Append((New-Builder $free 0)) }
        $named.Append((New-Choices $termValues))
        $named.Append((New-Builder $free 0))
        $engine.LoadGrammar((New-Object System.Speech.Recognition.Grammar($named)))
    }
    $engine.SetInputToWaveStream($stream)
    $engine.InitialSilenceTimeout = [TimeSpan]::FromSeconds(3)
    $parts = New-Object System.Collections.Generic.List[string]
    for ($i = 0; $i -lt 40; $i++) {
        # Once the stream is consumed the engine reports missing input instead of returning null.
        try { $result = $engine.Recognize() } catch [InvalidOperationException] { break }
        if ($null -eq $result) { break }
        $parts.Add($result.Text)
    }
    if ($parts.Count -eq 0) { throw 'No speech recognised' }
    [Console]::Out.Write([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($parts -join ' '))))
}
finally { $engine.Dispose(); $stream.Dispose() }
