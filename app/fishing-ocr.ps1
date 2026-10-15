$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.Streams.DataWriter, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
$script:asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1
function Wait-Result($operation, $resultType) {
    $task = $script:asTask.MakeGenericMethod($resultType).Invoke($null, @($operation))
    if (-not $task.Wait(6000)) { throw 'OCR operation timed out.' }
    return $task.Result
}
try {
    $language = [Windows.Globalization.Language, Windows.Foundation, ContentType=WindowsRuntime]::new('en-US')
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
    if ($null -eq $engine) { throw 'English Windows OCR is unavailable.' }
    [Console]::Out.WriteLine('{"ready":true}')
    while ($null -ne ($line = [Console]::ReadLine())) {
        $stream = $writer = $bitmap = $null
        try {
            $bytes = [Convert]::FromBase64String($line)
            $stream = [Windows.Storage.Streams.InMemoryRandomAccessStream]::new()
            $writer = [Windows.Storage.Streams.DataWriter]::new($stream)
            $writer.WriteBytes($bytes)
            Wait-Result ($writer.StoreAsync()) ([uint32]) | Out-Null
            $writer.DetachStream() | Out-Null
            $stream.Seek(0)
            $decoder = Wait-Result ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
            $bitmap = Wait-Result ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
            $result = Wait-Result ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
            $lines = @($result.Lines | ForEach-Object { $_.Text })
            $boxes = @($result.Lines | ForEach-Object {
                $rects = @($_.Words | ForEach-Object { $_.BoundingRect })
                $left = ($rects | Measure-Object -Property X -Minimum).Minimum
                $top = ($rects | Measure-Object -Property Y -Minimum).Minimum
                $right = ($rects | ForEach-Object { $_.X + $_.Width } | Measure-Object -Maximum).Maximum
                $bottom = ($rects | ForEach-Object { $_.Y + $_.Height } | Measure-Object -Maximum).Maximum
                ,@($left, $top, ($right-$left), ($bottom-$top))
            })
            $words = @($result.Lines | ForEach-Object { $_.Words } | ForEach-Object {
                $rect = $_.BoundingRect
                @{ text = $_.Text; box = @($rect.X, $rect.Y, $rect.Width, $rect.Height) }
            })
            [Console]::Out.WriteLine((@{ lines = $lines; boxes = $boxes; words = $words } | ConvertTo-Json -Compress -Depth 5))
        } finally {
            if ($null -ne $bitmap) { $bitmap.Dispose() }
            if ($null -ne $writer) { $writer.Dispose() }
            if ($null -ne $stream) { $stream.Dispose() }
        }
    }
} catch {
    [Console]::Out.WriteLine((@{ error = $_.Exception.Message } | ConvertTo-Json -Compress))
    exit 1
}
