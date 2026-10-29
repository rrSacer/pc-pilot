#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=0
DESKTOP_USER=""
while (($#)); do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --user) (($# >= 2)) || { echo 'Uso: install.sh [--dry-run] [--user NOME]' >&2; exit 2; }; DESKTOP_USER=$2; shift 2 ;;
    -h|--help) echo 'Uso: install.sh [--dry-run] [--user NOME]'; exit 0 ;;
    *) echo "Opção desconhecida: $1" >&2; exit 2 ;;
  esac
done

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
say() { printf '%s\n' "$*"; }
run() { if ((DRY_RUN)); then printf '[dry-run]'; printf ' %q' "$@"; printf '\n'; else "$@"; fi; }
as_user() {
  local uid=$1 gid=$2 home=$3; shift 3
  if ((DRY_RUN)); then printf '[dry-run] setpriv --reuid=%q --regid=%q --init-groups env HOME=%q XDG_RUNTIME_DIR=/run/user/%q DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/%q/bus' "$uid" "$gid" "$home" "$uid" "$uid"; printf ' %q' "$@"; printf '\n'
  else setpriv --reuid="$uid" --regid="$gid" --init-groups env HOME="$home" XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" "$@"; fi
}
backup() { local f=$1; [[ -e $f ]] || return 0; run cp -a -- "$f" "$f.pcpilot.bak"; }

if ((!DRY_RUN)) && ((EUID != 0)); then echo 'Execute como root (sudo ./install.sh).' >&2; exit 1; fi

if [[ -z $DESKTOP_USER ]]; then
  sid=$(loginctl show-seat seat0 -p ActiveSession --value 2>/dev/null || true)
  if [[ -n $sid ]]; then DESKTOP_USER=$(loginctl show-session "$sid" -p Name --value 2>/dev/null || true); fi
fi
if [[ -z $DESKTOP_USER && -r /run/systemd/seats/seat0 ]]; then
  ACTIVE_UID=$(sed -n 's/^ACTIVE_UID=//p' /run/systemd/seats/seat0 | head -n1)
  if [[ -n $ACTIVE_UID ]]; then DESKTOP_USER=$(getent passwd "$ACTIVE_UID" | cut -d: -f1 || true); fi
fi
if [[ -z $DESKTOP_USER ]]; then
  echo 'Não foi possível detectar o usuário ativo do seat0. Informe --user NOME.' >&2
  exit 1
fi
if ! getent passwd "$DESKTOP_USER" >/dev/null; then echo "Usuário inexistente: $DESKTOP_USER" >&2; exit 1; fi
IFS=: read -r _ _ UIDN GIDN _ HOME_DIR _ < <(getent passwd "$DESKTOP_USER")
[[ -n $UIDN && -n $GIDN && -n $HOME_DIR ]] || { echo "Entrada passwd inválida: $DESKTOP_USER" >&2; exit 1; }

say "Instalando PCPilot para o usuário gráfico: $DESKTOP_USER (uid $UIDN)"
run install -d -o root -g root -m 0755 /opt/pcpilot
run cp -a --no-preserve=ownership "$ROOT/pcpilot" /opt/pcpilot/
run find /opt/pcpilot/pcpilot -type d -name __pycache__ -prune -exec rm -rf '{}' +
run cp -a --no-preserve=ownership "$ROOT/bin" /opt/pcpilot/
run cp -a --no-preserve=ownership "$ROOT/extension" /opt/pcpilot/
run chown -R root:root /opt/pcpilot
run chmod -R go-w /opt/pcpilot
run chmod 0755 /opt/pcpilot/bin/pcpilot /opt/pcpilot/bin/pcpilot-mcp
run ln -sfn /opt/pcpilot/bin/pcpilot /usr/local/bin/pcpilot

run install -d -o root -g root -m 0755 /etc/pcpilot
if [[ ! -e /etc/pcpilot/config.json ]]; then
  run install -o root -g root -m 0644 "$ROOT/config.example.json" /etc/pcpilot/config.json
  run install -d -o root -g root -m 0755 /var/lib/pcpilot
  run touch /var/lib/pcpilot/installer-created-config
  run chown root:root /var/lib/pcpilot/installer-created-config
  run chmod 0600 /var/lib/pcpilot/installer-created-config
else say 'Configuração existente preservada: /etc/pcpilot/config.json'; fi

run install -o root -g root -m 0644 "$ROOT/systemd/pcpilot.service" /etc/systemd/system/pcpilot.service
run systemctl daemon-reload
run systemctl enable --now pcpilot.service

