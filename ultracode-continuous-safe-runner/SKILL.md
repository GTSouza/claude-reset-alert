---
name: ultracode-continuous-safe-runner
description: Executa trabalho contínuo, incremental e retomável, com commit por tarefa e gate oficial de rate limit (5h + semanal) antes de cada lote, pausando com checkpoint antes do teto.
---

# Skill: Ultracode Continuous Safe Runner

Use esta skill quando o usuário pedir para executar tarefas de forma **contínua, incremental, com commits frequentes e sem bater rate limit**.

## Objetivo

Trabalhar em modo contínuo, pausável e retomável, sempre:

- quebrando o trabalho em tarefas pequenas e independentes;
- validando e fazendo commit por tarefa finalizada;
- consultando o **gate oficial** de rate limit entre lotes;
- pausando com checkpoint antes de atingir o teto (5h ou semanal);
- deixando o repositório em estado retomável.

## Gate de rate limit (regra principal)

Antes de iniciar e a cada 1–2 tarefas, rode **um único comando** — ele decide por você:

```bash
~/.claude/tools/token_monitor.py gate
```

O `gate` faz **tudo mastigado**: lê a última leitura de `/usage` do banco (custo zero,
refaz ao vivo só se a anterior estiver velha > 5 min), decide, explica e já imprime o
bloco de pausa pronto. **Você não parseia porcentagem nem calcula limiar** — apenas
executa e segue o output literalmente:

- `DECISION: GO` (exit `0`) → a linha `➡️` confirma: inicie a próxima tarefa.
- `DECISION: PAUSE` (exit `10`) → a linha `🛑` manda parar e o gate **já imprime o
  cabeçalho de pausa entre `─── cole e complete ───`**. Copie esse bloco, preencha `<N>`
  e as listas de concluído/pendente, e responda.
- `DECISION: UNKNOWN` (exit `2`) → sem leitura válida; a linha `⚠️` orienta — trate como
  PAUSE (conservador).

Tetos default: `--max-5h 80` e `--max-week 90` (cobre 5h e semanal juntos). Ajuste só se o
usuário pedir (ex.: `gate --max-5h 70`). Para automação, `gate --json` traz os mesmos
campos prontos, incluindo `advice` e `pause_header`.

Por padrão o gate mede só o provedor primário (`--provider claude`) — decisão, exit codes
(`0`/`10`/`2`) e bloco de pausa idênticos. O `motivo:` vem SEMPRE prefixado pelo provedor
(ex.: `motivo: claude 5h em 95% (>= 80%)`), em qualquer modo — automações que parseiam o
bloco devem esperar o prefixo. Se um lote usar dois modelos (um implementa, outro revisa),
`--provider both` retorna `PAUSE` se qualquer um estourar (o prefixo aponta o responsável);
`--provider codex` mede só o revisor. O contrato (`DECISION:`, exit, `pause_header`,
chaves do `--json`) é o mesmo em qualquer modo.

Nunca use `budget` do Workflow como medidor de consumo: ele não representa o consumo real.

## Fluxo padrão

1. Entrar no diretório de trabalho; conferir estado:

   ```bash
   git status --short
   git branch --show-current
   ```

2. Rodar o gate inicial:

   ```bash
   ~/.claude/tools/token_monitor.py gate
   ```

   Se já vier `PAUSE`, pare antes de começar e informe o reset.

3. Entender a solicitação e montar uma fila de tarefas pequenas e independentes.
4. Para cada tarefa: implementar → revisar diff → validar → corrigir → commit específico.
5. A cada 1–2 tarefas, rodar o gate de novo.
6. Em `PAUSE`, pausar com checkpoint. Em `GO`, seguir.
7. Concluído tudo, retornar o **formato de conclusão**.

## Diagnóstico opcional

Use só quando precisar investigar consumo (não no loop normal):

```bash
~/.claude/tools/token_monitor.py status               # resumo num olhar: ambos os provedores + veredito do gate
~/.claude/tools/token_monitor.py meter                # força uma leitura ao vivo do /usage (mede o Codex junto; --no-codex desliga)
~/.claude/tools/token_monitor.py meter-report         # histórico das leituras
~/.claude/tools/token_monitor.py codex-meter          # força a leitura do medidor do revisor
~/.claude/tools/token_monitor.py codex-meter-report   # histórico do medidor do revisor
~/.claude/tools/token_monitor.py bursts --session <id>
~/.claude/tools/token_monitor.py report --by billing
```

## Política de tarefas

Cada tarefa deve ser pequena, verificável, independente quando possível, segura para
commit isolado e retomável. Não misture refactor, fix, teste e feature no mesmo commit
quando puder separar.

