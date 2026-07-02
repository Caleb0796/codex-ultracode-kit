# Install codex-ultracode-kit into $CODEX_HOME (default ~/.codex) on Windows.
# Validates first, then installs. Pass-through flags: --dry-run, --uninstall, --codex-home DIR
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Get-Command python3 -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command python -ErrorAction SilentlyContinue }
if (-not $py) { Write-Error "python3/python not found"; exit 127 }
# $ErrorActionPreference does NOT stop on native-command nonzero exits — check explicitly,
# or a failed validation would install anyway.
& $py.Source "$here/scripts/check_package.py"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $py.Source "$here/scripts/install.py" @args
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
