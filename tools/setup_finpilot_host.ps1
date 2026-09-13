# Run PowerShell as Administrator, then execute this file.
$hosts = "$env:SystemRoot\System32\drivers\etc\hosts"
$line = "127.0.0.1 finpilot"
if (-not (Select-String -Path $hosts -Pattern "^127\.0\.0\.1\s+finpilot$" -Quiet)) {
  Add-Content -Path $hosts -Value $line
  Write-Host "Added http://finpilot"
} else {
  Write-Host "http://finpilot is already configured"
}
