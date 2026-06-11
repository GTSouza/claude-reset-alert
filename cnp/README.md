# Telegram MCP

MCP server que expõe ferramentas para interagir com a API do Telegram Bot, mais um cliente de demonstração.

## Pré-requisitos

- Python 3.10+
- Um bot criado via [@BotFather](https://t.me/BotFather)

## Setup

### 1. Configure o `.env`

```
TELEGRAM_BOT_TOKEN=<token do BotFather>
TELEGRAM_CHAT_ID=<seu chat ID — veja como obter abaixo>
```

### 2. Instale as dependências

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 3. Descubra seu `chat_id`

1. Mande qualquer mensagem para o seu bot no Telegram
2. Rode o cliente:
   ```bash
   python telegram_mcp_client.py
   ```
3. O `chat_id` aparece na seção "Recent updates" — copie e cole no `.env`

### 4. Envie uma mensagem de teste

Com o `TELEGRAM_CHAT_ID` preenchido, rode novamente:

```bash
python telegram_mcp_client.py
```

## Ferramentas disponíveis

| Tool | Descrição |
|---|---|
| `send_message` | Envia uma mensagem de texto para um chat (suporta Markdown) |
| `get_updates` | Lista as mensagens recentes recebidas pelo bot |
| `get_me` | Retorna informações básicas do bot |
| `send_token_report` | Executa o `token_monitor.py` e envia o resultado ao Telegram |

## Uso

### Cliente de demonstração

```bash
python telegram_mcp_client.py
```

### Servidor standalone (para testes)

```bash
python telegram_mcp_server.py
```

## Integração com Claude

### Claude desktop app

Edite `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "telegram": {
      "command": "/caminho/para/.venv/bin/python",
      "args": ["/caminho/para/telegram_mcp_server.py"],
      "env": {
        "TELEGRAM_BOT_TOKEN": "<seu token>"
      }
    }
  }
}
```

Reinicie o Claude app após salvar.

### Claude Code (CLI)

```bash
claude mcp add telegram \
  -e TELEGRAM_BOT_TOKEN=<seu token> \
  -e TELEGRAM_CHAT_ID=<seu chat_id> \
  -- /caminho/para/.venv/bin/python /caminho/para/telegram_mcp_server.py
```

Para adicionar globalmente (todos os projetos):

```bash
claude mcp add telegram -s user \
  -e TELEGRAM_BOT_TOKEN=<seu token> \
  -e TELEGRAM_CHAT_ID=<seu chat_id> \
  -- /caminho/para/.venv/bin/python /caminho/para/telegram_mcp_server.py
```

Verifique com:

```bash
claude mcp list
```

### Como usar no Claude Code

Com o servidor conectado, basta pedir em linguagem natural:

#### Mensagens simples

```
Manda uma mensagem no Telegram: deploy concluído com sucesso
```

```
Avisa no Telegram que a reunião começa em 10 minutos
```

```
Manda pro chat <chat_id> no Telegram: alerta de erro no serviço X
```

O `chat_id` padrão (definido em `TELEGRAM_CHAT_ID`) é usado automaticamente.

#### Relatórios de tokens via `send_token_report`

A tool `send_token_report` executa o `~/.claude/tools/token_monitor.py` e envia o resultado formatado direto no Telegram.

**Relatório da semana agrupado por modelo:**
```
Manda o relatório de tokens da semana no Telegram
```

**Relatório das últimas 5h agrupado por sessão:**
```
Manda pro Telegram o uso de tokens das últimas 5h por sessão
```

**Relatório do dia agrupado por projeto:**
```
Envia o relatório de hoje por projeto no Telegram
```

**Batidas de limite:**
```
Manda no Telegram os episódios de rate limit
```

**Histórico do medidor oficial (/usage):**
```
Envia o meter-report no Telegram
```

**Detalhamento de bursts de atividade:**
```
Manda o relatório de bursts no Telegram
```

##### Parâmetros da tool

| Parâmetro | Opções | Padrão | Descrição |
|---|---|---|---|
| `command` | `report`, `limits`, `meter-report`, `bursts` | `report` | Subcomando do token_monitor |
| `window` | `5h`, `day`, `week`, `month` | `week` | Janela de tempo (só para `report`) |
| `by` | `model`, `session`, `project`, `day`, `billing`, `none` | `model` | Eixo de agrupamento (só para `report`) |
| `chat_id` | qualquer chat ID | `TELEGRAM_CHAT_ID` | Destinatário (usa o padrão se omitido) |

## Ponte Telegram ↔ Claude Code (`telegram_bridge.py`)

A bridge fica em loop contínuo ouvindo o bot. Cada mensagem recebida dispara
`claude -p` em subprocess; Claude usa as MCP tools para responder direto no Telegram.

```
Você (Telegram) → bot → bridge → claude -p → MCP send_message → você (Telegram)
```

### Requisito: permissões do Claude Code

A bridge spawna `claude -p --dangerously-skip-permissions` para que Claude execute as MCP tools automaticamente, sem parar para pedir confirmação. Isso é necessário para o loop funcionar sem interação humana.

> **Nota de segurança:** use `--allowed` para restringir quais chat_ids podem controlar a bridge.

### Iniciar a bridge

```bash
# Processa mensagens de qualquer chat
python telegram_bridge.py

# Restringe a chat_ids específicos (recomendado)
python telegram_bridge.py --allowed <seu_chat_id>

# Usa modelo mais rápido para respostas mais ágeis
python telegram_bridge.py --allowed <seu_chat_id> --model haiku
```

### Exemplos de mensagens para o bot

Uma vez com a bridge rodando, envie mensagens ao bot normalmente:

```
Manda o relatório de tokens da semana
```
```
Qual é o status do bot?
```
```
Me mostra os episódios de rate limit
```
```
Relatório de hoje por modelo
```

### Flags

| Flag | Padrão | Descrição |
|---|---|---|
| `--allowed CHAT_ID ...` | todos | Chat IDs autorizados a interagir com a bridge |
| `--model MODEL` | config do Claude Code | Modelo usado pelo Claude (ex.: `haiku`, `sonnet`) |

> Internamente, a bridge sempre passa `--dangerously-skip-permissions` ao `claude -p` para evitar prompts de confirmação no loop.

### Rodar como serviço (launchd no macOS)

Crie `~/Library/LaunchAgents/com.telegram-bridge.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.telegram-bridge</string>
  <key>ProgramArguments</key>
  <array>
    <string>/caminho/para/.venv/bin/python</string>
    <string>/caminho/para/telegram_bridge.py</string>
    <string>--allowed</string>
    <string>SEU_CHAT_ID</string>
    <string>--model</string>
    <string>haiku</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/caminho/para/cnp</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/telegram-bridge.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/telegram-bridge.err</string>
</dict>
</plist>
```

Ative:
```bash
launchctl load ~/Library/LaunchAgents/com.telegram-bridge.plist
```

## Estrutura

```
telegram_mcp_server.py   # Servidor MCP com as tools do Telegram
telegram_mcp_client.py   # Cliente de demonstração
telegram_bridge.py       # Bridge Telegram ↔ Claude Code (loop contínuo)
.env                     # Credenciais (não commitado)
pyproject.toml           # Dependências
```
