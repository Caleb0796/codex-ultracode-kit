#!/usr/bin/env bash
# Install codex-ultracode-kit into $CODEX_HOME (default ~/.codex).
# Validates the package first, then installs the skill + skeptic role.
# Passes flags through: --dry-run, --uninstall, --codex-home DIR
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
command -v python3 >/dev/null 2>&1 || { echo "python3 not found"; exit 127; }
python3 "$HERE/scripts/check_package.py"
python3 "$HERE/scripts/install.py" "$@"
