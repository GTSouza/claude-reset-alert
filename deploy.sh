#!/bin/sh
# Publica a cópia versionada do monitor na cópia de runtime (~/.claude/tools).
# Valida com py_compile ANTES de copiar — nunca publica um arquivo quebrado.
set -e
SRC="$(cd "$(dirname "$0")" && pwd)/cnp/token_monitor.py"
DEST="${CLAUDE_TOOLS_DIR:-$HOME/.claude/tools}/token_monitor.py"

python3 -m py_compile "$SRC"
cp "$SRC" "$DEST"
chmod +x "$DEST"   # permite a invocação direta `token_monitor.py gate` (shebang)
echo "deployed → $DEST"
