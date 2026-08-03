<#
.SYNOPSIS
    Create (or remove) a desktop shortcut that launches the MNQ Intelligence
    assistant with one click, using the premium glass application icon.

.DESCRIPTION
    Points a Windows .lnk at the venv's pythonw.exe running
    "-m tools.launch_desktop" (windowless), with the repository as the working
    directory and assets/branding/mnq_intelligence.ico as the icon. Delegates
    to the existing startup architecture: no live trading, no broker execution.

    Requires no administrator privileges (writes only to the current user's
    Desktop and Start Menu). Idempotent: re-running updates the existing
    shortcut in place. Validates the result before reporting success.

.PARAMETER StartMenu
    Also create a Start Menu shortcut under "MNQ Intelligence".

.PARAMETER Uninstall
    Remove the desktop (and Start Menu) shortcut(s) instead of creating them.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\install_desktop_shortcut.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\install_desktop_shortcut.ps1 -StartMenu
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\install_desktop_shortcut.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$StartMenu,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

# tools\.. = repository root (quoted paths throughout).
$RepoRoot     = Split-Path -Parent $PSScriptRoot
$PythonW      = Join-Path $RepoRoot '.venv\Scripts\pythonw.exe'
$IconPath     = Join-Path $RepoRoot 'assets\branding\mnq_intelligence.ico'
$ShortcutName = 'MNQ Intelligence.lnk'
$Arguments    = '-m tools.launch_desktop'

$DesktopDir      = [Environment]::GetFolderPath('Desktop')
$StartMenuFolder = Join-Path ([Environment]::GetFolderPath('Programs')) 'MNQ Intelligence'

function Remove-AppShortcut {
    param([string]$Directory)
    $path = Join-Path $Directory $ShortcutName
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Force
        Write-Host "  removed: $path"
        return $true
    }
    Write-Host "  not present: $path"
    return $false
}

function New-AppShortcut {
    param([string]$Directory)
    if (-not (Test-Path -LiteralPath $Directory)) {
        New-Item -ItemType Directory -Path $Directory -Force | Out-Null
    }
    $path = Join-Path $Directory $ShortcutName
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($path)
    $shortcut.TargetPath       = $PythonW
    $shortcut.Arguments        = $Arguments
    $shortcut.WorkingDirectory = $RepoRoot
    $shortcut.IconLocation     = "$IconPath,0"
    $shortcut.Description       = 'MNQ Intelligence - delayed-paper order-flow assistant (no live trading)'
    $shortcut.WindowStyle       = 1
    $shortcut.Save()

    # Validate: the file exists and points where we intended.
    if (-not (Test-Path -LiteralPath $path)) {
        throw "shortcut was not created: $path"
    }
    $check = $shell.CreateShortcut($path)
    if ($check.TargetPath -ne $PythonW) {
        throw "shortcut target mismatch: '$($check.TargetPath)' != '$PythonW'"
    }
    Write-Host "  created: $path"
    return $path
}

Write-Host "MNQ Intelligence desktop shortcut installer"
Write-Host "  repository: $RepoRoot"

if ($Uninstall) {
    Write-Host "Removing shortcuts..."
    $removed = $false
    if (Remove-AppShortcut -Directory $DesktopDir) { $removed = $true }
    if (Test-Path -LiteralPath $StartMenuFolder) {
        if (Remove-AppShortcut -Directory $StartMenuFolder) { $removed = $true }
        if (-not (Get-ChildItem -LiteralPath $StartMenuFolder -Force -ErrorAction SilentlyContinue)) {
            Remove-Item -LiteralPath $StartMenuFolder -Force
        }
    }
    if ($removed) { Write-Host "Uninstall complete." } else { Write-Host "Nothing to remove." }
    exit 0
}

# Install: validate prerequisites first so we never create a broken shortcut.
if (-not (Test-Path -LiteralPath $PythonW)) {
    Write-Error "Python launcher not found: $PythonW`nCreate the .venv and install dependencies first."
    exit 2
}
if (-not (Test-Path -LiteralPath $IconPath)) {
    Write-Warning "Icon not found: $IconPath"
    Write-Warning "Generate it: .venv\Scripts\python.exe -m tools.generate_app_icon"
    Write-Error "Refusing to create a shortcut without its icon."
    exit 2
}

Write-Host "Creating shortcuts..."
try {
    New-AppShortcut -Directory $DesktopDir | Out-Null
    if ($StartMenu) { New-AppShortcut -Directory $StartMenuFolder | Out-Null }
    Write-Host "Success. Double-click 'MNQ Intelligence' on your Desktop to launch (delayed-paper mode)."
    exit 0
} catch {
    Write-Error "Failed to create shortcut: $($_.Exception.Message)"
    exit 1
}
