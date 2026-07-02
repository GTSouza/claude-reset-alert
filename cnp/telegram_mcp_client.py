import asyncio
import sys
import os
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()


def _tool_text(result) -> str:
    return result.content[0].text if result.content else "(empty)"


async def main():
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    # caminho absoluto: rodar o cliente de fora de cnp/ (ex.: da raiz do repo) não pode
    # quebrar o spawn do server com "can't open file"
    server_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "telegram_mcp_server.py")
    command, args = (
        ("uv", ["run", server_path])
        if os.getenv("USE_UV", "0") == "1"
        else (sys.executable, [server_path])
    )

    server_params = StdioServerParameters(command=command, args=args)

    async with stdio_client(server_params) as (stdio, write):
        async with ClientSession(stdio, write) as session:
            await session.initialize()

            tools_result = await session.list_tools()
            print("Available tools:")
            for tool in tools_result.tools:
                print(f"  {tool.name}: {tool.description}")
            print()

            result = await session.call_tool("get_me", {})
            print(f"Bot info: {_tool_text(result)}")
            print()

            result = await session.call_tool("get_updates", {"limit": 5})
            print("Recent updates:")
            print(_tool_text(result))
            print()

            if chat_id:
                result = await session.call_tool(
                    "send_message",
                    {"chat_id": chat_id, "text": "Hello from MCP! 🤖"},
                )
                print(f"Send result: {_tool_text(result)}")
            else:
                print(
                    "Tip: set TELEGRAM_CHAT_ID in .env with a chat ID from the updates above"
                    " and re-run to send a test message."
                )


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
