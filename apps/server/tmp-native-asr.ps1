Add-Type -AssemblyName System.Speech
$wav = 'C:\Users\Administrator\Desktop\Projects\Interview\apps\server\tmp-native-asr-zh.wav'
$rec = [System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers() | Select-Object -First 1
$engine = New-Object System.Speech.Recognition.SpeechRecognitionEngine($rec)
$engine.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))
$engine.SetInputToWaveFile($wav)
$result = $engine.Recognize()
if ($null -eq $result) { Write-Output '__EMPTY__' } else { Write-Output $result.Text }
$engine.Dispose()
