#!/usr/bin/env bash
set -euo pipefail

ROOT="${1:-.}"
cd "$ROOT"

fail=0

echo "[1/5] Runtime/private files"
for pattern in '*.db' '*.db-*' '*.sqlite' '*.sqlite3' '*.session' '*.pem' '*.key' '*.p12' '*.pfx' '.env' 'secrets.env'; do
  while IFS= read -r -d '' f; do
    echo "BLOCK: private/runtime file: $f"
    fail=1
  done < <(find . -type f -name "$pattern" -print0 2>/dev/null)
done

for dir in whatsapp-auth .wwebjs_auth .wwebjs_cache node_modules media uploads backups logs __pycache__ .pytest_cache; do
  if find . -type d -name "$dir" -print -quit 2>/dev/null | grep -q .; then
    echo "BLOCK: runtime directory found: $dir"
    fail=1
  fi
done

echo "[2/5] Obvious credential literals"
# Conservative scan: reports likely literal credentials while allowing getenv/process.env references.
if grep -RInE --exclude-dir=.git --exclude='verify_snapshot.sh' \
  '((api[_-]?hash|bot[_-]?token|webhook[_-]?token|admin[_-]?(secret|password)|password|passwd)[[:space:]]*[:=][[:space:]]*["'\''`][^"'\''`$]{8,}["'\''`])|(https://api\.telegram\.org/bot[0-9]+:[A-Za-z0-9_-]+)' . 2>/dev/null; then
  echo "BLOCK: possible hard-coded credential shown above"
  fail=1
fi

echo "[3/5] Private infrastructure markers"
if grep -RInE --exclude-dir=.git --exclude='CHANGELOG.md' --exclude='README.md' --exclude='verify_snapshot.sh' \
  '100\.98\.33\.61|manager-H510M-HDV-M-2-SE|https://docs\.google\.com/spreadsheets/d/[A-Za-z0-9_-]{20,}' . 2>/dev/null; then
  echo "BLOCK: private infrastructure or real Google Sheet ID shown above"
  fail=1
fi

echo "[4/5] Syntax checks"
while IFS= read -r -d '' f; do
  python3 -c 'import ast, pathlib, sys; p=pathlib.Path(sys.argv[1]); ast.parse(p.read_text(encoding="utf-8"), filename=str(p))' "$f"
done < <(find . -type f -name '*.py' -print0)

if command -v node >/dev/null 2>&1; then
  while IFS= read -r -d '' f; do
    node --check "$f" >/dev/null
  done < <(find . -type f -name '*.js' -print0)
else
  echo "WARN: node not installed; JS syntax check skipped"
fi

echo "[5/5] Result"
if [[ "$fail" -ne 0 ]]; then
  echo "Snapshot is NOT ready for public GitHub. Fix the BLOCK items first."
  exit 2
fi

echo "Snapshot passed the basic public-release checks."
