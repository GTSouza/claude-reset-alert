#!/bin/sh
# Publica as cópias versionadas na cópia de runtime (~/.claude/tools).
# Valida ANTES de copiar (py_compile / sh -n) — nunca publica um arquivo quebrado.
# A publicação é ATÔMICA (cp em temp + mv no mesmo diretório): o watcher em modo
# --watch observa o arquivo a cada 1s e faz re-exec; um cp direto (trunca+escreve)
# abriria janela para ele executar um script meio-copiado.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
TOOLS_DIR="${CLAUDE_TOOLS_DIR:-$HOME/.claude/tools}"

SRC="$HERE/cnp/token_monitor.py"
DEST="$TOOLS_DIR/token_monitor.py"
HOOK_SRC="$HERE/token_monitor_hook.sh"
HOOK_DEST="$TOOLS_DIR/token_monitor_hook.sh"

python3 -m py_compile "$SRC"
sh -n "$HOOK_SRC"                # valida a sintaxe do wrapper de hook
mkdir -p "$TOOLS_DIR"           # cria ~/.claude/tools (ou CLAUDE_TOOLS_DIR) numa máquina nova

publish() { # src dest — cópia atômica com permissão de execução
  tmp="$(mktemp "$2.XXXXXX")"
  cp "$1" "$tmp"
  chmod +x "$tmp"
  mv -f "$tmp" "$2"             # rename no mesmo FS: atômico
  echo "deployed → $2"
}
publish "$SRC" "$DEST"          # invocação direta `token_monitor.py gate` (shebang)
publish "$HOOK_SRC" "$HOOK_DEST"  # o hook do Claude Code chama este script direto

# Watcher gerenciado (launchd): reinicia p/ já rodar a versão nova AGORA.
# Um watcher avulso também se troca sozinho: o loop detecta o script novo e re-exec.
# Falha do kickstart NÃO falha o deploy (if/else não dispara o set -e).
LABEL="${TOKEN_MONITOR_LAUNCHD_LABEL:-com.gtsouza.token-monitor}"
if command -v launchctl >/dev/null 2>&1 \
   && launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  if launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null; then
    echo "watcher (launchd) reiniciado na versão nova"
  else
    echo "aviso: kickstart falhou — o watcher se atualiza sozinho no próximo tick (~1s)"
  fi
fi
