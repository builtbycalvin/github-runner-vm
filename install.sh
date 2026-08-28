#!/bin/bash
set -euo pipefail
command -v python3 >/dev/null 2>&1 || { echo 'Install Python 3.10 or newer before running setup.' >&2; exit 2; }
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$script_dir/ci_vm.py" --install "$@"
