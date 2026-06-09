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

Em ambos os casos você recebe: **notificação no macOS** + **som** (distinto por janela) + **mensagem no Telegram** (se configurado).

## Pré-requisitos

- **Claude Code CLI** (`claude`) autenticado na sua assinatura.
- **macOS** para as notificações nativas (`osascript`) e o som (`afplay`). Em outros sistemas o script roda, mas só registra no log / Telegram.
- `jq` (opcional, recomendado) para parse mais robusto da saída. Sem ele, há um fallback com `sed`.
- `curl` (opcional) — apenas para as notificações via Telegram.

## Uso

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

## Configuração

Tudo é controlado por variáveis de ambiente (com valores padrão):

| Variável         | Padrão                      | Descrição |
|------------------|-----------------------------|-----------|
| `INTERVAL`       | `60`                        | Intervalo entre checagens, em segundos (modo `watch`). |
| `MODEL`          | `haiku`                     | Modelo usado na chamada `/usage`. |
| `DROP_THRESHOLD` | `5`                         | Queda mínima de % para considerar que a cota liberou. |
| `SOUND_5H`       | `Ping`                      | Som da notificação da janela de 5h. |
| `SOUND_WEEK`     | `Submarine`                 | Som da notificação da janela semanal. |
| `STATE_DIR`      | `~/.claude/limit-watch`     | Onde ficam o log e o estado das janelas. |
| `TG_ENV_FILE`    | `$STATE_DIR/telegram.env`   | Arquivo carregado automaticamente com as credenciais do Telegram. |
| `TG_TOKEN`       | —                           | Token do bot (do `@BotFather`). |
| `TG_CHAT_ID`     | —                           | Um ou mais chat_ids separados por vírgula. |

Os sons disponíveis estão em `/System/Library/Sounds` (ex.: `Glass`, `Ping`, `Submarine`, `Hero`, `Sosumi`).

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

4. Salve as credenciais em `~/.claude/limit-watch/telegram.env` para o script carregar sozinho:

   ```
   TG_TOKEN=123456:ABC-DEF...
   TG_CHAT_ID=123456789
   ```

   O `TG_CHAT_ID` aceita usuário, **grupo** (número negativo), canal (`@canal`) e vários valores separados por vírgula: `123,-100456,@canal`.

## Estado e log

- **Log:** `~/.claude/limit-watch/watch.log`
- **Estado das janelas:** arquivos `last_session_*` e `last_week_*` em `~/.claude/limit-watch/`, usados para comparar a checagem atual com a anterior.

Para "resetar" a memória do script (e tratar a próxima checagem como a primeira), apague os arquivos de estado:

```bash
rm ~/.claude/limit-watch/last_*
```
