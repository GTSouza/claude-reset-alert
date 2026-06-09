#!/usr/bin/env bash
#
# claude-limit-watch-linux.sh  (versão Linux)
# Detecta quando o Claude Code (assinatura) reseta os limites de 5h e semanal,
# e também quando a cota "libera" antes do horário previsto (queda do % de uso).
#
# Fonte dos dados: `claude -p "/usage"`, que roda de forma não-interativa,
# tem CUSTO ZERO (não consome tokens nem cota) e retorna algo como:
#
#   Current session: 15% used · resets Jun 9 at 11:50pm (America/Sao_Paulo)
#   Current week (all models): 2% used · resets Jun 15 at 9pm (America/Sao_Paulo)
#   Current week (Sonnet only): 0% used
#
#   - "Current session"        -> janela de 5 horas
#   - "Current week (all ...)" -> janela semanal
#
# Detecção de reset (por janela):
#   1) o horário de reset avança para um novo valor  -> nova janela
#   2) o % de uso cai além de DROP_THRESHOLD         -> cota liberou antes do previsto
#   Em ambos os casos: notificação no desktop (notify-send) + som + Telegram.
#
# Notificações no Linux usam `notify-send` (pacote libnotify-bin) e som via
# `canberra-gtk-play`, `paplay` ou `aplay` (o que estiver disponível).
#
# Uso:
#   ./claude-limit-watch-linux.sh            # vigia em loop e notifica
#   ./claude-limit-watch-linux.sh once       # checa o estado atual uma vez e sai
#   ./claude-limit-watch-linux.sh getid      # ajuda a descobrir o chat_id do Telegram
#   INTERVAL=120 ./claude-limit-watch-linux.sh
#
set -uo pipefail

# ---------- configuração ----------
INTERVAL="${INTERVAL:-60}"
MODEL="${MODEL:-haiku}"
DROP_THRESHOLD="${DROP_THRESHOLD:-5}"  # queda mínima de % p/ considerar que a cota liberou
# Eventos do tema de som freedesktop (usados por canberra-gtk-play -i <evento>).
SOUND_5H="${SOUND_5H:-message-new-instant}"  # som da janela de 5h
SOUND_WEEK="${SOUND_WEEK:-complete}"         # som da janela semanal
STATE_DIR="${STATE_DIR:-$HOME/.claude/limit-watch}"
LOG_FILE="$STATE_DIR/watch.log"
mkdir -p "$STATE_DIR"

# --- Telegram (opcional) ---
# Defina o token (do @BotFather) e o chat_id para receber as notificações no
# Telegram. O chat_id pode ser de um usuário, de um GRUPO (número negativo) ou
# de um canal, e aceita vários separados por vírgula: "123,-100456,@canal".
# Aceita os nomes TG_TOKEN/TG_CHAT_ID ou TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID.
# Os valores são carregados automaticamente de, na ordem:
#   1) ~/.claude/limit-watch/telegram.env
#   2) ./.env  (na pasta do script)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TG_ENV_FILE="${TG_ENV_FILE:-$STATE_DIR/telegram.env}"
[[ -f "$TG_ENV_FILE" ]] && set -a && source "$TG_ENV_FILE" && set +a
[[ -f "$SCRIPT_DIR/.env" ]] && set -a && source "$SCRIPT_DIR/.env" && set +a
TG_TOKEN="${TG_TOKEN:-${TELEGRAM_BOT_TOKEN:-}}"
TG_CHAT_ID="${TG_CHAT_ID:-${TELEGRAM_CHAT_ID:-}}"

# ---------- utilidades ----------
ts() { date "+%Y-%m-%d %H:%M:%S"; }
log() { printf '%s  %s\n' "$(ts)" "$*" | tee -a "$LOG_FILE"; }

# Toca um som de evento do tema (sem travar o loop).
play_sound() { # evento
  local ev="$1"
  if command -v canberra-gtk-play >/dev/null 2>&1; then
    canberra-gtk-play -i "$ev" >/dev/null 2>&1 &
  elif command -v paplay >/dev/null 2>&1; then
    paplay "/usr/share/sounds/freedesktop/stereo/${ev}.oga" >/dev/null 2>&1 &
  elif command -v aplay >/dev/null 2>&1; then
    aplay "/usr/share/sounds/alsa/Front_Center.wav" >/dev/null 2>&1 &
  fi
}

# Envia mensagem ao Telegram (para cada chat_id em TG_CHAT_ID).
tg_send() { # texto
  [[ -z "$TG_TOKEN" || -z "$TG_CHAT_ID" ]] && return 0
  command -v curl >/dev/null 2>&1 || { log "⚠️  curl ausente; Telegram desativado"; return 0; }
  local text="$1" id
  IFS=',' read -ra ids <<< "$TG_CHAT_ID"
  for id in "${ids[@]}"; do
    id="$(printf '%s' "$id" | tr -d '[:space:]')"
    [[ -z "$id" ]] && continue
    curl -s -m 10 -o /dev/null \
      --data-urlencode "chat_id=${id}" \
      --data-urlencode "text=${text}" \
      "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
      || log "⚠️  falha ao enviar Telegram p/ ${id}"
  done
}

