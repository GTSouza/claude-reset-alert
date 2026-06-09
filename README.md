# claude-limit-watch

Monitora os limites de uso do **Claude Code** (assinatura) e te avisa quando eles **resetam** ou quando a cota **libera antes do horário previsto**.

A cada checagem o script roda `claude -p "/usage"` de forma não-interativa — chamada de **custo zero**, que não consome tokens nem cota — e lê algo como:

```
Current session: 15% used · resets Jun 9 at 11:50pm (America/Sao_Paulo)
Current week (all models): 2% used · resets Jun 15 at 9pm (America/Sao_Paulo)
Current week (Sonnet only): 0% used
```

- **Current session** → janela de **5 horas**
- **Current week (all models)** → janela **semanal**

## Como ele detecta um reset

Para cada janela (5h e semanal), dispara uma notificação quando:

1. **O horário de reset avança** para um novo valor → começou uma nova janela; ou
2. **O % de uso cai** além de `DROP_THRESHOLD` → a cota liberou antes do previsto.

Em ambos os casos você recebe: **notificação no desktop** + **som** (distinto por janela) + **mensagem no Telegram** (se configurado).

## Versões por plataforma

Há um script por sistema operacional, todos com os mesmos modos e a mesma configuração — muda só a forma de notificar:

| Sistema | Script | Notificação | Som |
|---------|--------|-------------|-----|
| macOS   | [`claude-limit-watch.sh`](claude-limit-watch.sh)       | `osascript` | `afplay` (sons de `/System/Library/Sounds`) |
| Linux   | [`claude-limit-watch-linux.sh`](claude-limit-watch-linux.sh) | `notify-send` | `canberra-gtk-play` / `paplay` / `aplay` |
| Windows | [`claude-limit-watch.ps1`](claude-limit-watch.ps1)     | Toast (Win10+) com fallback p/ balão | `System.Media.SystemSounds` |

## Pré-requisitos

Comuns a todos:

- **Claude Code CLI** (`claude`) autenticado na sua assinatura.

Por plataforma:

- **macOS** — `osascript` e `afplay` (nativos). `jq` (opcional, parse mais robusto), `curl` (opcional, Telegram).
- **Linux** — `notify-send` (pacote `libnotify-bin`) e, para som, `canberra-gtk-play` (`libcanberra-gtk-module`), `paplay` ou `aplay`. `jq` e `curl` opcionais como acima.
- **Windows** — **PowerShell 5+** (ou PowerShell 7). Notificações e Telegram são nativos (não precisa de `curl`/`jq`).

## Uso

### macOS

```bash
chmod +x claude-limit-watch.sh

./claude-limit-watch.sh          # vigia em loop e notifica (modo padrão: watch)
./claude-limit-watch.sh once     # checa o estado atual uma vez e sai
./claude-limit-watch.sh getid    # ajuda a descobrir o chat_id do Telegram
```

Rodar em background com log:

```bash
nohup ./claude-limit-watch.sh > /dev/null 2>&1 &
tail -f ~/.claude/limit-watch/watch.log
```

### Linux

```bash
chmod +x claude-limit-watch-linux.sh

./claude-limit-watch-linux.sh          # watch
./claude-limit-watch-linux.sh once
./claude-limit-watch-linux.sh getid

# em background:
nohup ./claude-limit-watch-linux.sh > /dev/null 2>&1 &
tail -f ~/.claude/limit-watch/watch.log
```

### Windows (PowerShell)

```powershell
# se a execução de scripts estiver bloqueada, libere só para esta sessão:
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

.\claude-limit-watch.ps1            # watch
.\claude-limit-watch.ps1 once
.\claude-limit-watch.ps1 getid

# variáveis de ambiente são definidas com $env: antes de chamar:
$env:INTERVAL = 120; .\claude-limit-watch.ps1
```

## Configuração

Tudo é controlado por variáveis de ambiente (com valores padrão):

| Variável         | Padrão                      | Descrição |
|------------------|-----------------------------|-----------|
| `INTERVAL`       | `60`                        | Intervalo entre checagens, em segundos (modo `watch`). |
| `MODEL`          | `haiku`                     | Modelo usado na chamada `/usage`. |
| `DROP_THRESHOLD` | `5`                         | Queda mínima de % para considerar que a cota liberou. |
| `SOUND_5H`       | macOS `Ping` · Linux `message-new-instant` · Win `Asterisk` | Som da notificação da janela de 5h. |
| `SOUND_WEEK`     | macOS `Submarine` · Linux `complete` · Win `Exclamation`    | Som da notificação da janela semanal. |
| `STATE_DIR`      | `~/.claude/limit-watch`     | Onde ficam o log e o estado das janelas. |
| `TG_ENV_FILE`    | `$STATE_DIR/telegram.env`   | Arquivo carregado automaticamente com as credenciais do Telegram. |
| `TG_TOKEN`       | —                           | Token do bot (do `@BotFather`). Alias: `TELEGRAM_BOT_TOKEN`. |
| `TG_CHAT_ID`     | —                           | Um ou mais chat_ids separados por vírgula. Alias: `TELEGRAM_CHAT_ID`. |

Valores de som por plataforma:

- **macOS** — nome de arquivo em `/System/Library/Sounds` (ex.: `Glass`, `Ping`, `Submarine`, `Hero`, `Sosumi`).
- **Linux** — nome de evento do tema freedesktop (ex.: `message-new-instant`, `complete`, `bell`, `dialog-warning`).
- **Windows** — nome de som do sistema (`Asterisk`, `Beep`, `Exclamation`, `Hand`, `Question`).

### Onde colocar as credenciais

O script carrega as variáveis automaticamente, nesta ordem (a primeira que existir já vale):

1. `~/.claude/limit-watch/telegram.env`
2. `.env` na pasta do script

Para o Telegram, aceita tanto `TG_TOKEN`/`TG_CHAT_ID` quanto `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`.

Exemplos:

```bash
INTERVAL=120 ./claude-limit-watch.sh           # checa a cada 2 minutos
DROP_THRESHOLD=10 ./claude-limit-watch.sh once # só alerta se o uso cair >10%
```

## Notificações no Telegram (opcional)

1. Crie um bot com o [@BotFather](https://t.me/BotFather) e copie o **token**.
2. Mande qualquer mensagem para o bot (ou adicione-o a um grupo e mande uma mensagem lá).
3. Descubra o `chat_id`:

   ```bash
   TG_TOKEN=seu_token ./claude-limit-watch.sh getid
   ```

4. Salve as credenciais para o script carregar sozinho. Crie um `.env` na pasta
   do script (ou `~/.claude/limit-watch/telegram.env`):

   ```
   TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
   TELEGRAM_CHAT_ID=123456789
   ```

   > Os nomes `TG_TOKEN`/`TG_CHAT_ID` também funcionam. O `.env` da pasta do
   > script **já está no `.gitignore`** — não comite suas credenciais.

   O chat_id aceita usuário, **grupo** (número negativo), canal (`@canal`) e vários valores separados por vírgula: `123,-100456,@canal`.

## Estado e log

- **Log:** `~/.claude/limit-watch/watch.log`
- **Estado das janelas:** arquivos `last_session_*` e `last_week_*` em `~/.claude/limit-watch/`, usados para comparar a checagem atual com a anterior.

Para "resetar" a memória do script (e tratar a próxima checagem como a primeira), apague os arquivos de estado:

```bash
rm ~/.claude/limit-watch/last_*
```
