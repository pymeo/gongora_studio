#!/usr/bin/env bash
# Alternativa a systemd: recogida cada 6 horas con cron.
# Solo hace falta si systemd de usuario no esta disponible.
set -euo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MARKER="# gongora-studio-collect"
# flock evita solapamiento igual que hace el propio comando.
LINE="0 */6 * * * flock -n $PROJECT/var/cron.lock $PROJECT/.venv/bin/gongora collect --trigger cron >> $PROJECT/var/logs/cron.log 2>&1 $MARKER"

if ! command -v crontab >/dev/null 2>&1; then
    echo "crontab no esta disponible." >&2
    exit 1
fi

mkdir -p "$PROJECT/var/logs"
actual="$(crontab -l 2>/dev/null || true)"
if grep -qF "$MARKER" <<<"$actual"; then
    echo "La entrada de cron ya existe. Nada que hacer."
else
    printf '%s\n%s\n' "$actual" "$LINE" | grep -v '^$' | crontab -
    echo "Entrada de cron instalada:"
    echo "  $LINE"
fi
echo
echo "Comprobar:  crontab -l"
echo "Quitar:     crontab -l | grep -v '$MARKER' | crontab -"
echo "El demonio cron debe estar en marcha: service cron status"