## Política de commits

Um commit por tarefa finalizada e validada. Formato `<tipo>: <descrição curta>`:

```text
fix: handle missing video feed metadata
refactor: simplify feed state synchronization
test: add coverage for incremental parser
docs: document ultracode resume workflow
chore: add checkpoint for continuous runner
```

Antes e depois de cada commit, confira `git diff` e `git status --short`.

## Política de validação

Validações proporcionais à tarefa. Se não souber o stack, inspecione `package.json`,
`pyproject.toml`, `Cargo.toml`, `go.mod`, `Makefile` ou `README.md` e escolha o comando
adequado (`npm test` / `pytest` / `cargo test` / `go test ./...` / lint / typecheck).
Se uma validação falhar por motivo não relacionado à tarefa, registre isso no relatório.

## Checkpoint de retomada

Ao pausar ou antes de mudanças maiores, registre: tarefas concluídas, pendentes, arquivos
modificados, validações executadas, último commit e próximo passo. Use um arquivo se
adequado, ex.: `.ultracode-continuous-checkpoint.md`.

## Formato de pausa obrigatória

Em `PAUSE`, **cole o cabeçalho que o próprio `gate` imprimiu** (bloco `pausado/motivo/
reset/semanal`, ou o campo `pause_header` do `--json`) e complete o resto:

```text
<cabeçalho do gate: pausado/motivo/reset/semanal>

concluído:
- <tarefa concluída>

pendente:
- <tarefa pendente>

validações:
- <comando>: <resultado>

último commit:
- <hash ou mensagem>

checkpoint:
- <arquivo ou descrição do estado salvo>
```

## Auto-retomada no reset (self-resume) — OBRIGATÓRIO ao pausar

Uma sessão interativa **NÃO volta sozinha**: ela só age quando (a) o usuário manda mensagem ou
(b) um agendamento dispara nela. Se a pausa só imprime o cabeçalho e para, a sessão fica **ociosa
até o usuário voltar** — foi exatamente o que aconteceu ("a sessão não resumiu no último pause").

Então, SEMPRE que pausar por `PAUSE` (5h ou semanal), **agende a própria retomada** antes de parar:

1. Pegue o horário de reset do gate (`gate --json` → `session_reset`/`week_reset`; ou o
   `pause_header`). Calcule os minutos até ~2 min depois do reset (se não der pra parsear com
   segurança, use `delay_minutes: 60` e re-decida ao acordar).
2. Agende uma mensagem de retomada **nesta mesma sessão** (preserva o contexto/checkpoint):
   ```
   send_later(
     delay_minutes: <minutos até logo após o reset>,   # ou at: <RFC3339 do reset+2min>
     message: "Auto-resume: rode `python3 ~/.claude/tools/token_monitor.py gate`. Se GO, retome a
               fila pendente do checkpoint (<arquivo>) de onde parou. Se ainda PAUSE, reagende o
               self-resume e pare de novo."
   )
   ```
   Guarde o `trigger_id` retornado no checkpoint.
3. Informe ao usuário no bloco de pausa: "auto-resume agendado p/ ~<hora> (id `<trigger_id>`)".
4. Se o usuário retomar **antes** do disparo, cancele o agendamento pendente
   (`delete_trigger <trigger_id>`) para não haver poke redundante.

Observações:
- `send_later` é um wrapper de `create_trigger` self-bind + one-shot: dispara UMA vez nesta sessão e
  se auto-desativa. Ele aparece em `list_triggers` até disparar (por isso `list_triggers` vazio =
  nada agendado = a sessão nunca ia voltar sozinha).
- Ambiente **headless/nuvem fresca**: uma sessão nova agendada pode não enxergar o estado LOCAL
  git-ignorado. Aí prefira o self-bind (fire na MESMA sessão, que mantém o contexto) e não um
  `create_new_session_on_fire`. Se nem isso for viável no ambiente, diga explicitamente "sem
  self-resume viável aqui; retomada é manual" em vez de fingir que agendou.

## Formato de conclusão

```text
concluído: 0 tarefas restantes

commits:
- <hash> <mensagem>

validações:
- <comando>: <resultado>

observações:
- <limitação, falha pré-existente ou ponto de atenção>
```

## Instrução operacional

Trabalhe continuamente sem pedir confirmação a cada etapa. Só pergunte se houver
ambiguidade bloqueante; caso contrário, tome a melhor decisão segura, documente e siga.

Nunca inicie nova tarefa quando o `gate` retornar `PAUSE` (ou `UNKNOWN`).
Sempre deixe o repositório em estado retomável.
