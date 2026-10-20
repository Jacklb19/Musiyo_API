# Generic installed Windows voice only; emits base64 raw PCM16 mono 16 kHz and never writes text or audio to disk.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$stream = New-Object System.IO.MemoryStream
try {
    $voice = $synth.GetInstalledVoices() | Where-Object {
        $_.Enabled -and $_.VoiceInfo.Name -eq $payload.voice -and $_.VoiceInfo.Culture.TwoLetterISOLanguageName -eq 'es'
    } | Select-Object -First 1
    if ($null -eq $voice) { throw 'Selected Spanish voice is not installed' }
    $synth.SelectVoice($voice.VoiceInfo.Name)
    $format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
    $synth.SetOutputToAudioStream($stream, $format)
    $synth.Speak([string]$payload.text)
    if ($stream.Length -gt 3000000) { throw 'Audio exceeds the output limit' }
    [Console]::Out.Write([Convert]::ToBase64String($stream.ToArray()))
}
finally { $synth.Dispose(); $stream.Dispose() }
