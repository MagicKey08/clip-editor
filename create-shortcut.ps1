# Creates a "ClipEditor" shortcut (with icon) on the desktop and in the Start menu.
# Run:  right-click -> "Run with PowerShell"   or   powershell -ExecutionPolicy Bypass -File create-shortcut.ps1
$root = $PSScriptRoot
$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
    $py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
    if ($py) { $pythonw = Join-Path (Split-Path $py) "pythonw.exe" }
}
if (-not $pythonw -or -not (Test-Path $pythonw)) {
    Write-Host "pythonw.exe not found - please install Python 3 (python.org) and try again." -ForegroundColor Red
    exit 1
}
$ws = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
    $lnk = $ws.CreateShortcut((Join-Path $dir "ClipEditor.lnk"))
    $lnk.TargetPath = $pythonw
    $lnk.Arguments = "`"$root\ClipEditor.pyw`""
    $lnk.WorkingDirectory = $root
    $lnk.IconLocation = "$root\app.ico,0"
    $lnk.Description = "ClipEditor - edit multi-track game clips"
    $lnk.Save()
    Write-Host "Created $(Join-Path $dir 'ClipEditor.lnk')"
}
