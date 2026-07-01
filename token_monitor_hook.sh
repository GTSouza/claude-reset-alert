#!/bin/sh
# Hook do Claude Code para o token_monitor: mantém o banco fresco a cada evento
# de sessão, SEM nunca falhar a sessão e SEM injetar saída no contexto.
#
# Recebe o nome do evento como $1 (SessionStart | Stop | PreCompact) e roda só
# subcomandos BARATOS e sem efeito colateral:
#   - ingest      -> importa transcripts + billing + uso do Codex (sem notificar)
#   - codex-meter -> snapshot do rate-limit do Codex a partir dos rollouts (custo zero)
# NUNCA usa meter/gate/status nem codex-meter --refresh/--watch: esses chamam
# `claude -p /usage` (claude aninhado) ou gastam um turno do `codex exec`.
#
# Regras de segurança do hook:
#   - toda saída vai para /dev/null: no SessionStart o stdout (exit 0) entraria no
#     contexto do Claude, então o hook precisa ser MUDO.
#   - cada chamada é limitada por `timeout` (se existir) e o script SEMPRE sai 0:
#     um banco travado, um .jsonl a meio de escrita ou a falta de python3 viram
#     no-op, nunca um erro que bloqueie o turno ou a compactação.
event="${1:-Stop}"
TM="${TOKEN_MONITOR:-$HOME/.claude/tools/token_monitor.py}"
[ -f "$TM" ] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0

# Limite (s) da chamada atual. `timeout`/`gtimeout` são opcionais (macOS não traz
# `timeout` por padrão); sem eles, roda direto — os subcomandos usados são rápidos.
TIMEOUT=30
tm() { # $@ = subcomando + flags do token_monitor.py
  if command -v timeout >/dev/null 2>&1; then
    timeout "$TIMEOUT" python3 "$TM" "$@" >/dev/null 2>&1 || true
  elif command -v gtimeout >/dev/null 2>&1; then
    gtimeout "$TIMEOUT" python3 "$TM" "$@" >/dev/null 2>&1 || true
  else
    python3 "$TM" "$@" >/dev/null 2>&1 || true
  fi
}

case "$event" in
  SessionStart)
    TIMEOUT=30; tm ingest
    TIMEOUT=20; tm codex-meter --no-notify   # rollout do Codex fresco (sem subprocess)
    ;;
  *)                                          # Stop, PreCompact e qualquer outro
    TIMEOUT=30; tm ingest
    ;;
esac

exit 0
