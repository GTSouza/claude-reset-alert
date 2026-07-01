"""
telegram_bridge.py — Ponte Telegram ↔ Claude Code.

Loop de long-polling no Telegram: cada mensagem recebida dispara
`claude -p` em subprocess. Claude tem o MCP server do Telegram
registrado globalmente e usa send_message / send_token_report para
responder. Roda sem parar até Ctrl-C.

Uso:
    python telegram_bridge.py                  # processa qualquer chat
    python telegram_bridge.py --allowed 123 456 789  # só esses chat_ids
    python telegram_bridge.py --model haiku    # modelo mais rápido
"""
import argparse
import os
import shutil
import subprocess
import sys
import threading
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

CLAUDE_BIN = shutil.which("claude") or "/opt/homebrew/bin/claude"

SYSTEM_PROMPT = """\
You are an assistant that responds exclusively via Telegram MCP tools.
NEVER write a plain text response — always call a tool to reply.

Available MCP tools (server: telegram):
- send_message(text, chat_id)
- send_token_report(command, window, since, by, model_filter, session_filter, project_filter, io_only, limit, chat_id)

send_token_report parameter mapping:
  command   → "report" (default), "limits", "meter-report", "bursts"
  window    → "5h", "day", "week" (default), "month"
  since     → ISO date "YYYY-MM-DD" (overrides window)
  by        → "none" (default/global), "model", "session", "project", "day", "billing"
  model_filter   → base model id (prefix match), e.g. "claude-fable-5", "claude-sonnet-5", "claude-opus-4-8", "claude-haiku-4-5" (matches [1m]/dated variants)
  session_filter → session_id prefix
  project_filter → project name substring
  io_only   → true = show only in/out tokens (no cache/cost), comparable to Claude app
  limit     → max rows for limits/meter-report (default 50)

Natural language → parameter examples:
  "relatório da semana por modelo"          → command=report, window=week, by=model
  "uso de hoje"                             → command=report, window=day
  "tokens do fable" / "modelo fable"        → command=report, model_filter=claude-fable-5, by=model
  "tokens do sonnet"                        → command=report, model_filter=claude-sonnet-5, by=model
  "últimas 5h por sessão"                   → command=report, window=5h, by=session
  "desde junho por projeto"                 → command=report, since=2026-06-01, by=project
  "assinatura vs crédito"                   → command=report, by=billing
  "só in/out" / "comparar com app"          → command=report, io_only=true
  "batidas de limite" / "rate limit"        → command=limits
  "medidor oficial" / "meter"               → command=meter-report
  "bursts" / "atividade"                    → command=bursts

Workflow:
1. Parse the user request and map to the best send_token_report parameters.
2. Always include the provided chat_id.
3. Call the tool — do not output anything else.
"""


def _get_updates(offset: int, timeout: int = 30) -> list[dict]:
    try:
        resp = httpx.get(
            f"{BASE_URL}/getUpdates",
            params={
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": ["message"],
            },
            timeout=timeout + 10,
        )
        data = resp.json()
        return data.get("result", []) if data.get("ok") else []
    except Exception as exc:
        print(f"[poll error] {exc}", flush=True)
        time.sleep(5)
        return []


def _send_typing(chat_id: int) -> None:
    try:
        httpx.post(
            f"{BASE_URL}/sendChatAction",
            json={"chat_id": chat_id, "action": "typing"},
            timeout=5,
        )
    except Exception:
        pass


def _process(chat_id: int, text: str, model: str) -> None:
    prompt = f"User message (chat_id={chat_id}): {text}"
    _send_typing(chat_id)
    print(f"[→ claude] chat_id={chat_id}  msg={text!r}", flush=True)

    cmd = [
        CLAUDE_BIN, "-p", prompt,
        "--system-prompt", SYSTEM_PROMPT,
        "--dangerously-skip-permissions",
    ]
    if model:
        cmd += ["--model", model]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except FileNotFoundError:
        print(f"[error] claude não encontrado em: {CLAUDE_BIN}", flush=True)
        return
    except subprocess.TimeoutExpired:
        print("[error] claude timeout (180s)", flush=True)
        return

    if result.returncode != 0:
        print(f"[claude stderr] {result.stderr[:300]}", flush=True)
    else:
        print(f"[← claude] {result.stdout[:200]}", flush=True)


def _skip_pending(offset: int) -> int:
    """Drena os updates pendentes sem processá-los; retorna o próximo offset."""
    updates = _get_updates(offset, timeout=0)
    if updates:
        new_offset = updates[-1]["update_id"] + 1
        print(f"[bridge] ignorando {len(updates)} mensagem(ns) anteriores ao boot (offset={new_offset})", flush=True)
        return new_offset
    return offset


def run(allowed: set[int], model: str) -> None:
    assert BOT_TOKEN, "TELEGRAM_BOT_TOKEN não definido no .env"
    assert os.path.isfile(CLAUDE_BIN), f"claude CLI não encontrado: {CLAUDE_BIN}"

    who = f"apenas {allowed}" if allowed else "qualquer chat"
    print(f"[bridge] ouvindo Telegram — {who} — modelo: {model or 'padrão'}", flush=True)
    print("[bridge] Ctrl-C para parar\n", flush=True)

    offset = _skip_pending(0)

    while True:
        updates = _get_updates(offset)
        for update in updates:
            offset = update["update_id"] + 1
            msg = update.get("message", {})
            text = (msg.get("text") or "").strip()
            chat_id: int = (msg.get("chat") or {}).get("id", 0)
            sender = (msg.get("from") or {}).get("username", "?")

            if not text or not chat_id:
                continue
            if allowed and chat_id not in allowed:
                print(f"[skip] chat_id={chat_id} (@{sender}) não autorizado", flush=True)
                continue

            threading.Thread(
                target=_process,
                args=(chat_id, text, model),
                daemon=True,
            ).start()


def main() -> None:
    ap = argparse.ArgumentParser(description="Ponte Telegram ↔ Claude Code")
    ap.add_argument(
        "--allowed",
        nargs="*",
        type=int,
        default=[],
        metavar="CHAT_ID",
        help="Lista de chat_ids permitidos (padrão: todos)",
    )
    ap.add_argument(
        "--model",
        default="",
        help="Modelo do Claude (ex.: haiku, sonnet). Padrão: configuração do Claude Code",
    )
    args = ap.parse_args()

    try:
        run(set(args.allowed), args.model)
    except KeyboardInterrupt:
        print("\n[bridge] encerrado.", flush=True)
        sys.exit(0)


if __name__ == "__main__":
    main()
