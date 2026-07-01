#!/usr/bin/env bash
#
# claude-limit-watch.sh
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
#   1) o instante de reset avança além de RESET_TOLERANCE -> nova janela
#   2) o % de uso cai além de DROP_THRESHOLD         -> cota liberou antes do previsto
#   Em ambos os casos: notificação no macOS + som (distinto por janela) + Telegram.
#
# Uso:
#   ./claude-limit-watch.sh            # vigia em loop e notifica
#   ./claude-limit-watch.sh once       # checa o estado atual uma vez e sai
#   ./claude-limit-watch.sh getid      # ajuda a descobrir o chat_id do Telegram
#   INTERVAL=120 ./claude-limit-watch.sh
#
set -uo pipefail

# ---------- configuração ----------
INTERVAL="${INTERVAL:-300}"  # 5 minutos
MODEL="${MODEL:-haiku}"
DROP_THRESHOLD="${DROP_THRESHOLD:-5}"  # queda mínima de % p/ considerar que a cota liberou
RESET_TOLERANCE="${RESET_TOLERANCE:-600}"  # segundos: variação no horário de reset que NÃO conta como nova janela (evita falso positivo de "3pm" vs "2:59pm")
SOUND_5H="${SOUND_5H:-Ping}"           # som da janela de 5h
SOUND_WEEK="${SOUND_WEEK:-Submarine}"  # som da janela semanal
SOUNDS_DIR="/System/Library/Sounds"
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

notify() { # título, mensagem, som(opcional, padrão Glass)
  local title="$1" msg="$2" sound="${3:-Glass}"
  log "🔔 $title — $msg  (som: $sound)"
  command -v osascript >/dev/null 2>&1 &&
    osascript -e "display notification \"${msg//\"/\\\"}\" with title \"${title//\"/\\\"}\" sound name \"${sound//\"/\\\"}\"" >/dev/null 2>&1
  command -v afplay >/dev/null 2>&1 && afplay "$SOUNDS_DIR/${sound}.aiff" >/dev/null 2>&1 &
  tg_send "🤖 ${title}"$'\n'"${msg}"
}

# Roda /usage uma vez e devolve o texto do campo "result".
fetch_usage_once() {
  local out
  out="$(claude -p "/usage" --model "$MODEL" --output-format json </dev/null 2>/dev/null)" || return 1
  [[ -z "$out" ]] && return 1
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$out" | jq -r '.result // empty'
  else
    printf '%s' "$out" | sed -n 's/.*"result":"\(.*\)","stop_reason".*/\1/p' | sed 's/\\n/\n/g'
  fi
}

# O `/usage` em modo -p é uma resposta gerada pelo modelo: às vezes vem a tabela
# completa ("Current session: X% used · resets ..."), às vezes só uma frase
# genérica, às vezes vazio. Tentamos algumas vezes até obter uma resposta que
# contenha os percentuais; caso contrário, falhamos para o chamador tratar.
FETCH_RETRIES="${FETCH_RETRIES:-3}"
fetch_usage() {
  local attempt usage
  for (( attempt = 1; attempt <= FETCH_RETRIES; attempt++ )); do
    usage="$(fetch_usage_once)" || usage=""
    if printf '%s' "$usage" | grep -qiE 'Current session|% used'; then
      printf '%s' "$usage"
      return 0
    fi
    (( attempt < FETCH_RETRIES )) && sleep 2
  done
  return 1
}

# Converte um horário de reset ("Jun 10 at 3pm (America/Sao_Paulo)") em epoch.
# O texto vem do modelo e arredonda os minutos a cada poll (3pm <-> 2:59pm),
# então comparamos instantes com tolerância em vez de comparar strings.
# Devolve vazio (e código !=0) se não conseguir interpretar.
# "mon day year HH MM AMPM" -> epoch, cobrindo GNU date (Linux) e BSD date (macOS).
_epoch_ymd() { # mon day year HH MM AMPM
  if date --version >/dev/null 2>&1; then          # GNU date (Linux): lê 12h e 24h
    date -d "$1 $2 $3 $4:$5 $6" +%s 2>/dev/null
  elif [[ -n "$6" ]]; then                         # BSD date (macOS): 12h com am/pm
    date -j -f "%b %d %Y %I:%M %p" "$1 $2 $3 $4:$5 $6" +%s 2>/dev/null
  else                                             # BSD date: relógio de 24h (sem am/pm)
    date -j -f "%b %d %Y %H:%M" "$1 $2 $3 $4:$5" +%s 2>/dev/null
  fi
}

