#!/usr/bin/env bash
# Instala/atualiza as unidades systemd --user do Cursor Memory e ativa o timer + UI.
# Idempotente. Não exige argumentos.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
VENV_PY="$REPO/.venv/bin/python"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()  { printf '\033[1;32m ok \033[0m %s\n' "$*"; }
erro() { printf '\033[1;31merro:\033[0m %s\n' "$*" >&2; exit 1; }

command -v systemctl >/dev/null || erro "systemctl não encontrado"
[ -x "$VENV_PY" ] || erro "venv ausente — rode: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"

mkdir -p "$UNIT_DIR" "$REPO/data"

for unit in cursor-memory-extract.service cursor-memory-extract.timer cursor-memory-web.service; do
  src="$REPO/systemd/${unit}.in"
  [ -f "$src" ] || erro "template em falta: $src"
  sed "s|@REPO@|$REPO|g" "$src" > "$UNIT_DIR/$unit"
  ok "$UNIT_DIR/$unit"
done

systemctl --user daemon-reload
systemctl --user enable --now cursor-memory-web.service
systemctl --user enable --now cursor-memory-extract.timer

# Primeiro extract se o índice ainda não existir
if [ ! -f "$REPO/data/index.db" ]; then
  log "índice ausente — a correr extract inicial (pode demorar alguns minutos)"
  systemctl --user start cursor-memory-extract.service
else
  log "a disparar extract incremental"
  systemctl --user start cursor-memory-extract.service || true
fi

ok "UI: http://127.0.0.1:8777/"
ok "timer: $(systemctl --user show cursor-memory-extract.timer -p NextElapseUSecRealtime --value 2>/dev/null || echo ativo)"
echo
systemctl --user --no-pager --full status cursor-memory-web.service cursor-memory-extract.timer | sed -n '1,40p'
