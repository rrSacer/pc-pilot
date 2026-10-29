#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=0
DESKTOP_USER=""
while (($#)); do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --user) (($# >= 2)) || { echo 'Uso: uninstall.sh [--dry-run] [--user NOME]' >&2; exit 2; }; DESKTOP_USER=$2; shift 2 ;;
    -h|--help) echo 'Uso: uninstall.sh [--dry-run] [--user NOME]'; exit 0 ;;
    *) echo "Opção desconhecida: $1" >&2; exit 2 ;;
  esac
done
run() { if ((DRY_RUN)); then printf '[dry-run]'; printf ' %q' "$@"; printf '\n'; else "$@"; fi; }
as_user() {
  local uid=$1 gid=$2 home=$3; shift 3
  if ((DRY_RUN)); then printf '[dry-run] setpriv --reuid=%q --regid=%q --init-groups env HOME=%q XDG_RUNTIME_DIR=/run/user/%q DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/%q/bus' "$uid" "$gid" "$home" "$uid" "$uid"; printf ' %q' "$@"; printf '\n'
  else setpriv --reuid="$uid" --regid="$gid" --init-groups env HOME="$home" XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" "$@"; fi
}
if ((!DRY_RUN)) && ((EUID != 0)); then echo 'Execute como root (sudo ./uninstall.sh).' >&2; exit 1; fi

if [[ -z $DESKTOP_USER ]]; then
  sid=$(loginctl show-seat seat0 -p ActiveSession --value 2>/dev/null || true)
  if [[ -n $sid ]]; then DESKTOP_USER=$(loginctl show-session "$sid" -p Name --value 2>/dev/null || true); fi
fi
if [[ -z $DESKTOP_USER && -r /run/systemd/seats/seat0 ]]; then
  ACTIVE_UID=$(sed -n 's/^ACTIVE_UID=//p' /run/systemd/seats/seat0 | head -n1)
  if [[ -n $ACTIVE_UID ]]; then DESKTOP_USER=$(getent passwd "$ACTIVE_UID" | cut -d: -f1 || true); fi
fi
if [[ -z $DESKTOP_USER ]] || ! getent passwd "$DESKTOP_USER" >/dev/null; then
  echo 'Não foi possível detectar o usuário gráfico; informe --user NOME.' >&2; exit 1
fi
IFS=: read -r _ _ UIDN GIDN _ HOME_DIR _ < <(getent passwd "$DESKTOP_USER")
EXT_DIR="$HOME_DIR/.local/share/gnome-shell/extensions/pcpilot@local"

if command -v claude >/dev/null 2>&1; then
  if claude mcp get pcpilot 2>/dev/null | grep -Fq '/opt/pcpilot/bin/pcpilot-mcp'; then
    run claude mcp remove -s user pcpilot
  else echo 'Claude: nenhuma entrada PCPilot correspondente para remover.'; fi
fi

CODEX=/root/.codex/config.toml
if [[ -f $CODEX ]] && python3 - "$CODEX" <<'PY'
import pathlib,sys,tomllib
d=tomllib.loads(pathlib.Path(sys.argv[1]).read_text())
raise SystemExit(0 if d.get('mcp_servers',{}).get('pcpilot',{}).get('command')=='/opt/pcpilot/bin/pcpilot-mcp' else 1)
PY
then
  if ((DRY_RUN)); then echo "[dry-run] backup e remover a tabela [mcp_servers.pcpilot] de $CODEX mantendo o restante"
  else
    cp -a -- "$CODEX" "$CODEX.pcpilot.bak"
    python3 - "$CODEX" <<'PY'
import pathlib,re,sys,tomllib
p=pathlib.Path(sys.argv[1]); s=p.read_text(); d=tomllib.loads(s)
if d.get('mcp_servers',{}).get('pcpilot',{}).get('command') != '/opt/pcpilot/bin/pcpilot-mcp': raise SystemExit(0)
pattern=r'(?ms)^\[mcp_servers\.pcpilot\][ \t]*\n(?:^(?!\[).*(?:\n|$))*'
s=re.sub(pattern,'',s,count=1); p.write_text(s.rstrip()+"\n")
PY
  fi
fi

GEMINI=/root/.gemini/settings.json
if [[ -f $GEMINI ]] && python3 - "$GEMINI" <<'PY'
import json,pathlib,sys
d=json.loads(pathlib.Path(sys.argv[1]).read_text())
raise SystemExit(0 if d.get('mcpServers',{}).get('pcpilot',{}).get('command')=='/opt/pcpilot/bin/pcpilot-mcp' else 1)
PY
then
  if ((DRY_RUN)); then echo "[dry-run] backup e remover mcpServers.pcpilot de $GEMINI mantendo o restante"
  else
    cp -a -- "$GEMINI" "$GEMINI.pcpilot.bak"
    python3 - "$GEMINI" <<'PY'
import json,pathlib,sys
p=pathlib.Path(sys.argv[1]); d=json.loads(p.read_text()); servers=d.get('mcpServers',{})
if servers.get('pcpilot',{}).get('command')=='/opt/pcpilot/bin/pcpilot-mcp':
    del servers['pcpilot']
    if not servers: d.pop('mcpServers',None)
p.write_text(json.dumps(d,indent=2,ensure_ascii=False)+"\n")
PY
  fi
fi

if ((DRY_RUN)); then
  echo "[dry-run] consultar enabled-extensions e remover pcpilot@local mantendo as demais entradas via gsettings como $DESKTOP_USER"
else
  ENABLED=$(as_user "$UIDN" "$GIDN" "$HOME_DIR" gsettings get org.gnome.shell enabled-extensions)
  if [[ "$ENABLED" == *"pcpilot@local"* ]]; then
    NEW_ENABLED=$(python3 -c 'import ast,sys; v=ast.literal_eval(sys.argv[1]); v=[x for x in v if x!="pcpilot@local"]; print("["+", ".join("\x27"+x.replace("\x27","\\\x27")+"\x27" for x in v)+"]")' "$ENABLED")
    as_user "$UIDN" "$GIDN" "$HOME_DIR" gsettings set org.gnome.shell enabled-extensions "$NEW_ENABLED"
  fi
fi
if [[ -e /etc/systemd/system/pcpilot.service ]] || systemctl cat pcpilot.service >/dev/null 2>&1; then
  if ((DRY_RUN)); then run systemctl disable --now pcpilot.service
  else systemctl disable --now pcpilot.service 2>/dev/null || true; fi
fi
run systemctl daemon-reload
run rm -f /etc/systemd/system/pcpilot.service /usr/local/bin/pcpilot
run rm -rf -- /opt/pcpilot "$EXT_DIR"
if [[ -e /var/lib/pcpilot/installer-created-config ]]; then
  run rm -f /etc/pcpilot/config.json /var/lib/pcpilot/installer-created-config
fi
if ((DRY_RUN)); then echo '[dry-run] remover diretórios PCPilot vazios de /etc e /var/lib'
else rmdir /etc/pcpilot /var/lib/pcpilot 2>/dev/null || true; fi
echo 'PCPilot removido. Backups de configurações dos agentes foram mantidos como *.pcpilot.bak.'
