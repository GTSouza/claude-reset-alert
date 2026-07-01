#!/bin/sh
# Publica as cópias versionadas na cópia de runtime (~/.claude/tools).
# Valida ANTES de copiar (py_compile / sh -n) — nunca publica um arquivo quebrado.
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
cp "$SRC" "$DEST"
chmod +x "$DEST"                # permite a invocação direta `token_monitor.py gate` (shebang)
cp "$HOOK_SRC" "$HOOK_DEST"
chmod +x "$HOOK_DEST"           # o hook do Claude Code chama este script direto
echo "deployed → $DEST"
echo "deployed → $HOOK_DEST"
