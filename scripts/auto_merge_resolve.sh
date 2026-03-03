#!/usr/bin/env bash
set -euo pipefail

# Uso:
#   ./scripts/auto_merge_resolve.sh <branch-origem>
# Exemplo:
#   ./scripts/auto_merge_resolve.sh origin/main

if [ "${1:-}" = "" ]; then
  echo "Uso: $0 <branch-origem>"
  exit 1
fi

SOURCE_BRANCH="$1"

# Inicia merge (pode falhar com conflitos, o script trata em seguida)
set +e
git merge "$SOURCE_BRANCH"
merge_exit=$?
set -e

if [ "$merge_exit" -eq 0 ]; then
  echo "Merge concluído sem conflitos."
  exit 0
fi

if ! git rev-parse -q --verify MERGE_HEAD >/dev/null; then
  echo "Merge falhou por motivo diferente de conflito."
  exit "$merge_exit"
fi

echo "Conflitos detectados. Aplicando resolução automática para arquivos críticos..."

# Mantém a versão atual da branch para os arquivos mais sensíveis do projeto.
for f in server.py templates/week.html templates/week.htlm; do
  if git ls-files -u -- "$f" | grep -q .; then
    echo " - resolvendo $f com --ours"
    git checkout --ours -- "$f"
    git add "$f"
  fi
done

# Se ainda restar conflito, interrompe para revisão manual.
if git ls-files -u | grep -q .; then
  echo "Ainda existem conflitos em outros arquivos:" 
  git status --short
  echo "Resolva manualmente e rode: git commit"
  exit 2
fi

git commit -m "Resolve merge conflicts automatically (server/week templates)"
echo "Merge finalizado e commitado com sucesso."


# validação final para evitar deploy com erro de sintaxe/conflito
./scripts/validate_merge_state.sh
