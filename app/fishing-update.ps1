param([Parameter(Mandatory=$true)][string]$PlanPath)
$ErrorActionPreference='Stop'
$updatePlan=$null
$backupPath=$null
$installed=$false
function Get-UpdateHash([string]$LiteralPath) {
    $hash=[System.Security.Cryptography.SHA256]::Create()
    $stream=[IO.File]::OpenRead($LiteralPath)
    try { return [BitConverter]::ToString($hash.ComputeHash($stream)).Replace('-','').ToLowerInvariant() }
    finally { $stream.Dispose(); $hash.Dispose() }
}
try {
    $updatePlan=Get-Content -LiteralPath $PlanPath -Raw | ConvertFrom-Json
    $sourcePath=[IO.Path]::GetFullPath([string]$updatePlan.source)
    $targetPath=[IO.Path]::GetFullPath([string]$updatePlan.target)
    $stagePath=[IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($PlanPath))
    if ([IO.Path]::GetDirectoryName($sourcePath) -ne $stagePath -or [IO.Path]::GetExtension($targetPath) -ne '.exe') { throw 'Invalid update plan' }
    $sourceItem=Get-Item -LiteralPath $sourcePath
    if ($sourceItem.Length -ne [long]$updatePlan.size -or (Get-UpdateHash $sourcePath) -ne [string]$updatePlan.sha256) { throw 'Update checksum mismatch' }
    $oldProcess=Get-Process -Id ([int]$updatePlan.pid) -ErrorAction SilentlyContinue
    if ($oldProcess -and -not $oldProcess.WaitForExit(45000)) { throw 'The running app did not close; no files were changed' }
    $backupPath=$targetPath+'.previous'
    $pendingPath=$targetPath+'.pending'
    Copy-Item -LiteralPath $sourcePath -Destination $pendingPath -Force
    if ((Get-UpdateHash $pendingPath) -ne [string]$updatePlan.sha256) { throw 'Staged copy checksum mismatch' }
    # The one-file bootloader can hold the EXE briefly after Python exits.
    $replaceDeadline=[DateTime]::UtcNow.AddSeconds(15)
    while ($true) {
        try { [IO.File]::Replace($pendingPath,$targetPath,$backupPath,$true); break }
        catch [IO.IOException] {
            if ([DateTime]::UtcNow -ge $replaceDeadline) { throw }
            Start-Sleep -Milliseconds 200
        }
    }
    $installed=$true
    Start-Process -FilePath $targetPath -WorkingDirectory ([IO.Path]::GetDirectoryName($targetPath)) | Out-Null
    @{ok=$true;message='Update installed and app restarted'} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stagePath 'result.json') -Encoding utf8
} catch {
    if ($installed -and $backupPath -and (Test-Path -LiteralPath $backupPath)) {
        try { Copy-Item -LiteralPath $backupPath -Destination $targetPath -Force } catch { }
    }
    $failureText='The update could not finish. The previous app was kept. Reopen the app and retry.'
    @{ok=$false;message=$failureText} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path ([IO.Path]::GetDirectoryName($PlanPath)) 'result.json') -Encoding utf8
    if ($updatePlan -and (Test-Path -LiteralPath ([string]$updatePlan.target))) {
        try { Start-Process -FilePath ([string]$updatePlan.target) -WorkingDirectory ([IO.Path]::GetDirectoryName([string]$updatePlan.target)) | Out-Null } catch { }
    }
}
