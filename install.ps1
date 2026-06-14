# Install codex-ultracode-kit into $CODEX_HOME (default ~/.codex) on Windows.
# Validates first, then installs. Pass-through flags: --dry-run, --uninstall, --codex-home DIR
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Get-Command python3 -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command python -ErrorAction SilentlyContinue }
if (-not $py) { Write-Error "python3/python not found"; exit 127 }
& $py.Source "$here/scripts/check_package.py"
& $py.Source "$here/scripts/install.py" @args
