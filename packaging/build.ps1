# One-click build: dist\<app> + user manual next to the exe
# (ASCII-only on purpose: Windows PowerShell 5 misreads UTF-8 without BOM)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$name = [string][char]0x58C1 + [char]0x7EB8 + [char]0x89E3 + [char]0x5305   # 壁纸解包

Remove-Item -Recurse -Force "dist", "build" -ErrorAction SilentlyContinue

& ".\.venv\Scripts\pyinstaller.exe" --noconfirm --clean --onedir --windowed `
  --name $name `
  --icon "src\ui\app.ico" `
  --version-file "packaging\version_info.txt" `
  --add-data "src\ui\index.html;ui" `
  --add-data "src\ui\app.ico;ui" `
  --collect-all webview --collect-all pythonnet --collect-all clr_loader `
  --distpath "dist" --workpath "build" `
  "src\app.py"

$out = Join-Path (Resolve-Path "dist") $name
Get-ChildItem "docs" -Filter *.html | Copy-Item -Destination $out -Force
Copy-Item "THIRD_PARTY_NOTICES.md" -Destination $out -Force
Copy-Item "LICENSES" -Destination $out -Recurse -Force

Write-Host "`nDone: $out" -ForegroundColor Green
Get-ChildItem $out | Select-Object Name, Length | Format-Table -AutoSize
