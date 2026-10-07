#!/usr/bin/env bash
# Instala la recogida periodica de Faro con systemd de usuario.
#
# Uso:   ./ops/install-scheduler.sh
# Quitar: systemctl --user disable --now gongora-collect.timer
set -euo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl no esta disponible. Usa la alternativa: ./ops/install-cron.sh" >&2
    exit 1
fi
if ! systemctl --user is-system-running >/dev/null 2>&1; then
    estado="$(systemctl --user is-system-running 2>&1 || true)"
    if [[ "$estado" != "degraded" && "$estado" != "running" ]]; then
        echo "No hay instancia de systemd de usuario ($estado)." >&2
        echo "Usa la alternativa: ./ops/install-cron.sh" >&2
        exit 1
    fi
fi
if [[ ! -x "$PROJECT/.venv/bin/gongora" ]]; then
    echo "Falta $PROJECT/.venv/bin/gongora. Instala el proyecto primero:" >&2
    echo "  python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2
    exit 1
fi

mkdir -p "$UNIT_DIR"
for unit in gongora-collect.service gongora-collect.timer; do
    sed "s|__PROJECT__|$PROJECT|g" "$PROJECT/ops/systemd/$unit" > "$UNIT_DIR/$unit"
    echo "escrito $UNIT_DIR/$unit"
done

systemctl --user daemon-reload
systemctl --user enable --now gongora-collect.timer

echo
echo "Temporizador instalado. Recogida cada 6 horas."
systemctl --user list-timers gongora-collect.timer --all --no-pager || true
echo
echo "IMPORTANTE: el equipo y WSL deben estar en marcha para que se ejecute."
echo "Un temporizador de usuario solo corre mientras hay sesion de usuario activa."
echo "Para que siga tras cerrar la sesion (requiere privilegios):"
echo "    sudo loginctl enable-linger $USER"
echo "Si el equipo esta apagado a la hora prevista, esa recogida NO se recupera"
echo "(Persistent=false, a proposito: evita una rafaga al volver)."