EXT_DIR="$HOME_DIR/.local/share/gnome-shell/extensions/pcpilot@local"
run install -d -o "$UIDN" -g "$GIDN" -m 0755 "$(dirname -- "$EXT_DIR")"
run rm -rf -- "$EXT_DIR"
run cp -a --no-preserve=ownership "$ROOT/extension/pcpilot@local" "$EXT_DIR"
run chown -R "$UIDN:$GIDN" "$EXT_DIR"
run chmod -R go-w "$EXT_DIR"
if ((DRY_RUN)); then
  say "[dry-run] consultar enabled-extensions e acrescentar pcpilot@local mantendo as entradas existentes via gsettings como $DESKTOP_USER"
else
  ENABLED=$(as_user "$UIDN" "$GIDN" "$HOME_DIR" gsettings get org.gnome.shell enabled-extensions)
  if [[ "$ENABLED" != *"pcpilot@local"* ]]; then
    NEW_ENABLED=$(python3 -c 'import ast,sys; v=ast.literal_eval(sys.argv[1]); v.append("pcpilot@local") if "pcpilot@local" not in v else None; print("["+", ".join("\x27"+x.replace("\x27","\\\x27")+"\x27" for x in v)+"]")' "$ENABLED")
    as_user "$UIDN" "$GIDN" "$HOME_DIR" gsettings set org.gnome.shell enabled-extensions "$NEW_ENABLED"
  else say 'Extensão já está em enabled-extensions.'; fi
fi

# Claude Code is optional; avoid changing a registration that already exists.
if command -v claude >/dev/null 2>&1; then
  if claude mcp get pcpilot >/dev/null 2>&1; then say 'MCP pcpilot já registrado no Claude Code.'
  elif ((DRY_RUN)); then say '[dry-run] claude mcp add -s user pcpilot -- /opt/pcpilot/bin/pcpilot-mcp'
  else claude mcp add -s user pcpilot -- /opt/pcpilot/bin/pcpilot-mcp; fi
else say 'Claude Code não encontrado; registro MCP ignorado.'; fi

CODEX=/root/.codex/config.toml
if python3 - "$CODEX" <<'PY'
import pathlib, sys, tomllib
p = pathlib.Path(sys.argv[1])
try: data = tomllib.loads(p.read_text()) if p.exists() else {}
except Exception as e: raise SystemExit(f"TOML inválido em {p}: {e}")
raise SystemExit(0 if "pcpilot" in data.get("mcp_servers", {}) else 1)
PY
then say 'MCP pcpilot já está configurado no Codex.'
elif ((DRY_RUN)); then say "[dry-run] backup se existir e anexar [mcp_servers.pcpilot] a $CODEX"
else
  run install -d -o root -g root -m 0700 /root/.codex
  backup "$CODEX"
  printf '\n[mcp_servers.pcpilot]\ncommand = "/opt/pcpilot/bin/pcpilot-mcp"\n' >> "$CODEX"
fi

GEMINI=/root/.gemini/settings.json
if python3 - "$GEMINI" <<'PY'
import json, pathlib, sys
p=pathlib.Path(sys.argv[1])
try: d=json.loads(p.read_text()) if p.exists() else {}
except Exception as e: raise SystemExit(f"JSON inválido em {p}: {e}")
raise SystemExit(0 if "pcpilot" in d.get("mcpServers", {}) else 1)
PY
then say 'MCP pcpilot já está configurado no Gemini.'
elif ((DRY_RUN)); then say "[dry-run] backup se existir e adicionar mcpServers.pcpilot a $GEMINI"
else
  run install -d -o root -g root -m 0700 /root/.gemini
  backup "$GEMINI"
  python3 - "$GEMINI" <<'PY'
import json, pathlib, sys
p=pathlib.Path(sys.argv[1]); d=json.loads(p.read_text()) if p.exists() else {}
d.setdefault("mcpServers", {})["pcpilot"]={"command":"/opt/pcpilot/bin/pcpilot-mcp"}
p.write_text(json.dumps(d, indent=2, ensure_ascii=False)+"\n")
PY
  chown root:root "$GEMINI"; chmod 0600 "$GEMINI"
fi

cat <<'EOF'

Instalação concluída. Saia da sessão GNOME e entre novamente uma vez para carregar a extensão.
Teste: sudo pcpilot status; em seguida peça ao Claude para abrir o Firefox. A tela deve mostrar a borda vermelha e o aviso.
Ctrl+Alt+Esc devolve o controle imediatamente.
Emergência por outro terminal ou SSH: sudo pcpilot release ou sudo systemctl stop pcpilot.
EOF
