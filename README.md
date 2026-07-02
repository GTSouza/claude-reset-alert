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
2. **O % de uso cai** além de `DROP_THRESHOLD` → a cota liberou ou a janela resetou.

No **shell** a notificação é simples: avanço de horário → **"resetou ✅"**; queda de % → **"liberou ⬇️"** com "Uso caiu de X% para Y%" (sem distinguir 0% vs parcial nem marcar ⚠️).

> O subcomando [`meter`](#monitor-de-tokens-cnptoken_monitorpy) do `token_monitor.py` usa um esquema **mais rico** para os mesmos eventos: 🟢 resetou (natural) · 🟠 reset **ANTECIPADO** · 🔵 liberou (>0%) · 🔴 cap 100% · 💳 crédito. Se você quer esses estados, rode o `meter --watch` em vez (ou além) do watcher shell.
>
> **Dois tipos de reset.** O reset **natural** cai no horário programado (ou depois). O reset **antecipado** (🟠) é quando a Anthropic zera a cota **antes** da janela prevista (`now < reset previsto − RESET_TOLERANCE`); a mensagem diz "era p/ &lt;horário&gt;". No momento do reset o `/usage` às vezes volta como `N% used` **sem** a linha `resets ...` (horário = None) por vários minutos — então, na transição, o `meter` faz uma releitura ao vivo de confirmação (haiku, quase custo zero; no máximo uma por `RECONFIRM_COOLDOWN`, padrão 10min, mesmo se o `/usage` oscilar; `METER_RECONFIRM=0` desliga) para recuperar o horário novo e separar um reset real de um glitch transitório (se a releitura traz de volta o valor antigo não-zero, foi glitch e é adotado). Se a releitura ainda vier sem horário, entra o **warm-up** (`METER_WARMUP=1`, padrão): um prompt mínimo no modelo mais barato **gasta um tiquinho da cota recém-resetada** para tirar a janela de 0% — o `/usage` parece omitir a linha `resets` com a janela ociosa em 0% — e relê uma vez buscando o horário REAL (`METER_WARMUP=0` desliga; este passo **não** é custo zero). Se o horário ainda não veio, ele é **estimado** uma vez (`now` + duração da janela: 5h / 7d) e marcado com `≈`, carregado adiante enquanto a janela seguir zerada até um poll saudável trazer o real (`METER_INFER_RESET=0` desliga a estimativa e deixa o horário desconhecido).

> O texto do `/usage` arredonda os minutos a cada consulta (ex.: `3pm` ↔ `2:59pm`), então a detecção compara o **instante** do reset com tolerância (`RESET_TOLERANCE`, padrão 600s) em vez do texto literal — assim variações de poucos minutos não geram falsos alertas de reset.

Em todos os casos você recebe: **notificação no desktop** + **som** (distinto por janela) + **mensagem no Telegram** (se configurado).

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

O script carrega os DOIS arquivos em sequência — quando ambos existem e definem a mesma chave, **o `.env` da pasta do script (carregado por último) tem a palavra final**:

1. `~/.claude/limit-watch/telegram.env`
2. `.env` na pasta do script *(sobrescreve o anterior em chaves repetidas)*

Não duplique chaves nos dois arquivos: quem rotaciona o token só no `telegram.env` mas mantém um `.env` antigo na pasta do repo segue usando o token velho (e o envio falha em silêncio, só logado no `watch.log`). Para o Telegram, aceita tanto `TG_TOKEN`/`TG_CHAT_ID` quanto `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`.

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

Por convenção fica em `~/.claude/tools/token_monitor.py` (a cópia versionada é [`cnp/token_monitor.py`](cnp/token_monitor.py) — mantenha as duas em sincronia). Use o [`deploy.sh`](deploy.sh), que valida (`py_compile`) antes de copiar e dá `chmod +x`:

```bash
./deploy.sh   # cnp/token_monitor.py → ~/.claude/tools/token_monitor.py (override: CLAUDE_TOOLS_DIR)
```

> Reinicie qualquer `--watch` em execução após o deploy: um loop já rodando mantém o código antigo carregado em memória.

### Hooks do Claude Code (manter o banco fresco)

Para o banco ficar sempre atual sem rodar nada à mão, o [`token_monitor_hook.sh`](token_monitor_hook.sh) roda o `ingest` nos eventos de sessão do Claude Code. O `deploy.sh` publica o wrapper junto do monitor em `~/.claude/tools/`.

Ele só usa subcomandos **baratos e sem efeito colateral** — `ingest` (transcripts + billing + uso do Codex) e, no início da sessão, `codex-meter --no-notify` (snapshot do rollout do Codex, custo zero). **Nunca** usa `meter`/`gate`/`status`, que chamariam `claude -p /usage` (um `claude` aninhado por sessão). É **mudo** (toda saída vai para `/dev/null` — no `SessionStart` o stdout entraria no contexto) e **sempre sai 0** (um banco travado ou um `.jsonl` a meio de escrita viram no-op, nunca falham a sessão).

Registre no `~/.claude/settings.json` (global) apontando para o wrapper publicado:

```json
{
  "hooks": {
    "SessionStart": [{ "hooks": [{ "type": "command", "command": "~/.claude/tools/token_monitor_hook.sh SessionStart", "timeout": 60 }] }],
    "Stop":         [{ "hooks": [{ "type": "command", "command": "~/.claude/tools/token_monitor_hook.sh Stop", "timeout": 45 }] }],
    "PreCompact":   [{ "hooks": [{ "type": "command", "command": "~/.claude/tools/token_monitor_hook.sh PreCompact", "timeout": 45 }] }]
  }
}
```

Use o caminho absoluto do wrapper no `command`. O `SessionStart` mantém o banco fresco a cada início/retomada; o `Stop` captura o uso do turno que acabou; o `PreCompact` grava o estado antes da compactação. A leitura ao vivo do `/usage` do Claude continua sendo trabalho do `meter --watch` / dos watchers shell — os hooks não a substituem.

### Subcomandos

| Comando | Para que serve |
|---|---|
| `ingest` | Varre os `.jsonl` + `watch.log` e popula o banco (idempotente, incremental). Deduplica por `message.id`: cada mensagem da API aparece em várias linhas do transcript (uma por bloco de conteúdo) com o mesmo `usage` — contar por linha inflaria tudo ~2-3x. |
| `rebuild-usage` | Reconstrói a tabela `usage` com o dedup por `message.id` (migra um banco da era pré-dedup): faz backup, re-ingere as sessões com transcript em disco, deduplica órfãs por heurística e recalcula/re-aplica a calibração (`--no-calibrate` pula). |
| `watch` | `ingest` contínuo em loop (`--interval`, padrão 60s) — mantém o banco fresco sem hooks/cron externos. |
| `report` | Relatório agregado por janela (`--window 5h/day/week/month`) e eixo (`--by model/session/project/day/billing/none`). `--io-only` mostra só in/out (comparável ao app do Claude). |
| `codex-report` | Uso de tokens do **Codex** por sessão/dia/modelo (tokens + tempo ativo; assinatura, sem custo/token) — ver seção própria abaixo. |
| `limits` | Episódios em que você bateu o limite. |
| `meter` | Uma leitura do `/usage` do Claude (custo zero) — **e o Codex junto, se houver rollouts** (`--no-codex` desliga); grava na tabela `meter` e alerta reset/queda/cap. `--watch --interval 300` roda em loop (e confirma o Codex ao vivo quando um reset cruza, veja abaixo). |
| `meter-report` | Histórico das leituras do medidor. |
| `codex-meter` | Uma leitura do rate-limit do **Codex** a partir dos rollouts `~/.codex/sessions` (custo zero); grava na tabela `codex_meter` e alerta reset/queda/cap. `--watch --interval 300` roda em loop (e confirma ao vivo quando um reset cruza, veja abaixo). |
| `codex-meter-report` | Histórico das leituras do medidor do Codex. |
| `status` | Resumo num olhar: Claude + Codex (5h/semanal) + veredito do `gate --provider both`. |
| **`gate`** | **Veredito GO/PAUSE de rate limit** para runners; `--provider {claude,codex,both}` (default `claude`) — ver abaixo. |
| `calibrate` | Aprende o fator de custo real por modelo a partir de gastos reais de crédito (`--brl`/`--usd`, `--solve`, `--apply`). |
| `bursts` | Detalha clusters de atividade (gatilho manual × wakeup × task, billing, modelos, cap). |
| `dashboard` | Gera um **dashboard HTML autocontido** (CSS+SVG inline, custo zero — só lê o SQLite): status Claude+Codex com veredito do gate, custo/dia (14d, ~USD calibrado), custo por modelo (7d), sparkline do medidor 5h (24h), billing e eventos recentes. `--open` abre no navegador; `--out`/`DASHBOARD_PATH` mudam o destino (default `~/.claude/tools/dashboard.html`). |

> **Billing assinatura × crédito:** o monitor marca como `credits` o uso real produzido **depois** do medidor 5h ser confirmado em 100%, e como `subscription` o resto. Os relatórios `--by billing` e os custos `~USD` (calibrados via `calibrate`) saem desse cruzamento.

> **Fuso nos relatórios:** o eixo `--by day` e o `--since YYYY-MM-DD` (em `report` e `codex-report`) usam o **fuso local** (`METER_TZ`), não UTC — a atividade da madrugada local fica no dia certo. Um `--since` com datetime ISO completo (com tz) é respeitado como dado.

### Variáveis de ambiente do monitor

| Variável | Padrão | Descrição |
|---|---|---|
| `METER_MODEL` | `haiku` | Modelo usado nas chamadas `/usage` do monitor (a var `MODEL` é só dos watchers shell). |
| `METER_TZ` | `America/Sao_Paulo` | Fuso do medidor: âncora dos horários de reset sem fuso na linha e dos eixos `--by day`/`--since`. |
| `RESET_TOLERANCE` | `600` | Segundos de variação no horário de reset que não contam como nova janela. |
| `DROP_THRESHOLD` | `5` | Queda mínima de % para alertar "liberou". |
| `FETCH_RETRIES` | `3` | Tentativas de ler o `/usage` por ciclo. |
| `METER_RECONFIRM` | `1` | Releitura de confirmação numa leitura degradada (`0` desliga). |
| `RECONFIRM_COOLDOWN` | `600` | Segundos mínimos entre releituras de confirmação (limita o custo se o `/usage` oscilar). |
| `METER_WARMUP` | `1` | Warm-up (prompt mínimo, **gasta cota**) quando o reconfirm não recupera o horário (`0` desliga). |
| `METER_INFER_RESET` | `1` | Estimativa `≈` do próximo reset quando o horário não veio (`0` deixa desconhecido). |
| `CREDIT_PCT` | `100` | Limiar do medidor 5h a partir do qual a janela conta como capada (cap/crédito). |
| `WATCHLOG_PATH` | `~/.claude/limit-watch/watch.log` | Watch.log importado pelo `ingest` (aponte para o `STATE_DIR` customizado do watcher, se houver). |
| `FACTORS_PATH` | `~/.claude/tools/pricing_factors.json` | Onde `calibrate --apply` grava os fatores de custo por modelo. |
| `CODEX_SESSIONS_DIR` | `~/.codex/sessions` | Diretório de rollouts do Codex. |
| `DASHBOARD_PATH` | `~/.claude/tools/dashboard.html` | Saída padrão do subcomando `dashboard`. |

### Gate de rate limit (para runners)

O `gate` é o ponto de decisão único "**posso seguir trabalhando?**". Lê a última leitura do medidor do banco (custo zero) e só refaz uma leitura ao vivo se a anterior estiver velha (`--max-age`, padrão 300s). Aplica os tetos de 5h e semanal **juntos** e já entrega tudo mastigado:

```bash
~/.claude/tools/token_monitor.py gate            # texto: DECISION + conselho + bloco de pausa pronto
~/.claude/tools/token_monitor.py gate --json     # mesmos campos (advice, pause_header, ...) p/ automação
```

Saída em `PAUSE`:

```text
📊 5h: 95% · reset Jun 11 at 4:39pm (America/Sao_Paulo)  |  semanal: 80% · reset ...  (cache 199s)
motivo: claude 5h em 95% (>= 80%)
DECISION: PAUSE
🛑 Pare. Não inicie novas tarefas. Faça commit do que está validado e responda no formato de pausa abaixo...
─────── cole e complete ───────
pausado: <N> tarefas restantes
motivo: claude 5h em 95% (>= 80%)
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

A detecção compara cada rollout com o último snapshot cronológico já gravado: **reset** ocorre quando o horário (`resets_at`, um instante absoluto estável na janela) avança além de `RESET_TOLERANCE` — mesmo que o percentual não mude (janela re-saturada 100%→100% no reset); a idempotência vem de comparar sempre com o snapshot de mesmo `ts_epoch`, não de exigir mudança de %. **drop** exige queda acima de `DROP_THRESHOLD`, e **cap** ocorre quando a janela de 5h cruza 100%. Relê-lo no `--watch` é idempotente: um rollout estático nunca repete o mesmo alerta em polls posteriores. Cada evento dispara desktop + som + Telegram. O formato das mensagens é idêntico entre Claude e Codex — veja a seção acima para a lógica de ⚠️ em drops fora do horário de reset.

#### Snapshot estático, inferência de reset e confirmação ao vivo

O rollout é um **snapshot da última vez que o Codex rodou** — não se atualiza sozinho (o `/status` da TUI mostra o mesmo dado, só parece "ao vivo" porque a sessão está ativa). Para não ficar preso num valor velho, o medidor faz duas coisas:

- **Inferência de reset (no gate):** como `resets_at` é epoch absoluto, uma janela cujo reset já passou é tratada como recuperada (~0%) mesmo sem o Codex rodar. Sem isso, um `gate --provider codex` ficaria preso no % antigo para sempre e um runner gated em Codex nunca acordaria.
- **Confirmação ao vivo (1 turno mínimo):** gasta **1 turno mínimo** do `codex exec` para obter o número real, mas **só quando um reset já cruzou desde o rollout** (antes disso a leitura ainda é válida, pois sem o Codex rodar o uso não muda). Acontece **automaticamente nos loops `meter --watch` e `codex-meter --watch`** — **uma vez por snapshot** (se o turno falhar não regasta cota a cada poll) — para a leitura sair do valor preso (ex.: semanal em 100%) logo após o reset. Numa leitura avulsa, force com `codex-meter --refresh` (e `--force` gasta sempre, mesmo sem reset cruzado). O turno roda em `~` com `--skip-git-repo-check` (diretório não-trusted) e stdin fechado.

#### Uso de tokens (`codex-report`)

O `ingest` também varre os rollouts e grava o uso de tokens por sessão na tabela `codex_usage` (total cumulativo do último `token_count` + janela de tempo 1º→último evento + modelo). `codex-report` agrega isso por sessão/dia/modelo. Codex é assinatura, então reporta **tokens e tempo**, não custo por token.

```bash
~/.claude/tools/token_monitor.py ingest                      # popula codex_usage (além de usage/meter)
~/.claude/tools/token_monitor.py codex-report --by session   # tokens + tempo por sessão
~/.claude/tools/token_monitor.py codex-report --by day       # por dia
~/.claude/tools/token_monitor.py codex-report --by model     # por modelo
```

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

Usa o **mesmo caminho de leitura do `gate`** (refaz a leitura ao vivo se a última estiver velha, via `--max-age`/`--refresh`) e a **mesma regra de veredito** — assim `status` e `gate` nunca discordam. Sem nenhuma leitura válida o veredito é `UNKNOWN` (nunca um `GO` falso), e um bucket ausente (ex.: rollout do Codex sem janela semanal) não derruba o comando.

```bash
~/.claude/tools/token_monitor.py status                       # gate 5h<80% · semanal<90% (defaults)
~/.claude/tools/token_monitor.py status --max-5h 70 --max-week 85
~/.claude/tools/token_monitor.py status --refresh --no-notify # força leitura ao vivo, sem notificar
```

```text
=== Status — Claude + Codex (gate 5h<80% · semanal<90%) ===

📊 Claude   5h: 95% · reset Jun 24 at 4:39pm   |   semanal: 80% · reset Jun 30 at 9am  🛑   [cache 199s]
📊 Codex    5h: 42% · reset Jun 24 at 6:10pm   |   semanal: 18% · reset Jun 30 at 9am        [rollout 312s]

🛑 gate(both): PAUSE — estourado: claude
```

Cada linha traz a origem da leitura entre colchetes (`ao vivo` / `cache Ns` no Claude, `rollout Ns` no Codex). Flags: `--max-5h`, `--max-week`, `--max-age`, `--refresh`, `--no-notify` (mesmos defaults do `gate`).

#### Tabela `codex_meter` e env

O medidor do Codex grava na tabela nova `codex_meter` no mesmo SQLite, separada do `meter` do Claude (uma linha por leitura: `ts` UTC do rollout, plano, `session_pct`/`week_pct` + resets, e os eventos). O diretório de rollouts é configurável via `CODEX_SESSIONS_DIR` (default `~/.codex/sessions`); sem rollout recente com `rate_limits`, o medidor só reporta "sem leitura" e segue (não é erro).

---

## Skill: runner contínuo seguro

A skill [`ultracode-continuous-safe-runner`](ultracode-continuous-safe-runner/SKILL.md) orquestra trabalho **contínuo, incremental e retomável**: quebra a demanda em tarefas pequenas, valida e faz **um commit por tarefa**, consulta o `gate` a cada 1–2 tarefas e, em `PAUSE`, salva checkpoint e para antes de bater o limite. Toda a lógica de rate limit é delegada ao `gate` — a skill só executa o comando e segue a saída.
