param([switch]$Console)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$venv = Join-Path $root '.venv'
$python = Join-Path $venv 'Scripts\python.exe'
$icon = (Resolve-Path -LiteralPath (Join-Path $root 'work\cloudian.ico')).Path
$darkIcon = (Resolve-Path -LiteralPath (Join-Path $root 'work\cloudian-dark.ico')).Path
$appName = (Get-Content -LiteralPath (Join-Path $root 'app-name.txt') -Encoding UTF8 -Raw).Trim()
$subsystemOption = '--windowed'
if ($Console) {
    $appName += ' - CLI'
    $subsystemOption = '--console'
}
$source = Join-Path $root 'src\mailbot.py'
$dist = Join-Path $root 'dist'
$build = Join-Path $root 'build'

if (-not (Test-Path -LiteralPath $python)) {
    py -3.15 -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'Install 64-bit Python 3.15 for Windows and try again.' }
}
& $python -m pip install -r (Join-Path $root 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Could not install build dependencies.' }

New-Item -ItemType Directory -Path $build -Force | Out-Null
$options = @(
    '--noconfirm', '--onefile', $subsystemOption, '--noupx',
    '--name', $appName, '--distpath', $dist, '--workpath', $build, '--specpath', $build,
    '--icon', $icon, '--add-data', "${icon}:.", '--add-data', "${darkIcon}:.",
    '--hidden-import', 'pystray._win32',
    '--exclude-module', 'PIL.AvifImagePlugin', '--exclude-module', 'PIL._avif',
    '--exclude-module', 'PIL.WebPImagePlugin', '--exclude-module', 'PIL._webp',
    '--exclude-module', 'PIL.ImageCms', '--exclude-module', 'PIL._imagingcms',
    $source
)
& $python -m PyInstaller @options
if ($LASTEXITCODE -ne 0) { throw 'Could not build the executable.' }
Write-Host (Join-Path $dist "$appName.exe")
