#!/usr/bin/env bash
set -euo pipefail

# Resolve known recurring PR conflicts by keeping this branch's implementation
# for business-critical files and finishing the merge.

if ! git rev-parse -q --verify MERGE_HEAD >/dev/null; then
  echo "Nenhum merge em andamento (MERGE_HEAD não encontrado)."
  echo "Use: git merge <branch> e execute este script quando houver conflitos."
  exit 1
fi

files=(
  "server.py"
  "templates/week.html"
)

for f in "${files[@]}"; do
  if git ls-files -u -- "$f" | grep -q .; then
    echo "Resolvendo conflito em $f (mantendo versão atual da branch)..."
    git checkout --ours -- "$f"
    git add "$f"
  fi
done

if git ls-files -u | grep -q .; then
  echo "Ainda existem conflitos em outros arquivos. Resolva manualmente e finalize o commit."
  git status --short
  exit 2
fi

echo "Conflitos resolvidos. Finalize com:"
echo "  git commit -m \"Resolve merge conflicts\""


# validação final para evitar deploy com erro de sintaxe/conflito
./scripts/validate_merge_state.sh
