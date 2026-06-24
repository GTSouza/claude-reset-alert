# claude-reset-alert

Toolkit para **monitorar e governar o uso do Claude Code** (assinatura + créditos): avisa quando os limites resetam, mede o consumo real de tokens/custo, decide quando pausar para não bater rate limit e expõe tudo no Telegram.

## Componentes

| Componente | O que faz | Docs |
|---|---|---|
| **Watchers de limite** (`claude-limit-watch.{sh,linux.sh,ps1}`) | Vigiam o `/usage` em loop e notificam reset / liberação de cota (desktop + som + Telegram). Um script por SO. | esta página |
| **Monitor de tokens** ([`cnp/token_monitor.py`](cnp/token_monitor.py)) | Lê os transcripts `.jsonl`, agrega uso por janela/modelo/sessão/billing em SQLite, mede o `/usage` (custo zero), calibra custo real e decide GO/PAUSE via `gate`. | [seção abaixo](#monitor-de-tokens-cnptoken_monitorpy) |
| **Telegram MCP** ([`cnp/`](cnp/)) | Servidor MCP + bridge que envia relatórios e responde no Telegram via Claude. | [cnp/README.md](cnp/README.md) |
| **Skill runner seguro** ([`ultracode-continuous-safe-runner/`](ultracode-continuous-safe-runner/SKILL.md)) | Skill de trabalho contínuo, incremental e retomável que consulta o `gate` antes de cada lote e pausa com checkpoint antes do teto. | [seção abaixo](#skill-runner-contínuo-seguro) |

Os watchers e o `token_monitor.py` compartilham o mesmo arquivo de log (`~/.claude/limit-watch/watch.log`): o monitor importa o histórico do watcher para a tabela `meter`.

---

## Watchers de limite (shell)

Monitora os limites de uso do **Claude Code** (assinatura) e te avisa quando eles **resetam** ou quando a cota **libera antes do horário previsto**.

A cada checagem o script roda `claude -p "/usage"` de forma não-interativa — chamada de **custo zero**, que não consome tokens nem cota — e lê algo como:

```
Current session: 15% used · resets Jun 9 at 11:50pm (America/Sao_Paulo)
Current week (all models): 2% used · resets Jun 15 at 9pm (America/Sao_Paulo)
Current week (Sonnet only): 0% used
```

- **Current session** → janela de **5 horas**
- **Current week (all models)** → janela **semanal**

> ℹ️ A resposta do `/usage` em modo `-p` é **gerada pelo modelo**, então às vezes vem vazia ou sem os percentuais. Por isso cada ciclo tenta ler até `FETCH_RETRIES` vezes (padrão 3) antes de pular o ciclo; leituras inválidas são ignoradas sem disparar alerta falso.

## Como ele detecta um reset

Para cada janela (5h e semanal), dispara uma notificação quando:

1. **O horário de reset avança** além de `RESET_TOLERANCE` → começou uma nova janela; ou
2. **O % de uso cai** além de `DROP_THRESHOLD` → a cota liberou antes do previsto.

> O texto do `/usage` arredonda os minutos a cada consulta (ex.: `3pm` ↔ `2:59pm`), então a detecção compara o **instante** do reset com tolerância (`RESET_TOLERANCE`, padrão 600s) em vez do texto literal — assim variações de poucos minutos não geram falsos alertas de reset.

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
| `INTERVAL`       | `300`                       | Intervalo entre checagens, em segundos (modo `watch`). Padrão: 5 minutos. |
| `MODEL`          | `haiku`                     | Modelo usado na chamada `/usage`. |
| `FETCH_RETRIES`  | `3`                         | Tentativas de ler o `/usage` por ciclo até obter os percentuais (a resposta é gerada pelo modelo e às vezes vem vazia/genérica). |
| `DROP_THRESHOLD` | `5`                         | Queda mínima de % para considerar que a cota liberou. |
| `RESET_TOLERANCE`| `600`                       | Segundos de variação no horário de reset que **não** contam como nova janela (evita falso positivo de `3pm` vs `2:59pm`). |
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

Exemplos (macOS/Linux):

```bash
INTERVAL=60 ./claude-limit-watch.sh            # checa a cada 1 minuto (padrão é 300 = 5 min)
DROP_THRESHOLD=10 ./claude-limit-watch.sh once # só alerta se o uso cair >10%
```

### Onde colocar as credenciais

O script carrega as variáveis automaticamente, nesta ordem (a primeira que existir já vale):

1. `~/.claude/limit-watch/telegram.env`
2. `.env` na pasta do script

Para o Telegram, aceita tanto `TG_TOKEN`/`TG_CHAT_ID` quanto `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`.

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

## Rodar automaticamente (na inicialização)

Para o watcher subir junto com o sistema e reiniciar sozinho:

### macOS — launchd

Crie `~/Library/LaunchAgents/com.claude.limitwatch.plist` (ajuste o caminho do script):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>            <string>com.claude.limitwatch</string>
  <key>ProgramArguments</key> <array>
    <string>/caminho/para/claude-limit-watch.sh</string>
  </array>
  <key>RunAtLoad</key>        <true/>
  <key>KeepAlive</key>        <true/>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.claude.limitwatch.plist
```

### Linux — systemd (user service)

Crie `~/.config/systemd/user/claude-limit-watch.service`:

```ini
[Unit]
Description=Claude Code limit watcher

[Service]
ExecStart=/caminho/para/claude-limit-watch-linux.sh
Restart=always

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now claude-limit-watch.service
journalctl --user -u claude-limit-watch -f   # acompanhar
```

### Windows — Agendador de Tarefas

Crie uma tarefa que roda ao logon, em segundo plano:

```powershell
$action  = New-ScheduledTaskAction -Execute 'powershell.exe' `
  -Argument '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "C:\caminho\para\claude-limit-watch.ps1"'
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName 'ClaudeLimitWatch' -Action $action -Trigger $trigger
```

## Estado e log

- **Log:** `~/.claude/limit-watch/watch.log`
- **Estado das janelas:** arquivos `last_session_*` e `last_week_*` em `~/.claude/limit-watch/`, usados para comparar a checagem atual com a anterior.

Para "resetar" a memória do script (e tratar a próxima checagem como a primeira), apague os arquivos de estado:

```bash
rm ~/.claude/limit-watch/last_*
```

---

## Monitor de tokens (`cnp/token_monitor.py`)

Enquanto os watchers só olham os **percentuais** do `/usage`, o `token_monitor.py` lê os transcripts `.jsonl` que o Claude Code grava em `~/.claude/projects/`, agrega o uso em **SQLite** (`~/.claude/tools/token_usage.db`) e cruza com o medidor oficial. Sem dependências externas (só stdlib).

Por convenção fica em `~/.claude/tools/token_monitor.py` (a cópia versionada é [`cnp/token_monitor.py`](cnp/token_monitor.py) — mantenha as duas em sincronia):

```bash
cp cnp/token_monitor.py ~/.claude/tools/token_monitor.py
chmod +x ~/.claude/tools/token_monitor.py   # permite a invocação direta `token_monitor.py gate` (shebang)
```

### Subcomandos

| Comando | Para que serve |
|---|---|
| `ingest` | Varre os `.jsonl` + `watch.log` e popula o banco (idempotente, incremental). |
| `report` | Relatório agregado por janela (`--window 5h/day/week/month`) e eixo (`--by model/session/project/day/billing/none`). `--io-only` mostra só in/out (comparável ao app do Claude). |
| `limits` | Episódios em que você bateu o limite. |
| `meter` | Uma leitura do `/usage` do Claude (custo zero) — **e o Codex junto, se houver rollouts** (`--no-codex` desliga); grava na tabela `meter` e alerta reset/queda/cap. `--watch --interval 300` roda em loop. |
| `meter-report` | Histórico das leituras do medidor. |
| `codex-meter` | Uma leitura do rate-limit do **Codex** a partir dos rollouts `~/.codex/sessions` (custo zero); grava na tabela `codex_meter` e alerta reset/queda/cap. `--watch --interval 300` roda em loop. |
| `codex-meter-report` | Histórico das leituras do medidor do Codex. |
| `status` | Resumo num olhar: Claude + Codex (5h/semanal) + veredito do `gate --provider both`. |
| **`gate`** | **Veredito GO/PAUSE de rate limit** para runners; `--provider {claude,codex,both}` (default `claude`) — ver abaixo. |
| `calibrate` | Aprende o fator de custo real por modelo a partir de gastos reais de crédito (`--brl`/`--usd`, `--solve`, `--apply`). |
| `bursts` | Detalha clusters de atividade (gatilho manual × wakeup × task, billing, modelos, cap). |

> **Billing assinatura × crédito:** o monitor marca como `credits` o uso real produzido **depois** do medidor 5h ser confirmado em 100%, e como `subscription` o resto. Os relatórios `--by billing` e os custos `~USD` (calibrados via `calibrate`) saem desse cruzamento.

### Gate de rate limit (para runners)

O `gate` é o ponto de decisão único "**posso seguir trabalhando?**". Lê a última leitura do medidor do banco (custo zero) e só refaz uma leitura ao vivo se a anterior estiver velha (`--max-age`, padrão 300s). Aplica os tetos de 5h e semanal **juntos** e já entrega tudo mastigado:

```bash
~/.claude/tools/token_monitor.py gate            # texto: DECISION + conselho + bloco de pausa pronto
~/.claude/tools/token_monitor.py gate --json     # mesmos campos (advice, pause_header, ...) p/ automação
```

Saída em `PAUSE`:

```text
📊 5h: 95% · reset Jun 11 at 4:39pm (America/Sao_Paulo)  |  semanal: 80% · reset ...  (cache 199s)
motivo: 5h em 95% (>= 80%)
DECISION: PAUSE
🛑 Pare. Não inicie novas tarefas. Faça commit do que está validado e responda no formato de pausa abaixo...
─────── cole e complete ───────
pausado: <N> tarefas restantes
motivo: 5h em 95% (>= 80%)
reset: Jun 11 at 4:39pm (America/Sao_Paulo)
semanal: 80%
────────────────────────────────
```

**Exit codes:** `0` = GO · `10` = PAUSE · `2` = UNKNOWN (sem leitura — trate como pausa).

| Flag | Padrão | Descrição |
|---|---|---|
| `--max-5h` | `80` | Teto da janela de 5h em %. |
| `--max-week` | `90` | Teto semanal em %. |
| `--max-age` | `300` | Segundos: leitura mais velha que isto dispara uma medição ao vivo. |
| `--refresh` | — | Força a leitura ao vivo do `/usage` agora. |
| `--json` | — | Saída estruturada em vez de texto. |
| `--no-notify` | — | Não notifica ao refazer a leitura. |

### Codex (rate-limit)

Além do Claude Code, o monitor mede o uso do **Codex CLI** — tudo **aditivo e backward-compatible** (nada do fluxo do Claude muda). O Codex não tem um `/usage`, mas grava os rate limits da conta em cada rollout de sessão (`~/.codex/sessions/AAAA/MM/DD/rollout-*.jsonl`), num evento `token_count` com `rate_limits.primary` (janela de **5h**) e `rate_limits.secondary` (**semanal**), cada um com `used_percent` + `resets_at`. O monitor lê o rollout mais fresco **ao vivo, custo zero** (vale "de quando o Codex rodou pela última vez").

```bash
~/.claude/tools/token_monitor.py codex-meter          # 1 leitura do rate-limit do Codex; grava em codex_meter + alerta
~/.claude/tools/token_monitor.py codex-meter --watch --interval 300   # loop; alerta reset/queda/cap
~/.claude/tools/token_monitor.py codex-meter --json   # saída estruturada
~/.claude/tools/token_monitor.py codex-meter --refresh   # confirma ao vivo (1 turno mínimo do codex exec) só se um reset já cruzou; --force sempre
~/.claude/tools/token_monitor.py codex-meter-report   # histórico (tabela codex_meter)
```

A detecção de evento é a mesma do medidor do Claude (compara com a leitura anterior na tabela): **reset** quando o horário de reset avança além de `RESET_TOLERANCE`, **drop** quando o % cai além de `DROP_THRESHOLD`, **cap** quando a janela de 5h cruza 100%. Cada evento dispara desktop + som + Telegram.

#### Snapshot estático, inferência de reset e `--refresh`

O rollout é um **snapshot da última vez que o Codex rodou** — não se atualiza sozinho (o `/status` da TUI mostra o mesmo dado, só parece "ao vivo" porque a sessão está ativa). Para não ficar preso num valor velho, o medidor faz duas coisas:

- **Inferência de reset (no gate):** como `resets_at` é epoch absoluto, uma janela cujo reset já passou é tratada como recuperada (~0%) mesmo sem o Codex rodar. Sem isso, um `gate --provider codex` ficaria preso no % antigo para sempre e um runner gated em Codex nunca acordaria.
- **Confirmação ao vivo (`codex-meter --refresh`):** gasta **1 turno mínimo** do `codex exec` para obter o número real, mas **só quando um reset já cruzou desde o rollout** (antes disso a leitura ainda é válida, pois sem o Codex rodar o uso não muda). `--force` gasta sempre. Use fora de um loop ativo quando quiser confirmar o reset em vez de só inferir.

#### `meter` agora inclui o Codex

`meter` (a leitura do `/usage` do Claude) também mede o Codex por padrão, quando há rollouts — inclusive em `meter --watch`. A linha do Claude (`5h: N% usado · reset ...`, que o `ingest`/`watch.log` parseia) **fica inalterada**.

```bash
~/.claude/tools/token_monitor.py meter               # Claude + Codex (se houver rollouts)
~/.claude/tools/token_monitor.py meter --no-codex    # só Claude (comportamento antigo)
```

#### Gate por provider

O `gate` ganhou `--provider {claude,codex,both}`. **O default é `claude`** — veredito, exit codes e bloco de pausa idênticos. Com `--provider both`, aplica os tetos aos dois medidores e decide **PAUSE se Claude OU Codex estourar** (o cabeçalho usa a pior leitura).

```bash
~/.claude/tools/token_monitor.py gate                      # só Claude (default, inalterado)
~/.claude/tools/token_monitor.py gate --provider both      # PAUSE se Claude OU Codex estourar
~/.claude/tools/token_monitor.py gate --provider codex --json
```

Com dois providers cada leitura é prefixada (`[claude]`/`[codex]`) e o `motivo:` cita o provider (ex.: `codex 5h em 92% (>= 80%)`). O `--json` ganhou `provider` e `providers[]` (uma entrada por medidor); os campos de topo (`decision`/`advice`/`pause_header`/`session_*`/`week_*`) seguem o **mesmo contrato** e refletem a pior leitura.

> **Contratos preservados:** a linha `DECISION:`, os exit codes `0`/`10`/`2`, o bloco `─── cole e complete ───` e as chaves do `--json` não mudaram. A única diferença cosmética é o `motivo:` prefixar o provider (ex.: `claude 5h em X%`).

#### `status` — resumo num olhar

```bash
~/.claude/tools/token_monitor.py status                       # gate 5h<80% · semanal<90% (defaults)
~/.claude/tools/token_monitor.py status --max-5h 70 --max-week 85
```

```text
=== Status — Claude + Codex (gate 5h<80% · semanal<90%) ===

📊 Claude   5h: 95% · reset Jun 24 at 4:39pm   |   semanal: 80% · reset Jun 30 at 9am  🛑
📊 Codex    5h: 42% · reset Jun 24 at 6:10pm   |   semanal: 18% · reset Jun 30 at 9am   [pro, 312s]

🛑 gate(both): PAUSE — estourado: claude
```

#### Tabela `codex_meter` e env

O medidor do Codex grava na tabela nova `codex_meter` no mesmo SQLite, separada do `meter` do Claude (uma linha por leitura: `ts` UTC do rollout, plano, `session_pct`/`week_pct` + resets, e os eventos). O diretório de rollouts é configurável via `CODEX_SESSIONS_DIR` (default `~/.codex/sessions`); sem rollout recente com `rate_limits`, o medidor só reporta "sem leitura" e segue (não é erro).

---

## Skill: runner contínuo seguro

A skill [`ultracode-continuous-safe-runner`](ultracode-continuous-safe-runner/SKILL.md) orquestra trabalho **contínuo, incremental e retomável**: quebra a demanda em tarefas pequenas, valida e faz **um commit por tarefa**, consulta o `gate` a cada 1–2 tarefas e, em `PAUSE`, salva checkpoint e para antes de bater o limite. Toda a lógica de rate limit é delegada ao `gate` — a skill só executa o comando e segue a saída.
