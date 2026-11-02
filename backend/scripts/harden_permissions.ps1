# Restrict the dashboard's secrets and data to the current Windows user (and SYSTEM).
# Run from the backend folder:  powershell -ExecutionPolicy Bypass -File scripts\harden_permissions.ps1
# Removes inherited permissions on: .env, data\ (SQLite database) and backups\.
$ErrorActionPreference = "Stop"
$me = "$env:USERDOMAIN\$env:USERNAME"
$targets = @(".env", "data", "backups") | Where-Object { Test-Path $_ }
foreach ($t in $targets) {
    $isDir = (Get-Item $t) -is [System.IO.DirectoryInfo]
    $grant = if ($isDir) { "(OI)(CI)F" } else { "F" }
    icacls $t /inheritance:r /grant:r "${me}:$grant" "SYSTEM:$grant" | Out-Null
    Write-Host "Restricted $t to $me and SYSTEM"
}
Write-Host "Done. Check with: icacls .env"
