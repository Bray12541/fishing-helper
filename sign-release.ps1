$ErrorActionPreference='Stop'
# Configure repository secrets WINDOWS_SIGN_PFX_BASE64 and WINDOWS_SIGN_PASSWORD.
# A production hardware-backed signing service can replace this step.
if (-not $env:WINDOWS_SIGN_PFX_BASE64) { Write-Output 'No publisher certificate configured; release remains unsigned.'; exit 0 }
$certificatePath=Join-Path $env:RUNNER_TEMP 'fishing-signing.pfx'
$certificate=$null
try {
    [IO.File]::WriteAllBytes($certificatePath,[Convert]::FromBase64String($env:WINDOWS_SIGN_PFX_BASE64))
    $certificate=[Security.Cryptography.X509Certificates.X509Certificate2]::new($certificatePath,$env:WINDOWS_SIGN_PASSWORD,[Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet)
    $signature=Set-AuthenticodeSignature -LiteralPath 'dist/Fishing Helper.exe' -Certificate $certificate -HashAlgorithm SHA256 -TimestampServer 'http://timestamp.digicert.com'
    if ($signature.Status -ne 'Valid') { throw 'Publisher signing failed Windows validation' }
    "FISHING_SIGNER=$($certificate.Thumbprint)" | Out-File -FilePath $env:GITHUB_ENV -Encoding utf8 -Append
} finally {
    if ($certificate) { $certificate.Dispose() }
    Remove-Item -LiteralPath $certificatePath -Force -ErrorAction SilentlyContinue
}