reset_to_epoch() { # "Mon DD at H[:MM]am/pm (tz)"
  local s="$1" mon day tm ampm hh mm year e
  s="${s%% (*}"                                   # remove " (timezone)"
  s="$(printf '%s' "$s" | tr -s ' ')"
  mon="$(printf '%s' "$s" | awk '{print $1}')"
  day="$(printf '%s' "$s" | awk '{print $2}')"
  tm="$(printf '%s'  "$s" | sed -n 's/.* at \(.*\)$/\1/p')"
  [[ -z "$mon" || -z "$day" || -z "$tm" ]] && return 1
  ampm="$(printf '%s' "$tm" | grep -oiE '[ap]m$' | tr '[:lower:]' '[:upper:]')"
  tm="$(printf '%s' "$tm" | sed -E 's/[aApP][mM]$//')"
  if [[ "$tm" == *:* ]]; then hh="${tm%%:*}"; mm="${tm#*:}"; else hh="$tm"; mm="00"; fi
  [[ "$hh" =~ ^[0-9]+$ && "$mm" =~ ^[0-9]+$ ]] || return 1
  # base decimal explícita: sem isso bash lê '08'/'09' como octal inválido e aborta.
  hh=$((10#$hh)); mm=$((10#$mm))
  printf -v hh '%02d' "$hh"; printf -v mm '%02d' "$mm"
  year="$(date +%Y)"
  e="$(_epoch_ymd "$mon" "$day" "$year" "$hh" "$mm" "$ampm")"
  # /usage só reporta resets FUTUROS; se o ano corrente jogou a data >1 dia no
  # passado (reset de janeiro lido em dezembro), é do ano seguinte.
  if [[ -n "$e" ]] && (( e < $(date +%s) - 86400 )); then
    e="$(_epoch_ymd "$mon" "$day" "$((year + 1))" "$hh" "$mm" "$ampm")"
  fi
  [[ -n "$e" ]] && printf '%s' "$e"
}

# Extrai "<percent>|<reset>" de uma linha do /usage que comece com $2.
parse_line() { # texto, prefixo
  local line pct reset
  # grep -vi 'only': ignora as linhas por-modelo "(... only)" p/ o prefixo genérico
  # "Current week" casar a linha agregada mesmo se o /usage relabelar o "(all ...)".
  line="$(printf '%s\n' "$1" | grep -i "$2" | grep -vi 'only' | head -1)"
  [[ -z "$line" ]] && { echo "|"; return; }
  pct="$(printf '%s' "$line" | grep -oiE '[0-9]+% used' | grep -oE '[0-9]+' | head -1)"
  reset="$(printf '%s' "$line" | sed -n 's/.*resets \(.*\)/\1/p')"
  echo "${pct}|${reset}"
}

print_status() {
  local usage="$1" s w
  s="$(parse_line "$usage" "Current session")"
  w="$(parse_line "$usage" "Current week")"
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

  # Reset por horário: só conta como nova janela se o instante avançou ALÉM da
  # tolerância. Assim "3pm" virar "2:59pm" (mesmo reset, arredondado) é ignorado,
  # mas um salto real (~5h ou ~1 semana à frente) dispara. Se algum horário NÃO for
  # interpretável, NÃO alerta por diferença de texto — um arredondamento de minuto
  # ("21:00" vs "20:59") viraria um "resetou ✅" falso. A queda do % (abaixo) ainda
  # detecta o reset real (100% -> 0%), então nada crítico se perde.
  local reset_advanced=0
  if [[ -n "$reset" && -n "$prev_reset" && "$reset" != "$prev_reset" ]]; then
    local cur_e prev_e
    cur_e="$(reset_to_epoch "$reset")"; prev_e="$(reset_to_epoch "$prev_reset")"
    if [[ -n "$cur_e" && -n "$prev_e" ]]; then
      (( cur_e - prev_e > RESET_TOLERANCE )) && reset_advanced=1
    fi
  fi

  if (( reset_advanced )); then
    notify "Claude Code: $label resetou ✅" "Nova janela. Próximo reset: $reset" "$sound"
  elif [[ -n "$pct" && -n "$prev_pct" ]] && (( pct < prev_pct - DROP_THRESHOLD )); then
    notify "Claude Code: cota da janela $label liberou ⬇️" "Uso caiu de ${prev_pct}% para ${pct}% (reset previsto: ${reset:-?})" "$sound"
  fi

  [[ -n "$reset" ]] && printf '%s' "$reset" > "$rf"
  [[ -n "$pct"   ]] && printf '%s' "$pct"   > "$pf"
}

check_once() {
  local usage s w s_pct s_reset w_pct w_reset
  usage="$(fetch_usage)" || {
    log "⚠️  /usage não retornou os percentuais (resposta vazia/genérica após ${FETCH_RETRIES} tentativas); tentando de novo no próximo ciclo."
    return 1
  }
  print_status "$usage"

  s="$(parse_line "$usage" "Current session")"; s_pct="${s%%|*}"; s_reset="${s#*|}"
  w="$(parse_line "$usage" "Current week")"; w_pct="${w%%|*}"; w_reset="${w#*|}"

  check_window "limite de 5h" "session" "$s_pct" "$s_reset" "$SOUND_5H"
  check_window "limite SEMANAL" "week" "$w_pct" "$w_reset" "$SOUND_WEEK"
}

# Ajuda a descobrir chat_id: liste mensagens recentes recebidas pelo bot.
# Mande qualquer mensagem ao bot (ou adicione-o ao grupo e mande uma msg) antes.
show_chat_ids() {
  [[ -z "$TG_TOKEN" ]] && { echo "Defina TG_TOKEN primeiro (em $TG_ENV_FILE)."; return 1; }
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
