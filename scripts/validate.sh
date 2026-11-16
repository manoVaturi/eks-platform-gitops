#!/usr/bin/env bash
# Local + CI validation. Needs: yamllint, helm, python3 with pyyaml; kubeconform is optional.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== yamllint"
yamllint -c .yamllint.yaml .

echo "== config cross-checks"
python3 scripts/check_configs.py

echo "== helm lint + render"
for chart in charts/*/; do
  name=$(basename "$chart")
  values=()
  # use every values file that targets this chart
  while IFS= read -r v; do values+=(-f "$v"); done < <(grep -rl "chart: $name\$" apps/*/envs/*/config.yaml 2>/dev/null | sed 's#config.yaml#values.yaml#' | while read -r f; do [ -f "$f" ] && echo "$f"; done | head -1)
  helm lint "$chart" "${values[@]}"
  if command -v kubeconform >/dev/null; then
    helm template "$name" "$chart" "${values[@]}" | kubeconform -strict -ignore-missing-schemas -summary
  fi
done
echo "all checks passed"
