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
    if ($updatePlan.health_token) {
        $healthPath=[IO.Path]::GetFullPath([string]$updatePlan.health_path)
        if ([IO.Path]::GetDirectoryName($healthPath) -ne $stagePath -or [string]$updatePlan.health_token -notmatch '^[a-f0-9]{64}$') { throw 'Invalid startup health plan' }
        $launchArguments='--update-health "'+$healthPath+'" --update-token '+[string]$updatePlan.health_token
        $newProcess=Start-Process -FilePath $targetPath -WorkingDirectory ([IO.Path]::GetDirectoryName($targetPath)) -ArgumentList $launchArguments -PassThru
        $healthDeadline=[DateTime]::UtcNow.AddSeconds(90)
        $healthy=$false
        while ([DateTime]::UtcNow -lt $healthDeadline) {
            if (Test-Path -LiteralPath $healthPath) {
                try {
                    $health=Get-Content -LiteralPath $healthPath -Raw | ConvertFrom-Json
                    if ($health.token -eq $updatePlan.health_token -and $health.version -eq $updatePlan.version -and -not $newProcess.HasExited) { $healthy=$true; break }
                } catch { }
            }
            if ($newProcess.HasExited) { break }
            Start-Sleep -Milliseconds 200
        }
        if (-not $healthy) {
            # Stop only the replacement process we started, including its one-file child.
            if (-not $newProcess.HasExited) { & "$env:SystemRoot\System32\taskkill.exe" /PID $newProcess.Id /T /F 2>$null | Out-Null; $newProcess.WaitForExit(10000) | Out-Null }
            throw 'The replacement did not acknowledge startup'
        }
    } else {
        Start-Process -FilePath $targetPath -WorkingDirectory ([IO.Path]::GetDirectoryName($targetPath)) | Out-Null
    }
    @{ok=$true;message='Update installed and app restarted'} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stagePath 'result.json') -Encoding utf8
} catch {
    if ($installed -and $backupPath -and (Test-Path -LiteralPath $backupPath)) {
        try {
            $rollbackPending=$targetPath+'.rollback-pending'
            Copy-Item -LiteralPath $backupPath -Destination $rollbackPending -Force
            $restoreDeadline=[DateTime]::UtcNow.AddSeconds(15)
            while ($true) {
                try { [IO.File]::Replace($rollbackPending,$targetPath,$targetPath+'.failed',$true); Remove-Item -LiteralPath ($targetPath+'.failed') -Force -ErrorAction SilentlyContinue; break }
                catch [IO.IOException] {
                    if ([DateTime]::UtcNow -ge $restoreDeadline) { throw }
                    Start-Sleep -Milliseconds 200
                }
            }
            $installed=$false
        } catch { }
    }
    $failureText=if ($installed) { 'Update failed and automatic restore could not finish. Restore the .previous backup manually.' } else { 'The update could not finish. The previous app was kept. Reopen the app and retry.' }
    @{ok=$false;message=$failureText} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path ([IO.Path]::GetDirectoryName($PlanPath)) 'result.json') -Encoding utf8
    if (-not $installed -and $updatePlan -and (Test-Path -LiteralPath ([string]$updatePlan.target))) {
        try { Start-Process -FilePath ([string]$updatePlan.target) -WorkingDirectory ([IO.Path]::GetDirectoryName([string]$updatePlan.target)) | Out-Null } catch { }
    }
}