notify() { # título, mensagem, som(evento, opcional)
  local title="$1" msg="$2" sound="${3:-bell}"
  log "🔔 $title — $msg  (som: $sound)"
  command -v notify-send >/dev/null 2>&1 &&
    notify-send -a "claude-limit-watch" "$title" "$msg" >/dev/null 2>&1
  play_sound "$sound"
  tg_send "🤖 ${title}"$'\n'"${msg}"
}

# Roda /usage e devolve o texto do campo "result".
fetch_usage() {
  local out
  out="$(claude -p "/usage" --model "$MODEL" --output-format json </dev/null 2>/dev/null)" || return 1
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$out" | jq -r '.result // empty'
  else
    printf '%s' "$out" | sed -n 's/.*"result":"\(.*\)","stop_reason".*/\1/p' | sed 's/\\n/\n/g'
  fi
}

# Extrai "<percent>|<reset>" de uma linha do /usage que comece com $2.
parse_line() { # texto, prefixo
  local line pct reset
  line="$(printf '%s\n' "$1" | grep -i "$2" | head -1)"
  [[ -z "$line" ]] && { echo "|"; return; }
  pct="$(printf '%s' "$line" | grep -oiE '[0-9]+% used' | grep -oE '[0-9]+' | head -1)"
  reset="$(printf '%s' "$line" | sed -n 's/.*resets \(.*\)/\1/p')"
  echo "${pct}|${reset}"
}

print_status() {
  local usage="$1" s w
  s="$(parse_line "$usage" "Current session")"
  w="$(parse_line "$usage" "Current week (all")"
  log "📊 5h: ${s%%|*}% usado · reset ${s#*|}  |  semanal: ${w%%|*}% usado · reset ${w#*|}"
}

# Avalia uma janela: detecta reset por mudança de horário OU por queda do %.
# args: rótulo, nome-curto(p/ arquivos de estado), pct atual, reset atual, som
check_window() {
  local label="$1" key="$2" pct="$3" reset="$4" sound="$5"
  local rf="$STATE_DIR/last_${key}_reset" pf="$STATE_DIR/last_${key}_pct"
  local prev_reset="" prev_pct=""
  [[ -f "$rf" ]] && prev_reset="$(cat "$rf")"
  [[ -f "$pf" ]] && prev_pct="$(cat "$pf")"

  if [[ -n "$reset" && -n "$prev_reset" && "$reset" != "$prev_reset" ]]; then
    notify "Claude Code: $label resetou ✅" "Nova janela. Próximo reset: $reset" "$sound"
  elif [[ -n "$pct" && -n "$prev_pct" ]] && (( pct < prev_pct - DROP_THRESHOLD )); then
    notify "Claude Code: cota da janela $label liberou ⬇️" "Uso caiu de ${prev_pct}% para ${pct}% (reset previsto: ${reset:-?})" "$sound"
  fi

  [[ -n "$reset" ]] && printf '%s' "$reset" > "$rf"
  [[ -n "$pct"   ]] && printf '%s' "$pct"   > "$pf"
}

check_once() {
  local usage s w s_pct s_reset w_pct w_reset
  usage="$(fetch_usage)" || { log "⚠️  falha ao consultar /usage"; return 1; }
  print_status "$usage"

  s="$(parse_line "$usage" "Current session")"; s_pct="${s%%|*}"; s_reset="${s#*|}"
  w="$(parse_line "$usage" "Current week (all")"; w_pct="${w%%|*}"; w_reset="${w#*|}"

  check_window "limite de 5h" "session" "$s_pct" "$s_reset" "$SOUND_5H"
  check_window "limite SEMANAL" "week" "$w_pct" "$w_reset" "$SOUND_WEEK"
}

# Ajuda a descobrir chat_id: liste mensagens recentes recebidas pelo bot.
# Mande qualquer mensagem ao bot (ou adicione-o ao grupo e mande uma msg) antes.
show_chat_ids() {
  [[ -z "$TG_TOKEN" ]] && { echo "Defina TG_TOKEN/TELEGRAM_BOT_TOKEN primeiro (em .env ou $TG_ENV_FILE)."; return 1; }
  echo "Buscando chats recentes do bot (mande uma msg ao bot/grupo antes)..."
  local resp; resp="$(curl -s -m 10 "https://api.telegram.org/bot${TG_TOKEN}/getUpdates")"
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$resp" | jq -r '
      .result[].message.chat
      | "chat_id=\(.id)  tipo=\(.type)  nome=\(.title // (.first_name // "") + " " + (.last_name // ""))"
    ' | sort -u
  else
    printf '%s\n' "$resp"
  fi
}

# ---------- execução ----------
case "${1:-watch}" in
  once)  check_once; exit $? ;;
  getid) show_chat_ids; exit $? ;;
  watch)
    log "👀 Vigiando limites do Claude Code via /usage (intervalo ${INTERVAL}s).${TG_TOKEN:+ Telegram ON.} Ctrl+C para parar."
    while true; do
      check_once
      sleep "$INTERVAL"
    done
    ;;
  *) echo "uso: $0 [watch|once|getid]"; exit 2 ;;
esac
