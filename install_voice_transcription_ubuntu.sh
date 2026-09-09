#!/usr/bin/env bash
set -euo pipefail

QUEUE_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$QUEUE_SCRIPT_DIR"

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-voice.txt

echo "Расшифровка голосовых установлена. При первом голосовом модель загрузится автоматически."
