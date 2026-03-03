#!/usr/bin/env bash
set -euo pipefail

# Valida se o repositório está sem marcadores de conflito e com server.py compilável.

if rg -n "<<<<<<<|=======|>>>>>>>" server.py templates >/dev/null 2>&1; then
  echo "Erro: marcadores de conflito encontrados em server.py/templates."
  rg -n "<<<<<<<|=======|>>>>>>>" server.py templates || true
  exit 1
fi

python -m py_compile server.py
DATABASE_URL=${DATABASE_URL:-sqlite:///validate_merge.db} python - <<'PY'
import server
assert hasattr(server, 'app')
print('WSGI app import ok')
PY

python - <<'PY'
from pathlib import Path
p = Path('validate_merge.db')
if p.exists():
    p.unlink()
PY

echo "Validação de merge/deploy concluída com sucesso."
