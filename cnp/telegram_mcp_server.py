import os
import subprocess
import sys
import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from pydantic import Field

TOKEN_MONITOR = os.path.expanduser("~/.claude/tools/token_monitor.py")

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
assert BOT_TOKEN, "TELEGRAM_BOT_TOKEN is not set in .env"

DEFAULT_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

mcp = FastMCP("TelegramMCP", log_level="ERROR")


@mcp.tool(
    name="send_message",
    description=(
        "Send a text message to a Telegram chat. "
        f"If no chat_id is provided, sends to the default chat ({DEFAULT_CHAT_ID})."
    ),
)
def send_message(
    text: str = Field(description="Message text (supports Markdown)"),
    chat_id: str = Field(default="", description="Telegram chat ID or @username. Uses default if omitted."),
) -> str:
    target = chat_id or DEFAULT_CHAT_ID
    if not target:
        return "Error: no chat_id provided and TELEGRAM_CHAT_ID is not set in .env"
    response = httpx.post(
        f"{BASE_URL}/sendMessage",
        json={"chat_id": target, "text": text, "parse_mode": "Markdown"},
    )
    data = response.json()
    if data.get("ok"):
        msg_id = data["result"]["message_id"]
        return f"Message sent (id={msg_id})"
    return f"Error: {data.get('description', 'Unknown error')}"


@mcp.tool(
    name="get_updates",
    description="Get recent messages sent to the bot. Use this to discover chat IDs.",
)
def get_updates(
    limit: int = Field(default=10, description="Maximum number of updates to return"),
) -> str:
    response = httpx.get(
        f"{BASE_URL}/getUpdates",
        params={"limit": limit},
    )
    data = response.json()
    if not data.get("ok"):
        return f"Error: {data.get('description', 'Unknown error')}"

    updates = data["result"]
    if not updates:
        return "No updates. Send any message to your bot first to get your chat ID."

    lines = []
    for update in updates:
        msg = update.get("message", {})
        chat = msg.get("chat", {})
        sender = msg.get("from", {})
        name = sender.get("username") or sender.get("first_name", "unknown")
        lines.append(
            f"chat_id={chat.get('id')}  from=@{name}  text={msg.get('text', '')!r}"
        )
    return "\n".join(lines)


@mcp.tool(
    name="get_me",
    description="Get basic information about the bot.",
)
def get_me() -> str:
    response = httpx.get(f"{BASE_URL}/getMe")
    data = response.json()
    if data.get("ok"):
        bot = data["result"]
        return f"@{bot['username']}  id={bot['id']}  name={bot['first_name']}"
    return f"Error: {data.get('description', 'Unknown error')}"


_TELEGRAM_LIMIT = 4096


@mcp.tool(
    name="send_token_report",
    description=(
        "Run token_monitor.py and send the output to Telegram. "
        "Subcommands: report, limits, meter-report, codex-meter-report, status, bursts. "
        "For 'report': supports window, since, by, model_filter, session_filter, project_filter, io_only. "
        "Examples: filter by model 'claude-fable-5' → model_filter='claude-fable-5'; "
        "last 5h → window='5h'; since a date → since='2026-06-01'; "
        "subscription vs credits → by='billing'; io_only=True for in/out only."
    ),
)
def send_token_report(
    command: str = Field(default="report", description="Subcommand: report, limits, meter-report, codex-meter-report, status, bursts"),
    window: str = Field(default="week", description="Time window: 5h, day, week, month (report only, ignored if since is set)"),
    since: str = Field(default="", description="Start date ISO YYYY-MM-DD, overrides window (report only)"),
    by: str = Field(default="none", description="Group by: model, session, project, day, billing, none (report only)"),
    model_filter: str = Field(default="", description="Filter by exact model name, e.g. claude-fable-5 (report only)"),
    session_filter: str = Field(default="", description="Filter by session_id prefix (report only)"),
    project_filter: str = Field(default="", description="Filter by project substring (report only)"),
    io_only: bool = Field(default=False, description="Show only input/output tokens, no cache/cost (report only)"),
    limit: int = Field(default=50, description="Max rows returned (limits and meter-report only)"),
    chat_id: str = Field(default="", description="Telegram chat ID. Uses default if omitted."),
) -> str:
    cmd = [sys.executable, TOKEN_MONITOR, command]
    if command == "report":
        if since:
            cmd += ["--since", since]
        else:
            cmd += ["--window", window]
        cmd += ["--by", by]
        if model_filter:
            cmd += ["--model", model_filter]
        if session_filter:
            cmd += ["--session", session_filter]
        if project_filter:
            cmd += ["--project", project_filter]
        if io_only:
            cmd += ["--io-only"]
    elif command in ("limits", "meter-report", "codex-meter-report"):
        cmd += ["--limit", str(limit)]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        output = (result.stdout or "") + (result.stderr or "")
    except FileNotFoundError:
        return f"Error: token_monitor.py not found at {TOKEN_MONITOR}"
    except subprocess.TimeoutExpired:
        return "Error: token_monitor.py timed out after 60s"

    if not output.strip():
        return "Error: token_monitor.py returned no output"

    target = chat_id or DEFAULT_CHAT_ID
    if not target:
        return "Error: no chat_id provided and TELEGRAM_CHAT_ID is not set"

    text = f"```\n{output.strip()}\n```"
    if len(text) > _TELEGRAM_LIMIT:
        truncated = output.strip()[: _TELEGRAM_LIMIT - 200]
        text = f"```\n{truncated}\n… (truncado)\n```"

    response = httpx.post(
        f"{BASE_URL}/sendMessage",
        json={"chat_id": target, "text": text, "parse_mode": "Markdown"},
    )
    data = response.json()
    if data.get("ok"):
        return f"Report sent (id={data['result']['message_id']})"
    return f"Error sending to Telegram: {data.get('description', 'Unknown error')}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
