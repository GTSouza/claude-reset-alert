#!/usr/bin/env python3
"""
token_monitor.py — Monitor de uso de tokens + créditos do Claude Code.

Lê os transcripts (.jsonl) que o Claude Code grava em ~/.claude/projects/,
agrega o uso de tokens e persiste em SQLite para relatórios por janela
(5h, dia, semana, mês), por modelo, por sessão, in/out e por FONTE DE COBRANÇA
(assinatura × créditos). Também consulta o medidor oficial (/usage, custo zero),
detecta cap/início de crédito em tempo real, e calibra o custo (US$) contra
gastos reais de crédito. Sem dependências externas (apenas stdlib).

Uso — tokens & relatórios:
  python3 token_monitor.py ingest            # varre .jsonl (Claude) + rollouts (Codex) + watch.log + billing
  python3 token_monitor.py report            # default: últimos 7 dias (global)
  python3 token_monitor.py report --window 5h
  python3 token_monitor.py report --since 2026-06-01 --by billing   # assinatura × crédito
  python3 token_monitor.py report --by model --model claude-fable-5 --session 86e5a22d
  python3 token_monitor.py report --by model --io-only   # só in/out+%out (comparável ao app do Claude)
  python3 token_monitor.py limits            # episódios de batida de limite
  python3 token_monitor.py bursts --session 86e5a22d  # timeline detalhada (gatilho/billing/cap)
  python3 token_monitor.py watch             # ingest contínuo

Uso — medidor oficial (porte do claude-limit-watch.sh, custo zero) + Codex (rollouts):
  python3 token_monitor.py meter             # 1 leitura de /usage (grava 5h%/semanal%/eventos)
  python3 token_monitor.py meter --watch --interval 300   # loop; alerta cap_5h/credits_started/reset/drop
  python3 token_monitor.py meter-report      # histórico do medidor
  python3 token_monitor.py gate              # veredito GO/PAUSE p/ runners (exit 0/10/2); usa cache, refaz se velho
  python3 token_monitor.py gate --json --max-5h 80 --max-week 90   # decisão estruturada p/ automação
  python3 token_monitor.py meter --no-codex   # só Claude (por padrão mede o Codex junto, se houver rollouts)
  python3 token_monitor.py codex-meter        # 1 leitura do rate-limit do Codex (rollouts ~/.codex/sessions)
  python3 token_monitor.py codex-meter --watch --interval 300   # loop; alerta reset/drop/cap (5h/semanal)
  python3 token_monitor.py codex-meter --refresh   # confirma ao vivo (1 turno mínimo do codex exec) só se um reset cruzou; --force sempre
  python3 token_monitor.py codex-meter-report # histórico do medidor do Codex
  python3 token_monitor.py codex-report       # uso de tokens do Codex por sessão/dia/modelo (tokens + tempo)
  python3 token_monitor.py status             # resumo num olhar: Claude + Codex + veredito do gate(both)
  python3 token_monitor.py gate --provider both   # PAUSE se Claude OU Codex estourar (default --provider claude)

Uso — calibração de custo (aprende fator por modelo dos gastos REAIS):
  python3 token_monitor.py calibrate --brl 47.85          # registra episódio (janela=último crédito)
  python3 token_monitor.py calibrate --solve              # resolve fatores por modelo (simula)
  python3 token_monitor.py calibrate --solve --apply      # grava em pricing_factors.json
  python3 token_monitor.py calibrate --list

Eixos (--by): model | session | project | day | billing | none(global)
Janelas (--window): 5h | day | week | month
--io-only: esconde cache/custo, mostra só in/out + %out (o app do Claude mostra ISSO, não cache);
  difere do app por: (1) app=conta toda/todas as máquinas, ferramenta=.jsonl local; (2) data do
  snapshot do app; (3) o app omite cache. Validação: Sonnet 4.6 bate exato (73.1k in / 6.2M out).
Custo: base PRICING × fator do modelo (calibrado); ~USD. Modelo sem dado real = fator 1.0.
Billing: token real enquanto medidor 5h==100% (após cap confirmado) = crédito; senão assinatura.
Env úteis: METER_TZ, CREDIT_PCT, RESET_TOLERANCE, DROP_THRESHOLD, FACTORS_PATH, CODEX_SESSIONS_DIR.
"""
from __future__ import annotations

import argparse
import collections
import html as _html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


def _load_dotenv(path: Path) -> None:
    """Carrega KEY=VAL do .env para os.environ (sem sobrescrever o que já existe)."""
    if not path.is_file():
        return
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k and k not in os.environ:
            os.environ[k] = v.strip().strip('"').strip("'")


# .env na raiz do próprio script (ex.: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)
_load_dotenv(Path(__file__).resolve().parent / ".env")

PROJECTS_DIR = Path.home() / ".claude" / "projects"
DB_PATH = Path.home() / ".claude" / "tools" / "token_usage.db"

# --- Medidor oficial (/usage), portado de claude-limit-watch.sh ---
METER_MODEL = os.environ.get("METER_MODEL", "haiku")   # /usage roda barato no haiku
METER_TZ = os.environ.get("METER_TZ", "America/Sao_Paulo")
RESET_TOLERANCE = int(os.environ.get("RESET_TOLERANCE", "600"))   # s: "3pm" vs "2:59pm"
DROP_THRESHOLD = int(os.environ.get("DROP_THRESHOLD", "5"))       # % de queda => cota liberou
FETCH_RETRIES = int(os.environ.get("FETCH_RETRIES", "3"))
# Reconfirma ao vivo uma leitura degradada (% sem horário de reset) antes de confiar nela;
# METER_RECONFIRM=0 desliga (não faz a releitura barata do haiku).
METER_RECONFIRM = os.environ.get("METER_RECONFIRM", "1") not in ("0", "false", "no", "")
# Cooldown (s) entre releituras de confirmação: um /usage que OSCILA degradado<->saudável não
# deve gastar 1 haiku por poll. Limita a ~1 releitura por janela (padrão 10min >> intervalo de poll).
RECONFIRM_COOLDOWN = int(os.environ.get("RECONFIRM_COOLDOWN", "600"))
# Fallback: quando um reset chega SEM horário e o reconfirm não recupera, estima o próximo reset
# como now + duração da janela (5h/7d), uma vez, e carrega adiante. METER_INFER_RESET=0 desliga.
METER_INFER_RESET = os.environ.get("METER_INFER_RESET", "1") not in ("0", "false", "no", "")
# Warm-up: se o reconfirm re-lê e o /usage segue sem horário, gera um tiquinho de uso (haiku)
# p/ tirar a janela de 0% e relê — tenta o horário REAL antes de cair na estimativa. Gasta um
# pouco de cota/centavos; METER_WARMUP=0 desliga (fica só reconfirm + estimativa).
METER_WARMUP = os.environ.get("METER_WARMUP", "1") not in ("0", "false", "no", "")
# telegram.env do próprio limit-watch (mesmo arquivo, reaproveitado)
TG_ENV_FILE = Path(os.environ.get("TG_ENV_FILE", str(Path.home() / ".claude" / "limit-watch" / "telegram.env")))

# Preço-BASE nominal por 1M tokens (USD): input / output / cache_read / cache_write(5m).
# Estes são os valores de referência; o custo final = base × FATOR_DO_MODELO, onde o
# fator é APRENDIDO dos seus gastos reais de crédito (comando `calibrate`). Modelo sem
# dado real fica com fator 1.0 (nominal) — ver `calibrate --solve`.
PRICING = {
    # Flagship (fable-tier) — mythos-5 (Project Glasswing) tem preço idêntico ao fable-5.
    "claude-fable-5":         {"in": 10.0, "out": 50.0, "cache_read": 1.00, "cache_write": 12.50},
    "claude-mythos-5":        {"in": 10.0, "out": 50.0, "cache_read": 1.00, "cache_write": 12.50},
    # Opus-tier
    "claude-opus-4-8":        {"in": 5.0,  "out": 25.0, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-7":        {"in": 5.0,  "out": 25.0, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-6":        {"in": 5.0,  "out": 25.0, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-5":        {"in": 5.0,  "out": 25.0, "cache_read": 0.50, "cache_write": 6.25},
    # Sonnet-tier — sonnet-5 tem preço promo 2/10 até 2026-08-31 (NÃO modelado aqui;
    # base fica no padrão 3/15, então sonnet-5 é super-estimado ~33% até lá).
    "claude-sonnet-5":        {"in": 3.0,  "out": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "claude-sonnet-4-6":      {"in": 3.0,  "out": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "claude-sonnet-4-5":      {"in": 3.0,  "out": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    # Haiku-tier
    "claude-haiku-4-5":       {"in": 1.0,  "out": 5.0,  "cache_read": 0.10, "cache_write": 1.25},
    # Fallback p/ modelo real não listado: tier Sonnet. É PROPOSITALMENTE conservador
    # (não opus-tier), mas base_cost avisa 1x por ID p/ o gap não passar em silêncio.
    "_default":               {"in": 3.0,  "out": 15.0, "cache_read": 0.30, "cache_write": 3.75},
}

# IDs 'claude-*' já avisados (uma vez cada) por caírem no _default. Evita subnotificar
# em silêncio o custo de um flagship novo (ex.: um sucessor do fable no tier Sonnet = -70%).
_UNPRICED_WARNED: set[str] = set()

# Textos de reset do /usage já avisados (uma vez cada) por não serem interpretáveis pelo
# _reset_to_epoch. Um formato novo que não parseia desliga os alertas de reset/cap em
# silêncio (o núcleo da ferramenta) — o aviso torna a deriva de formato visível.
_RESET_UNPARSED_WARNED: set[str] = set()


def _norm_model(model: str | None) -> str:
    """Normaliza variantes ao modelo base p/ compartilhar preço/fator: o sufixo '[1m]'
    e o sufixo DATADO '-YYYYMMDD' que o Claude Code grava de verdade nos transcripts
    (ex.: 'claude-haiku-4-5-20251001' -> 'claude-haiku-4-5'). Sem isso o ID datado não
    casa PRICING e cairia no _default (~3× o preço do haiku)."""
    return re.sub(r"-\d{8}$", "", (model or "_default").split("[")[0])

# Fatores por modelo (base × fator = custo real), persistidos em JSON e aprendidos
# via `calibrate`. Carregados uma vez; default 1.0 para modelo sem calibração.
FACTORS_PATH = Path(os.environ.get("FACTORS_PATH", str(Path.home() / ".claude" / "tools" / "pricing_factors.json")))


def _load_factors() -> dict:
    try:
        return json.loads(FACTORS_PATH.read_text())
    except Exception:
        return {}


FACTORS = _load_factors()

# Apenas a faixa real do Claude Code, ancorada para evitar falsos positivos
# (ex.: mensagens que apenas *discutem* limites). Exige o "resets ...".
LIMIT_PATTERNS = re.compile(
    r"hit your (session|usage|\w+) limit\b.*\bresets\b",
    re.IGNORECASE,
)

# Predicado único de "modelo real" (exclui synthetic/<local>) — versões SQL e Python
# lado a lado para não divergirem. Gate de billing, calibração e bursts.
REAL_MODEL_SQL = "model LIKE 'claude%'"


def is_real_model(model: str | None) -> bool:
    return bool(model) and model.startswith("claude")


# ----------------------------- DB ------------------------------------------ #
def db_connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    # Concorrência: WAL deixa leitor(es) + 1 escritor sem dar "readonly/locked";
    # busy_timeout faz aguardar o lock em vez de falhar na hora.
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
    con.execute("""
        CREATE TABLE IF NOT EXISTS usage (
            uuid TEXT PRIMARY KEY,
            ts TEXT,                 -- ISO UTC
            ts_epoch REAL,
            session_id TEXT,
            project TEXT,
            git_branch TEXT,
            model TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_read INTEGER,
            cache_write INTEGER,
            web_search INTEGER,
            web_fetch INTEGER,
            service_tier TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS limits (
            uuid TEXT PRIMARY KEY,
            ts TEXT,
            ts_epoch REAL,
            session_id TEXT,
            project TEXT,
            model TEXT,
            message TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS meter (
            ts TEXT PRIMARY KEY,         -- ISO UTC da leitura
            ts_epoch REAL,
            session_pct INTEGER,         -- janela de 5h (% usado)
            session_reset TEXT,          -- texto cru "Jun 10 at 3pm (tz)"
            session_reset_epoch REAL,
            week_pct INTEGER,            -- janela semanal
            week_reset TEXT,
            week_reset_epoch REAL,
            event TEXT                   -- 'reset_5h' | 'reset_week' | 'drop_5h' | 'drop_week' | NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS codex_meter (
            ts TEXT PRIMARY KEY,         -- ISO UTC do snapshot (ts do rollout do Codex)
            ts_epoch REAL,
            session_pct REAL,            -- janela 5h (primary, 300min) % usado
            session_reset TEXT,
            session_reset_epoch REAL,
            week_pct REAL,               -- janela semanal (secondary, 10080min)
            week_reset TEXT,
            week_reset_epoch REAL,
            plan TEXT,                   -- plus | pro | ...
            event TEXT                   -- 'reset_5h' | 'reset_week' | 'drop_5h' | 'drop_week' | 'cap_5h' | NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS codex_usage (
            rollout TEXT PRIMARY KEY,        -- caminho do rollout (1 por sessão Codex)
            session_id TEXT,                 -- uuid da sessão (do nome do arquivo)
            started_epoch REAL,              -- 1º timestamp do rollout
            ended_epoch REAL,                -- último timestamp (started->ended = tempo ativo)
            model TEXT,
            input_tokens INTEGER,
            cached_input_tokens INTEGER,
            output_tokens INTEGER,
            reasoning_output_tokens INTEGER,
            total_tokens INTEGER,            -- cumulativo (último token_count.total_token_usage)
            updated_epoch REAL               -- mtime do arquivo na última ingestão (idempotência)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS calibration (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,                 -- quando foi registrado
            note TEXT,
            real_usd REAL,           -- gasto real em USD (já convertido)
            win_from TEXT, win_to TEXT,
            tokens_json TEXT         -- {model: {in,out,cr,cw}} do episódio
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS ingest_state (
            path TEXT PRIMARY KEY,   -- .jsonl ou watch.log
            mtime REAL,
            size INTEGER,
            offset INTEGER           -- byte após a última linha completa lida
        )
    """)
    # Migração: coluna billing_source (subscription | credits) sem recriar o banco.
    cols = {r[1] for r in con.execute("PRAGMA table_info(usage)")}
    if "billing_source" not in cols:
        con.execute("ALTER TABLE usage ADD COLUMN billing_source TEXT DEFAULT 'subscription'")
    con.execute("CREATE INDEX IF NOT EXISTS idx_usage_epoch ON usage(ts_epoch)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_limits_epoch ON limits(ts_epoch)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_meter_epoch ON meter(ts_epoch)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_codex_meter_epoch ON codex_meter(ts_epoch)")
    con.commit()
    return con


def parse_ts(ts: str) -> float:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


# --------------------------- Ingest ---------------------------------------- #
def ingest(con: sqlite3.Connection, verbose: bool = True) -> tuple[int, int]:
    new_usage = new_limits = bad_ts = 0
    for jf in PROJECTS_DIR.rglob("*.jsonl"):
        project = jf.parent.name
        try:
            file_mtime = jf.stat().st_mtime
        except OSError:
            file_mtime = 0.0
        for line in _iter_new_lines(con, jf):
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("type") != "assistant":
                continue
            uuid = o.get("uuid")
            msg = o.get("message") or {}
            if not uuid or not isinstance(msg, dict):
                continue
            ts = o.get("timestamp", "")
            ts_epoch = parse_ts(ts)
            # timestamp ausente/ilegível => parse_ts devolve 0.0, e um ts_epoch=0 fica
            # INVISÍVEL em todo relatório com janela (ts_epoch >= since) e no billing.
            # Cai no mtime do arquivo (aproximação razoável) para a linha não sumir; conta
            # p/ avisar. A PK (uuid) mantém a releitura idempotente.
            if ts_epoch == 0.0 and file_mtime:
                ts_epoch = file_mtime
                bad_ts += 1
            model = msg.get("model")
            session_id = o.get("sessionId")

            # --- limit events ---
            text = _join_text(msg.get("content"))
            if text and LIMIT_PATTERNS.search(text):
                cur = con.execute(
                    "INSERT OR IGNORE INTO limits VALUES (?,?,?,?,?,?,?)",
                    (uuid, ts, ts_epoch, session_id, project, model, text[:300]),
                )
                new_limits += max(cur.rowcount, 0)

            # --- usage ---
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            sv = usage.get("server_tool_use") or {}
            t = _usage_tokens(usage)
            row = (
                uuid, ts, ts_epoch, session_id, project, o.get("gitBranch"),
                model, t["in"], t["out"], t["cr"], t["cw"],
                int(sv.get("web_search_requests", 0) or 0),
                int(sv.get("web_fetch_requests", 0) or 0),
                usage.get("service_tier"),
            )
            cur = con.execute(
                "INSERT OR IGNORE INTO usage "
                "(uuid, ts, ts_epoch, session_id, project, git_branch, model, "
                "input_tokens, output_tokens, cache_read, cache_write, "
                "web_search, web_fetch, service_tier) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row
            )
            new_usage += max(cur.rowcount, 0)
    con.commit()
    n_log = ingest_watchlog(con)
    n_credits = compute_billing(con)
    n_codex = ingest_codex(con)
    if verbose:
        extra = f" | +{n_log} leituras do watch.log" if n_log else ""
        extra += f" | +{n_codex} sessões Codex" if n_codex else ""
        extra += f" | ⚠️ {bad_ts} sem timestamp (usei o mtime do arquivo)" if bad_ts else ""
        print(f"ingest: +{new_usage} mensagens de uso, +{new_limits} batidas de limite "
              f"| {n_credits} msgs ≈créditos{extra}")
    return new_usage, new_limits


# Linha do watch.log: "YYYY-MM-DD HH:MM:SS  📊 5h: N% usado · reset R1  |  semanal: M% usado · reset R2"
# reset (.*?) e não (.+?): quando o /usage não traz o "resets ..." o watcher loga
# "... reset  | ..." — os PERCENTUAIS ainda são válidos e não devem ser descartados.
# \ufeff? tolera o BOM UTF-8 que o watcher do PowerShell (Add-Content -Encoding utf8)
# põe no início do watch.log — senão a 1ª linha do arquivo nunca casaria o regex.
_WATCHLOG_RE = re.compile(
    r"^\ufeff?(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+📊 5h:\s*(\d+)% usado · reset\s*(.*?)\s*\|\s+"
    r"semanal:\s*(\d+)% usado · reset\s*(.*?)\s*$"
)
WATCHLOG_PATH = Path(os.environ.get("WATCHLOG_PATH", str(Path.home() / ".claude" / "limit-watch" / "watch.log")))


def ingest_watchlog(con: sqlite3.Connection) -> int:
    """Importa o histórico do claude-limit-watch.sh (watch.log) para a tabela meter.

    O timestamp do log é hora LOCAL (METER_TZ); convertemos para UTC. Idempotente
    via PRIMARY KEY (ts) e incremental via ingest_state (só lê linhas novas).
    Convive com as leituras feitas pelo subcomando `meter`.
    """
    if ZoneInfo is None:
        return 0
    tz = ZoneInfo(METER_TZ)
    n = 0
    for line in _iter_new_lines(con, WATCHLOG_PATH):
        m = _WATCHLOG_RE.match(line.strip())
        if not m:
            continue
        local_ts, s_pct, s_reset, w_pct, w_reset = m.groups()
        try:
            dt = datetime.strptime(local_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz).astimezone(timezone.utc)
        except Exception:
            continue
        cur = con.execute(
            "INSERT OR IGNORE INTO meter VALUES (?,?,?,?,?,?,?,?,?)",
            (dt.isoformat(), dt.timestamp(), int(s_pct), s_reset, _reset_to_epoch(s_reset),
             int(w_pct), w_reset, _reset_to_epoch(w_reset), None),
        )
        n += max(cur.rowcount, 0)
    con.commit()
    return n


# Limiar de % do medidor 5h a partir do qual consideramos a janela CAPADA.
CREDIT_PCT = int(os.environ.get("CREDIT_PCT", "100"))


def compute_billing(con: sqlite3.Connection) -> int:
    """Marca usage.billing_source via MEDIDOR OFICIAL (tabela meter), não por banners.

    Regra: os intervalos de `credit_episodes()` (medidor 5h confirmado a 100% com
    tokens reais produzidos depois) viram 'credits'; todo o resto, 'subscription'.
    Os UPDATEs só tocam linhas cujo valor muda, em vez de reescrever a tabela toda.
    """
    con.execute(
        "UPDATE usage SET billing_source='subscription' "
        "WHERE billing_source IS NOT 'subscription'"
    )
    intervals = credit_episodes(con)
    for a, b in intervals:
        con.execute(
            "UPDATE usage SET billing_source='credits' "
            "WHERE ts_epoch > ? AND ts_epoch <= ? AND billing_source IS NOT 'credits'",
            (a, b),
        )
    n = con.execute("SELECT COUNT(*) FROM usage WHERE billing_source='credits'").fetchone()[0]
    con.commit()
    return n


def _real_output_tokens(con: sqlite3.Connection, a: float, b: float) -> int:
    """Σ output_tokens de modelos reais no intervalo (a, b]."""
    return con.execute(
        f"SELECT COALESCE(SUM(output_tokens), 0) FROM usage "
        f"WHERE ts_epoch > ? AND ts_epoch <= ? AND {REAL_MODEL_SQL}",
        (a, b),
    ).fetchone()[0]


def credit_episodes(con: sqlite3.Connection) -> list[tuple[float, float]]:
    """Detecta EPISÓDIOS de uso de crédito — mesma lógica do evento `credits_started`.

    Um episódio existe quando, após o medidor 5h ser CONFIRMADO em 100% (1ª leitura
    >= CREDIT_PCT depois de uma leitura <100%), há tokens reais produzidos. O intervalo
    de crédito vai do 100%-confirmado até a próxima leitura <100% (fim do cap).
    Isso ignora o trabalho que só CONSUMIU a cota (antes do 100% confirmado) e o ruído
    de borda em janelas que nunca passaram do cap (J1/J2/J3).
    """
    meters = con.execute(
        "SELECT ts_epoch, session_pct FROM meter WHERE session_pct IS NOT NULL ORDER BY ts_epoch"
    ).fetchall()
    if not meters:
        return []
    now = datetime.now(timezone.utc).timestamp()
    episodes = []
    i, n = 0, len(meters)
    while i < n:
        if meters[i][1] >= CREDIT_PCT:
            cap_start = meters[i][0]                       # 1ª leitura 100% da sequência
            j = i
            while j < n and meters[j][1] >= CREDIT_PCT:    # fim do streak de 100%
                j += 1
            last_100 = meters[j - 1][0]                    # última leitura 100% confirmada
            # O reset real cai no gap de poll entre a última 100% e a 1ª <100%. Estimamos
            # no PONTO MÉDIO (esperança do instante do reset): truncar em last_100
            # subnotifica até 1 intervalo de poll de crédito, e ir até a 1ª <100% pescaria
            # trabalho pós-reset (assinatura). O ponto médio é simétrico e — crucial —
            # dá intervalo NÃO-degenerado mesmo num streak de 1 leitura (senão (x, x]
            # seria vazio e zeraria o crédito). Ainda capado (sem <100% depois) =>
            # credita até agora.
            cap_end = (last_100 + meters[j][0]) / 2.0 if j < n else now
            # só é crédito se houve token real ESTRITAMENTE após o 100% confirmado
            if cap_end > cap_start and _real_output_tokens(con, cap_start, cap_end) > 0:
                episodes.append((cap_start, cap_end))
            i = j
        else:
            i += 1
    return episodes


def _iter_lines(path: Path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            yield from fh
    except OSError:
        return


def _iter_new_lines(con: sqlite3.Connection, path: Path):
    """Itera só as linhas NOVAS de um arquivo append-only (offset em ingest_state).

    Lê em binário para rastrear o offset por bytes; linha parcial (sem '\\n', ainda
    em escrita) fica para a próxima rodada. Arquivo truncado recomeça do zero —
    os INSERT OR IGNORE downstream mantêm a releitura idempotente.
    """
    try:
        st = path.stat()
    except OSError:
        return
    key = str(path)
    prev = con.execute(
        "SELECT mtime, size, offset FROM ingest_state WHERE path = ?", (key,)
    ).fetchone()
    if prev and prev[0] == st.st_mtime and prev[1] == st.st_size:
        return
    offset = prev[2] if prev and prev[2] <= st.st_size else 0
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            for raw in fh:
                if not raw.endswith(b"\n"):
                    break
                offset += len(raw)
                yield raw.decode("utf-8", errors="replace")
    except OSError:
        return
    con.execute(
        "INSERT OR REPLACE INTO ingest_state VALUES (?,?,?,?)",
        (key, st.st_mtime, st.st_size, offset),
    )


def _usage_tokens(usage: dict) -> dict:
    """Extrai os 4 contadores de token de um bloco `usage`, null-safe."""
    return {
        "in": int(usage.get("input_tokens", 0) or 0),
        "out": int(usage.get("output_tokens", 0) or 0),
        "cr": int(usage.get("cache_read_input_tokens", 0) or 0),
        "cw": int(usage.get("cache_creation_input_tokens", 0) or 0),
    }


def _join_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
        return " ".join(parts)
    return ""


# --------------------------- Report ---------------------------------------- #
WINDOWS = {
    "5h": timedelta(hours=5),
    "day": timedelta(days=1),
    "week": timedelta(days=7),
    "month": timedelta(days=30),
}


def _meter_tz():
    return ZoneInfo(METER_TZ) if ZoneInfo else timezone.utc


def _tz_offset_seconds() -> int:
    """Offset ATUAL do METER_TZ vs UTC, em segundos (ex.: America/Sao_Paulo -> -10800).
    Rotula o eixo 'day' no fuso local. Exato p/ zonas sem DST (Sao_Paulo); numa zona com
    DST usa o offset de agora p/ toda a janela (pode errar 1h nas viradas — aceitável)."""
    try:
        return int(datetime.now(_meter_tz()).utcoffset().total_seconds())
    except Exception:
        return 0


def _since_epoch_local(s: str) -> float:
    """'YYYY-MM-DD' (ou ISO) -> epoch. Data sem hora vira meia-noite no METER_TZ (local),
    consistente com o resto do relatório (que renderiza em local); um datetime ISO já com
    tz é respeitado. Antes '--since' assumia UTC e deslocava a janela pelo offset local."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_meter_tz())
    return dt.timestamp()


def base_cost(model: str | None, r: dict) -> float:
    """Custo NOMINAL (sem o fator de calibração)."""
    key = _norm_model(model)
    p = PRICING.get(key)
    if p is None:
        # Modelo real sem preço: usa _default mas AVISA (uma vez por ID). Sem isto, um
        # flagship novo caindo no tier Sonnet subnotificaria o custo em até ~70% sem sinal.
        if is_real_model(model) and key not in _UNPRICED_WARNED:
            _UNPRICED_WARNED.add(key)
            print(f"⚠️  modelo sem preço em PRICING: {key} — usando _default (custo pode "
                  f"estar subnotificado). Adicione-o a PRICING.", file=sys.stderr)
        p = PRICING["_default"]
    return (
        r["in"] / 1e6 * p["in"]
        + r["out"] / 1e6 * p["out"]
        + r["cread"] / 1e6 * p["cache_read"]
        + r["cwrite"] / 1e6 * p["cache_write"]
    )


def cost(model: str | None, r: dict) -> float:
    """Custo calibrado = base × fator do modelo NORMALIZADO (1.0 se não calibrado).
    O fator é gravado por _norm_model, então variantes '[1m]'/datadas do mesmo modelo
    compartilham um único fator — consistente com base_cost e com calibrate --solve."""
    return base_cost(model, r) * FACTORS.get(_norm_model(model), 1.0)


def _cost_row(t: dict) -> dict:
    """Adapta {in,out,cr,cw} (transcripts/episódios) para o shape de base_cost()."""
    return {"in": t.get("in", 0), "out": t.get("out", 0),
            "cread": t.get("cr", 0), "cwrite": t.get("cw", 0)}


def fmt(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _trunc(s: str, width: int) -> str:
    """Encurta preservando início E fim ('abc…xyz') com '…' no meio quando estoura a
    largura — assim dois session_ids/projetos que só diferem no fim não viram a MESMA
    linha na tabela (o corte simples '[:34]' fundia rótulos visualmente idênticos)."""
    if len(s) <= width:
        return s
    if width <= 1:
        return s[:width]
    keep = width - 1
    head = (keep + 1) // 2
    tail = keep - head
    return s[:head] + "…" + (s[-tail:] if tail else "")


def report(con: sqlite3.Connection, args) -> None:
    now = datetime.now(timezone.utc)
    if args.since:
        since_epoch = _since_epoch_local(args.since)
        label = f"desde {args.since}"
    else:
        delta = WINDOWS.get(args.window, WINDOWS["week"])
        since_epoch = (now - delta).timestamp()
        label = f"últimos {args.window}"

    # O eixo 'day' agrupa/rotula no fuso local (METER_TZ), não em UTC — senão a atividade
    # da madrugada local cai no dia seguinte. O offset entra como parâmetro do SELECT.
    group_params: list = []
    if args.by == "day":
        group = "substr(datetime(ts_epoch + ?, 'unixepoch'), 1, 10)"
        group_params = [_tz_offset_seconds()]
    else:
        group = {
            "model": "model",
            "session": "session_id",
            "project": "project",
            "billing": "billing_source",
            "none": "'GLOBAL'",
        }.get(args.by, "'GLOBAL'")

    # Filtros opcionais (--model prefixo do id, --session prefixo, --project substring).
    # --model casa por PREFIXO: o Claude Code grava variantes '[1m]' e datadas ('-YYYYMMDD'),
    # então '--model claude-haiku-4-5' precisa pegar 'claude-haiku-4-5-20251001' (senão dá 0).
    where = ["ts_epoch >= ?"]
    params: list = [since_epoch]
    # Batidas de limite são da CONTA (o banner de cap não carrega o modelo filtrado de
    # forma confiável), então a contagem ignora --model — mantém só janela/sessão/projeto.
    lim_where = ["ts_epoch >= ?"]
    lim_params: list = [since_epoch]
    for name, val, clause, param in (
        ("model", args.model, "model LIKE ?", f"{args.model}%"),
        ("session", args.session, "session_id LIKE ?", f"{args.session}%"),
        ("project", args.project, "project LIKE ?", f"%{args.project}%"),
    ):
        if val:
            where.append(clause); params.append(param)
            label += f" [{name}={val}]"
            if name != "model":
                lim_where.append(clause); lim_params.append(param)
    where_sql = " AND ".join(where)
    lim_where_sql = " AND ".join(lim_where)

    # SEMPRE agrupamos por (grupo, model) no SQL para que o custo use o preço
    # certo de cada modelo. As sub-linhas são somadas por grupo de exibição no
    # Python — senão o ~USD do grupo aplicaria o preço de um modelo arbitrário.
    sql = f"""
        SELECT {group} AS g, model,
               SUM(input_tokens), SUM(output_tokens),
               SUM(cache_read), SUM(cache_write),
               SUM(web_search), SUM(web_fetch),
               COUNT(*)
        FROM usage WHERE {where_sql}
        GROUP BY g, model
    """
    subrows = con.execute(sql, group_params + params).fetchall()

    print(f"\n=== Uso de tokens — {label} — por {args.by} ===\n")
    if not subrows:
        print("(sem dados nesta janela)\n")
        return

    # Agrega sub-linhas (grupo × modelo) em grupos de exibição, somando o custo
    # por modelo. agg[grupo] = {in,out,cread,cwrite,msgs,usd}
    agg: dict[str, dict] = {}
    for g, model, i, o, cr, cw, ws, wf, n in subrows:
        # chave de agregação COMPLETA: truncar aqui fundiria session_ids (UUID 36c) ou
        # projetos (caminho longo com prefixo comum) que partilham os 1ºs 32 chars,
        # somando grupos distintos. O truncamento p/ caber na coluna é só na exibição.
        key = str(g) if g else "?"
        d = agg.setdefault(key, {"in": 0, "out": 0, "cread": 0, "cwrite": 0, "msgs": 0, "usd": 0.0})
        d["usd"] += cost(model, {"in": i, "out": o, "cread": cr, "cwrite": cw})
        d["in"] += i; d["out"] += o; d["cread"] += cr; d["cwrite"] += cw; d["msgs"] += n

    if args.io_only:
        # Modo comparável ao app do Claude (aba Models): só in/out + % de output,
        # SEM cache e SEM custo. Ordena por output desc.
        ordered = sorted(agg.items(), key=lambda kv: kv[1]["out"], reverse=True)
        tot_out = sum(d["out"] for _, d in ordered) or 1
        header = f"{'grupo':<34} {'in':>12} {'out':>12} {'% out':>7} {'msgs':>6}"
        print("(modo --io-only: só in/out, sem cache/custo — comparável ao app do Claude)\n")
        print(header)
        print("-" * len(header))
        tot = {"in": 0, "out": 0, "msgs": 0}
        for g_disp, d in ordered:
            print(f"{_trunc(g_disp, 34):<34} {fmt(d['in']):>12} {fmt(d['out']):>12} {d['out'] / tot_out * 100:>6.1f}% {d['msgs']:>6}")
            for k in tot:
                tot[k] += d[k]
        print("-" * len(header))
        # % do TOTAL derivado dos dados (100.0% quando há output; 0.0% quando não há),
        # em vez de fixar '100.0%' — que enganava numa janela sem nenhum output.
        print(f"{'TOTAL':<34} {fmt(tot['in']):>12} {fmt(tot['out']):>12} {tot['out'] / tot_out * 100:>6.1f}% {tot['msgs']:>6}")
        print(f"\nTotal in+out: {fmt(tot['in'] + tot['out'])}  (o app mostra estes números, não o cache)\n")
        return

    ordered = sorted(agg.items(), key=lambda kv: kv[1]["in"] + kv[1]["out"] + kv[1]["cread"] + kv[1]["cwrite"], reverse=True)

    header = f"{'grupo':<34} {'in':>12} {'out':>12} {'cache_r':>13} {'cache_w':>12} {'msgs':>6} {'~USD':>9}"
    print(header)
    print("-" * len(header))
    tot = {"in": 0, "out": 0, "cread": 0, "cwrite": 0, "msgs": 0, "usd": 0.0}
    for g_disp, d in ordered:
        print(f"{_trunc(g_disp, 34):<34} {fmt(d['in']):>12} {fmt(d['out']):>12} {fmt(d['cread']):>13} {fmt(d['cwrite']):>12} {d['msgs']:>6} {d['usd']:>9.2f}")
        for k in tot:
            tot[k] += d[k]
    print("-" * len(header))
    print(f"{'TOTAL':<34} {fmt(tot['in']):>12} {fmt(tot['out']):>12} "
          f"{fmt(tot['cread']):>13} {fmt(tot['cwrite']):>12} {tot['msgs']:>6} {tot['usd']:>9.2f}")
    grand = tot["in"] + tot["out"] + tot["cread"] + tot["cwrite"]
    print(f"\nTotal de tokens (todos os tipos): {fmt(grand)}")
    nlim = con.execute(f"SELECT COUNT(*) FROM limits WHERE {lim_where_sql}", lim_params).fetchone()[0]
    note = " (da conta, ignora --model)" if args.model else ""
    print(f"Batidas de limite nesta janela{note}: {nlim}\n")


def limits(con: sqlite3.Connection, args) -> None:
    # Colapsa duplicatas: agrupa por (bucket de 10min, mensagem) => episódios.
    rows = con.execute(
        """
        SELECT MIN(ts) AS first_ts, MAX(ts) AS last_ts,
               COUNT(*) AS hits, message,
               GROUP_CONCAT(DISTINCT project)
        FROM limits
        GROUP BY CAST(ts_epoch / 600 AS INT), message
        ORDER BY first_ts DESC LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    print(f"\n=== Episódios de limite ({len(rows)}) ===\n")
    if not rows:
        print("(nenhum registrado)\n")
        return
    for first_ts, last_ts, hits, msg, projects in rows:
        print(f"{first_ts}  ({hits}x)  {msg.strip()[:120]}")
        print(f"    projetos: {(projects or '')[:120]}")
    print()


# ------------------------- Meter (/usage) ---------------------------------- #
# Porte de claude-limit-watch.sh: busca o medidor OFICIAL via `claude -p /usage`
# (custo zero, não consome cota), grava na tabela `meter` e detecta reset/queda.

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def _load_tg() -> tuple[str, str]:
    """Lê TG_TOKEN/TG_CHAT_ID do telegram.env (ou do ambiente)."""
    env = {}
    if TG_ENV_FILE.is_file():
        for line in TG_ENV_FILE.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    tok = os.environ.get("TG_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or env.get("TG_TOKEN") or env.get("TELEGRAM_BOT_TOKEN") or ""
    chat = os.environ.get("TG_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID") or env.get("TG_CHAT_ID") or env.get("TELEGRAM_CHAT_ID") or ""
    return tok, chat


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


def _notify(title: str, msg: str, sound: str = "Glass", tg: bool = True) -> None:
    plain_t, plain_m = _strip_html(title), _strip_html(msg)
    print(f"🔔 {plain_t} — {plain_m}")
    if sys.platform == "darwin" and shutil.which("osascript"):
        safe_t = plain_t.replace('"', '\\"')
        safe_m = plain_m.replace('"', '\\"')
        subprocess.run(["osascript", "-e",
                        f'display notification "{safe_m}" with title "{safe_t}" sound name "{sound}"'],
                       capture_output=True)
    if tg:
        tg_tok, chat = _load_tg()
        if tg_tok and chat and shutil.which("curl"):
            for cid in (c.strip() for c in chat.split(",") if c.strip()):
                subprocess.run(["curl", "-s", "-m", "10", "-o", "/dev/null",
                                "--data-urlencode", f"chat_id={cid}",
                                "--data-urlencode", f"text=🤖 {title}\n{msg}",
                                "--data-urlencode", "parse_mode=HTML",
                                f"https://api.telegram.org/bot{tg_tok}/sendMessage"],
                               capture_output=True)


def _meter_alert(provider: str, plan: str | None, kind: str, win: str,
                 pct, reset_txt, p_pct=None, n_tok: int | None = None,
                 flagged: bool = False, early: bool = False,
                 prev_reset: str | None = None) -> tuple[str, str, str]:
    """(title, msg, sound) padronizado p/ alertas de medidor (Claude e Codex iguais).
    kind: reset | drop | cap | credits ; win: '5h' | 'semanal'.
    flagged=True adiciona ⚠️ ao título (drop fora do horário de reset esperado).
    early=True => reset ANTECIPADO (anormal): a Anthropic zerou a cota antes da janela
    programada; prev_reset é o horário que estava previsto (p/ o texto 'era p/ ...').
    Título e msg usam HTML (Telegram parse_mode=HTML); _notify strip para console/macOS."""
    who = provider + (f" · {plan}" if plan else "")
    who_b = f"<b>{_html.escape(who)}</b>"
    sound = "Submarine" if win == "semanal" else "Ping"
    r_txt = _html.escape(re.sub(r"\s*\([^)]*\)\s*$", "", reset_txt or "?").strip())
    flag = " ⚠️" if flagged else ""
    def r(x):
        return round(x) if x is not None else 0
    if kind == "reset":
        if early:
            pr = _html.escape(re.sub(r"\s*\([^)]*\)\s*$", "", prev_reset or "").strip())
            era = f" (era p/ {pr})" if pr else ""
            return (f"🟠 {who_b} · {win} reset ANTECIPADO",
                    f"Anthropic resetou antes do previsto{era} — nova janela {win}, "
                    f"<b>{r(pct)}%</b> usado · reset {r_txt}", sound)
        return (f"🟢 {who_b} · {win} resetou",
                f"Nova janela {win} — <b>{r(pct)}%</b> usado · reset {r_txt}", sound)
    if kind == "drop":
        if r(pct) == 0:
            return (f"🟢 {who_b} · {win} resetou{flag}",
                    f"Nova janela {win} — <b>0%</b> usado · reset {r_txt}", sound)
        return (f"🔵 {who_b} · {win} liberou{flag}",
                f"Uso caiu <b>{r(p_pct)}% → {r(pct)}%</b> · reset {r_txt}", sound)
    if kind == "cap":
        return (f"🔴 {who_b} · {win} em 100%",
                f"Janela {win} capada · reset {r_txt}", "Sosumi")
    if kind == "credits":
        t = f"{n_tok:,}".replace(",", ".") if n_tok is not None else "?"
        return (f"💳 {who_b} · {win} em crédito",
                f"<b>{t}</b> tokens além da cota · reset {r_txt}", "Glass")
    return (f"{who_b} · {win}", "", sound)


def _fetch_usage_once() -> str:
    if not shutil.which("claude"):
        return ""
    try:
        out = subprocess.run(
            ["claude", "-p", "/usage", "--model", METER_MODEL, "--output-format", "json"],
            capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
        ).stdout
    except Exception:
        return ""
    if not out:
        return ""
    try:
        return json.loads(out).get("result", "") or ""
    except Exception:
        return out


def _fetch_usage() -> str | None:
    for attempt in range(1, FETCH_RETRIES + 1):
        u = _fetch_usage_once()
        if u and re.search(r"Current session|% used", u, re.IGNORECASE):
            return u
        if attempt < FETCH_RETRIES:
            time.sleep(2)
    return None


def _warmup_usage() -> None:
    """Gera um tiquinho de uso (prompt trivial no modelo mais barato) para 'destravar' o /usage,
    que parece omitir a linha 'resets' enquanto a janela está em 0% (ocioso). É o análogo do
    'forçar um rollout' do Codex, mas gerando atividade — a única alavanca do lado Claude, já
    que não há arquivo local com o horário. Best-effort e silencioso; a releitura seguinte diz
    se funcionou. Consome um pouco da cota recém-resetada (por isso é opcional, METER_WARMUP)."""
    if not shutil.which("claude"):
        return
    try:
        subprocess.run(
            ["claude", "-p", "responda apenas: ok", "--model", METER_MODEL],
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _parse_line(usage: str, prefix: str) -> tuple[int | None, str | None]:
    # ignora as linhas por-modelo "(... only)": assim o prefixo genérico "Current week"
    # casa a linha agregada mesmo se o /usage relabelar (ex.: "(all Claude models)" ->
    # "(combined)"), sem cair numa "(Sonnet only)".
    line = next((ln for ln in usage.splitlines()
                 if prefix.lower() in ln.lower() and "only" not in ln.lower()), None)
    if not line:
        return None, None
    mp = re.search(r"(\d+)%\s*used", line, re.IGNORECASE)
    mr = re.search(r"resets\s+(.+)$", line, re.IGNORECASE)
    pct = int(mp.group(1)) if mp else None
    reset = mr.group(1).strip() if mr else None
    return pct, reset


def _line_tz(s: str) -> str:
    """Fuso reportado na própria linha do /usage ('(America/Sao_Paulo)'), se válido;
    senão METER_TZ. A conta pode reportar num fuso diferente do METER_TZ do monitor —
    ancorar o reset no fuso errado desloca o epoch pela diferença de offset (horas)."""
    m = re.search(r"\(([^)]+)\)\s*$", s or "")
    if m and ZoneInfo is not None:
        try:
            ZoneInfo(m.group(1))
            return m.group(1)
        except Exception:
            pass
    return METER_TZ


def _reset_to_epoch(s: str | None) -> float | None:
    """'Jun 10 at 3pm (America/Sao_Paulo)' -> epoch (no fuso da própria linha).
    Tolera relógio de 24h ('at 15:00'), sem minutos ('at 3pm') e a forma relativa
    ('in 2 hours' / 'in 45 minutes') — resiliência à deriva de formato do /usage."""
    if not s or ZoneInfo is None:
        return None
    tzname = _line_tz(s)
    body = re.sub(r"\s*\(.*\)$", "", s).strip()          # tira "(timezone)"
    # Forma relativa: "in N hour(s)/minute(s)" (ancorada em agora, sem fuso).
    rel = re.match(r"in\s+(\d+)\s*(hour|hr|minute|min)", body, re.IGNORECASE)
    if rel:
        n = int(rel.group(1))
        secs = n * (3600 if rel.group(2).lower().startswith(("hour", "hr")) else 60)
        return datetime.now(timezone.utc).timestamp() + secs
    # Forma absoluta: am/pm OPCIONAL — sem o token, hh é lido como relógio de 24h.
    m = re.match(r"([A-Za-z]{3})\s+(\d{1,2})\s+at\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)?", body, re.IGNORECASE)
    if not m:
        return None
    mon, day, hh, mm, ap = m.groups()
    mon_n = _MONTHS.get(mon[:3].title())
    if not mon_n:
        return None
    hh = int(hh)
    if ap:
        hh = hh % 12 + (12 if ap.lower() == "pm" else 0)
    mm = int(mm) if mm else 0
    try:
        tz = ZoneInfo(tzname)
        now_local = datetime.now(tz)
        dt = datetime(now_local.year, mon_n, int(day), hh, mm, tzinfo=tz)
        # O /usage só reporta resets FUTUROS. Se ancorar no ano corrente jogou a data
        # para o passado (reset de janeiro lido em dezembro), é do ano seguinte — senão
        # a virada de ano produziria um salto de ~1 ano no epoch e um reset espúrio.
        if dt < now_local - timedelta(hours=12):
            dt = dt.replace(year=dt.year + 1)
        return dt.timestamp()
    except Exception:
        return None


def meter_once(con: sqlite3.Connection, notify: bool = True) -> bool:
    """Uma leitura do /usage: grava na tabela meter e dispara notificação de reset/queda."""
    usage = _fetch_usage()
    if usage is None:
        print("⚠️  /usage não retornou os percentuais (vazio/genérico).")
        return False
    s_pct, s_reset = _parse_line(usage, "Current session")
    w_pct, w_reset = _parse_line(usage, "Current week")

    # estado anterior p/ detectar reset/queda, p/ o reconfirm saber se ANTES havia horário, e
    # p/ o fallback de inferência carregar o horário (real ou estimado) adiante.
    prev = con.execute(
        "SELECT session_pct, session_reset, session_reset_epoch, week_pct, week_reset, week_reset_epoch "
        "FROM meter ORDER BY ts_epoch DESC LIMIT 1"
    ).fetchone()
    p_spct, p_sr_txt, p_sre, p_wpct, p_wr_txt, p_wre = prev if prev else (None, None, None, None, None, None)

    # RECONFIRM de leitura degradada: uma janela com % mas SEM horário de reset (reset None)
    # aparece no momento de um reset — inclusive do RESET ANTECIPADO (a Anthropic zera a cota
    # antes da janela programada). Visto ao vivo: 5h e semanal em 0% + reset None por ~20min.
    # Como o /usage roda barato (haiku), refaz UMA leitura ao vivo p/ (a) recuperar o novo
    # horário de reset e (b) separar um reset real de um glitch transitório — se a releitura
    # trouxer de volta o valor ANTIGO não-zero, foi glitch e é adotado (não vira reset falso).
    # Mesma ideia do reconfirm ao vivo do Codex. Dispara na TRANSIÇÃO bom->degradado (o poll
    # anterior tinha horário). Uma degradação PLANA (0%+None persistente) grava epoch NULL e
    # se desarma sozinha no próximo poll; mas um /usage que OSCILA (degradado<->saudável)
    # re-armaria a cada poll — então um cooldown (RECONFIRM_COOLDOWN, marcado por 'reconfirm'
    # na tabela) limita a ~1 releitura por janela de cooldown, mesmo oscilando.
    def _degraded(pct, reset):
        return pct is not None and reset is None
    s_bad = _degraded(s_pct, s_reset) and p_sre is not None
    w_bad = _degraded(w_pct, w_reset) and p_wre is not None
    did_reconfirm = False
    if METER_RECONFIRM and (s_bad or w_bad):
        last_rc = con.execute("SELECT MAX(ts_epoch) FROM meter WHERE event LIKE '%reconfirm%'").fetchone()[0]
        if last_rc is None or time.time() - last_rc >= RECONFIRM_COOLDOWN:
            did_reconfirm = True
            def _adopt(u):                       # adota só uma releitura SAUDÁVEL da janela degradada
                nonlocal s_pct, s_reset, w_pct, w_reset
                if u is None:
                    return False
                s2p, s2r = _parse_line(u, "Current session")
                w2p, w2r = _parse_line(u, "Current week")
                a = False
                if s_bad and _degraded(s_pct, s_reset) and s2p is not None and s2r is not None:
                    s_pct, s_reset = s2p, s2r; a = True
                if w_bad and _degraded(w_pct, w_reset) and w2p is not None and w2r is not None:
                    w_pct, w_reset = w2p, w2r; a = True
                return a
            def _still():                        # ainda degradado numa janela que motivou o reconfirm
                return (s_bad and _degraded(s_pct, s_reset)) or (w_bad and _degraded(w_pct, w_reset))
            adopted = _adopt(_fetch_usage())
            # WARM-UP: se a releitura ainda veio sem horário, gera um tiquinho de uso no modelo
            # mais barato p/ tirar a janela de 0% (o /usage parece omitir o 'resets' com ela em
            # 0%) e relê UMA vez, buscando o horário REAL antes de cair na estimativa.
            if METER_WARMUP and _still():
                print("🔥 warm-up: gerando uso mínimo p/ destravar o horário do /usage...")
                _warmup_usage()
                if _adopt(_fetch_usage()):
                    adopted = True
            print("🔁 releitura de confirmação do /usage"
                  + (" — horário de reset recuperado." if adopted else " — ainda sem horário de reset."))

    # /usage passou no aceite mas NENHUM percentual parseou (5h e semanal): não grava linha
    # all-NULL. Ela poluiria o histórico (as subqueries <100/>=100 tratam NULL como não
    # comparável) e reiniciaria a idade da leitura no gate sem trazer informação nova.
    # NB: um reset (natural ou antecipado) chega como 0% — pct=0 NÃO é None e é preservado.
    if s_pct is None and w_pct is None:
        print("⚠️  /usage sem percentuais parseáveis (5h e semanal); leitura descartada.")
        return False
    now = datetime.now(timezone.utc)
    print(f"📊 5h: {s_pct}% usado · reset {s_reset}  |  semanal: {w_pct}% usado · reset {w_reset}")

    s_re = _reset_to_epoch(s_reset)
    w_re = _reset_to_epoch(w_reset)

    # --- Fallback de inferência do horário de reset (simples, tipo Codex) ---
    # Quando um reset chega SEM horário (o /usage não traz a linha 'resets') e o reconfirm não
    # recuperou, estima o próximo reset como agora + a duração da janela (5h / 7d), UMA vez no
    # momento do reset, e carrega esse valor adiante enquanto a janela seguir zerada — até um
    # poll saudável trazer o horário real. Marca '≈' p/ deixar claro que é estimativa.
    if METER_INFER_RESET:
        now_ts = now.timestamp()
        if s_pct is not None and s_re is None:
            if p_sre is not None and p_spct is not None and round(s_pct) == round(p_spct):
                s_reset, s_re = p_sr_txt, p_sre                  # mesma janela zerada: carrega
            elif round(s_pct) == 0:
                s_re = now_ts + 5 * 3600                          # estima o próximo reset (5h)
                s_reset = f"≈ {_fmt_epoch(s_re)}"
        if w_pct is not None and w_re is None:
            if p_wre is not None and p_wpct is not None and round(w_pct) == round(p_wpct):
                w_reset, w_re = p_wr_txt, p_wre
            elif round(w_pct) == 0:
                w_re = now_ts + 7 * 86400                         # estima o próximo reset (7d)
                w_reset = f"≈ {_fmt_epoch(w_re)}"

    # Texto de reset presente mas não interpretável => alerta de reset/cap fica mudo.
    # Avisa uma vez por formato para tornar a deriva visível (ex.: novo layout do /usage).
    for txt, epoch in ((s_reset, s_re), (w_reset, w_re)):
        if txt and epoch is None and txt not in _RESET_UNPARSED_WARNED:
            _RESET_UNPARSED_WARNED.add(txt)
            print(f"⚠️  horário de reset não interpretável: {txt!r} — alertas de reset/cap "
                  f"desta janela ficam mudos até o formato ser suportado.")
    events = []
    if prev:
        # p_spct/p_sre/p_wpct/p_wre já desempacotados do prev (6 colunas) acima.
        for key, win, sound, pct, p_pct, rep, p_rep, reset_txt in (
            ("5h", "5h", "Ping", s_pct, p_spct, s_re, p_sre, s_reset),
            ("week", "semanal", "Submarine", w_pct, p_wpct, w_re, p_wre, w_reset),
        ):
            # ANTECIPADO (reset anormal): a janela renovou ANTES do horário previsto (p_rep) —
            # a Anthropic zerou a cota fora da janela programada. Natural = no horário ou depois.
            # p_rep None (horário anterior desconhecido) => não dá p/ classificar: trata como
            # natural, sem o marcador de antecipado indevido.
            early = p_rep is not None and now.timestamp() < p_rep - RESET_TOLERANCE
            prev_reset_txt = _fmt_epoch(p_rep) if early else None
            ev_reset = f"reset_{key}_early" if early else f"reset_{key}"
            # exige pct conhecido: um avanço de epoch com pct=None (parse falhou) não deve
            # virar 'resetou — 0% usado' e enganar um runner a retomar cedo demais.
            if pct is not None and rep and p_rep and rep - p_rep > RESET_TOLERANCE:
                # reset com horário NOVO já conhecido (o /usage reporta o próximo reset).
                events.append(ev_reset)
                _notify(*_meter_alert("Claude Code", None, "reset", win, pct, reset_txt, early=early, prev_reset=prev_reset_txt), tg=notify)
            elif pct is not None and p_pct is not None and pct < p_pct - DROP_THRESHOLD:
                if round(pct) == 0:
                    # zerou = reset (natural ou antecipado); o horário pode ainda estar ausente
                    # (reset ?) — o reconfirm tenta recuperá-lo, e se não vier, um poll saudável
                    # seguinte preenche o horário na tabela.
                    events.append(ev_reset)
                    _notify(*_meter_alert("Claude Code", None, "reset", win, pct, reset_txt, early=early, prev_reset=prev_reset_txt), tg=notify)
                else:
                    # queda parcial (não zerou) = cota liberou, não é reset completo. Marca ⚠️
                    # quando cai LONGE (±30min) do reset previsto — drop suspeito, fora da janela
                    # (mesmo critério do Codex). p_rep None (horário anterior desconhecido) => não
                    # marca, senão a deriva de formato viria com ⚠️ indevido.
                    events.append(f"drop_{key}")
                    far = p_rep is not None and abs(now.timestamp() - p_rep) >= 1800
                    _notify(*_meter_alert("Claude Code", None, "drop", win, pct, reset_txt, p_pct, flagged=far), tg=notify)

        # (1) janela 5h ATINGIU 100% (transição <100 -> 100). Compara com a última leitura
        # de % NÃO-NULA (não só a linha imediatamente anterior): se o poll anterior falhou
        # o parse do 5h (session_pct NULL), a transição <100->100 ainda é detectada e o
        # alerta de cap não some.
        last_spct_row = con.execute(
            "SELECT session_pct FROM meter WHERE session_pct IS NOT NULL ORDER BY ts_epoch DESC LIMIT 1"
        ).fetchone()
        last_spct = last_spct_row[0] if last_spct_row else None
        if s_pct is not None and s_pct >= CREDIT_PCT and last_spct is not None and last_spct < CREDIT_PCT:
            events.append("cap_5h")
            _notify(*_meter_alert("Claude Code", None, "cap", "5h", s_pct, s_reset), tg=notify)

    # (2) CRÉDITOS EM USO: medidor a 100% + tokens reais novos após o início do cap.
    # Com excedente desligado, 100% = robô para; logo, token a 100% = crédito.
    if s_pct is not None and s_pct >= CREDIT_PCT:
        ingest(con, verbose=False)                       # atualiza usage antes de medir
        # cap_start = PRIMEIRA leitura 100% da sequência atual (não a última <100%):
        # tokens entre a última <100% e a 1ª 100% são o que CONSUMIU a cota (assinatura);
        # só o que vem DEPOIS do 100% confirmado é crédito.
        last_below = con.execute(
            "SELECT MAX(ts_epoch) FROM meter WHERE session_pct < ? AND ts_epoch < ?",
            (CREDIT_PCT, now.timestamp()),
        ).fetchone()[0] or 0
        cap_start = con.execute(
            "SELECT MIN(ts_epoch) FROM meter WHERE session_pct >= ? AND ts_epoch > ? AND ts_epoch <= ?",
            (CREDIT_PCT, last_below, now.timestamp()),
        ).fetchone()[0] or now.timestamp()
        tok = _real_output_tokens(con, cap_start, now.timestamp())
        already = con.execute(
            "SELECT COUNT(*) FROM meter WHERE ts_epoch > ? AND event LIKE '%credits_started%'",
            (cap_start,),
        ).fetchone()[0]
        if tok > 0 and not already:
            events.append("credits_started")
            _notify(*_meter_alert("Claude Code", None, "credits", "5h", s_pct, s_reset, n_tok=tok), tg=notify)

    if did_reconfirm:
        events.append("reconfirm")          # marca p/ o cooldown do próximo poll
    con.execute(
        "INSERT OR REPLACE INTO meter VALUES (?,?,?,?,?,?,?,?,?)",
        (now.isoformat(), now.timestamp(), s_pct, s_reset, s_re, w_pct, w_reset, w_re,
         ",".join(events) or None),
    )
    con.commit()
    return True


def meter_report(con: sqlite3.Connection, args) -> None:
    rows = con.execute(
        "SELECT ts, session_pct, session_reset, week_pct, week_reset, event "
        "FROM meter ORDER BY ts_epoch DESC LIMIT ?", (args.limit,),
    ).fetchall()
    print(f"\n=== Medidor oficial (/usage) — últimas {len(rows)} leituras ===\n")
    if not rows:
        print("(sem leituras — rode: token_monitor.py meter)\n"); return
    tzname = next((tz for _, _, sr, _, wr, _ in rows
                   for tz in (_reset_disp(sr)[1], _reset_disp(wr)[1]) if tz), None)
    if tzname:
        print(f"(horários de reset no fuso {tzname})\n")
    print(f"{'quando (UTC)':<22}{'5h':>5}{'  reset 5h':<20}{'sem':>5}{'  reset semanal':<20}{'  evento'}")
    print("-" * 92)
    for ts, sp, sr, wp, wr, ev in rows:
        print(f"{ts[:19]:<22}{_pctstr(sp):>5}  {_reset_disp(sr)[0]:<18}{_pctstr(wp):>5}  {_reset_disp(wr)[0]:<18}{'  '+ev if ev else ''}")
    print()


# ----------------------------- Gate ---------------------------------------- #
# Decisão única "posso seguir trabalhando?" para runners contínuos (ver SKILL).
# Lê a ÚLTIMA leitura do medidor (barato, sem chamar /usage); só faz uma leitura
# AO VIVO se a última estiver velha (> --max-age) ou com --refresh. Assim reaproveita
# o `meter --watch` que já popula o banco e evita gastar tempo/latência por checagem.
# Saída legível + --json, e exit code: 0=GO · 10=PAUSE · 2=UNKNOWN (sem leitura).
GATE_GO, GATE_PAUSE, GATE_UNKNOWN = 0, 10, 2


def _latest_meter(con: sqlite3.Connection):
    return con.execute(
        "SELECT ts_epoch, session_pct, session_reset, week_pct, week_reset "
        "FROM meter ORDER BY ts_epoch DESC LIMIT 1"
    ).fetchone()


# --------------------------- Codex (CLI) rate-limit ------------------------- #
# O Codex CLI não expõe comando de usage, mas grava os rate limits da conta em
# CADA rollout de sessão (~/.codex/sessions/AAAA/MM/DD/rollout-*.jsonl), num evento
# `token_count`: payload.rate_limits.primary (janela 300min = 5h) e .secondary
# (10080min = semanal), com used_percent + resets_at (epoch). Lemos o mais fresco
# ao vivo (custo zero); a leitura vale "de quando o Codex rodou pela última vez".
CODEX_SESSIONS_DIR = Path(os.environ.get("CODEX_SESSIONS_DIR", str(Path.home() / ".codex" / "sessions")))


def _fmt_epoch(epoch) -> str | None:
    if not epoch:
        return None
    try:
        if ZoneInfo:
            tz, tzname = ZoneInfo(METER_TZ), METER_TZ
        else:
            tz, tzname = timezone.utc, "UTC"
        dt = datetime.fromtimestamp(float(epoch), tz)
        # mesmo padrão da linha do Claude: "Jun 24 at 9:39pm (America/Sao_Paulo)"
        return (dt.strftime("%b %-d at %-I:%M%p").replace("AM", "am").replace("PM", "pm")
                + f" ({tzname})")
    except Exception:
        return str(epoch)


def _pctstr(x) -> str:
    return f"{round(x)}%" if x is not None else "?"


def _reset_disp(s: str | None) -> tuple[str, str | None]:
    """'Jun 15 at 9pm (America/Sao_Paulo)' -> ('Jun 15 at 9pm', 'America/Sao_Paulo').
    Separa o fuso (redundante entre linhas — o mesmo p/ toda a conta) do horário, para
    o relatório mostrar o horário INTEIRO em coluna estreita e citar o fuso uma vez só,
    em vez de cortar o '(...)' no meio e esconder o fuso."""
    if not s:
        return "?", None
    m = re.search(r"\(([^)]+)\)\s*$", s)
    tz = m.group(1) if m else None
    body = re.sub(r"\s*\([^)]*\)\s*$", "", s).strip()
    return (body or "?"), tz


def read_codex_meter(scan_files: int = 40):
    """Rate-limit mais recente do Codex a partir dos rollouts. Retorna dict
    {ts_epoch, session_pct, session_reset_epoch, week_pct, week_reset_epoch, plan}
    ou None. session=primary(5h, 300min); week=secondary(semanal, 10080min).

    Varre do rollout mais novo p/ o mais velho e PARA no primeiro arquivo com rate_limits
    (mtime mais novo = leitura mais fresca). scan_files é só o teto de segurança quando
    NENHUM rollout recente tem rate_limits — antes o corte fixo em 8 podia perder a leitura
    (usuário com muitas sessões novas sem token_count) e devolver None indevidamente."""
    if not CODEX_SESSIONS_DIR.exists():
        return None
    files = sorted(CODEX_SESSIONS_DIR.glob("*/*/*/rollout-*.jsonl"),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:scan_files]
    best = None
    for fp in files:
        try:
            for line in _iter_lines(fp):
                if '"rate_limits"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                payload = obj.get("payload") or {}
                rl = payload.get("rate_limits") or (payload.get("info") or {}).get("rate_limits")
                if not rl:
                    continue
                prim = rl.get("primary") or {}
                sec = rl.get("secondary") or {}
                try:
                    ts = parse_ts(obj.get("timestamp", "")) if obj.get("timestamp") else fp.stat().st_mtime
                except Exception:
                    ts = fp.stat().st_mtime
                if best is not None and ts <= best["ts_epoch"]:
                    continue
                best = {
                    "ts_epoch": ts,
                    "session_pct": prim.get("used_percent"),
                    "session_reset_epoch": prim.get("resets_at"),
                    "week_pct": sec.get("used_percent"),
                    "week_reset_epoch": sec.get("resets_at"),
                    "plan": rl.get("plan_type"),
                }
        except Exception:
            continue
        # newest-first: o 1º arquivo que rende uma leitura já é o mais fresco; não precisa
        # varrer os mais antigos (evita ler dezenas de rollouts a cada poll do gate/status).
        if best is not None:
            break
    return best


_CODEX_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_CODEX_MODEL_RE = re.compile(r'"model"\s*:\s*"([^"]+)"')


def ingest_codex(con: sqlite3.Connection) -> int:
    """Varre os rollouts do Codex (~/.codex/sessions) e grava o uso de tokens por
    sessão na tabela codex_usage: total cumulativo (último token_count.total_token_usage),
    janela de tempo (1º->último timestamp = tempo ativo) e modelo. Idempotente via mtime
    (pula rollouts inalterados; re-ingere um que cresceu). Codex é assinatura: medimos
    tokens e tempo, não custo por token."""
    if not CODEX_SESSIONS_DIR.exists():
        return 0
    seen = {r[0]: (r[1] or 0) for r in con.execute(
        "SELECT rollout, updated_epoch FROM codex_usage").fetchall()}
    n = 0
    for f in CODEX_SESSIONS_DIR.glob("*/*/*/rollout-*.jsonl"):
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if seen.get(str(f), 0) >= mtime:
            continue  # inalterado desde a última ingestão
        m = _CODEX_UUID_RE.search(f.name)
        sid = m.group(0) if m else f.stem
        first = last = None
        model = None
        tot = None
        try:
            for line in f.open(errors="replace"):
                if model is None:
                    mm = _CODEX_MODEL_RE.search(line)
                    if mm:
                        model = mm.group(1)
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                t = o.get("timestamp")
                if t:
                    ep = parse_ts(t)
                    if ep:
                        if first is None:
                            first = ep
                        last = ep
                payload = o.get("payload")
                if isinstance(payload, dict) and payload.get("type") == "token_count":
                    info = payload.get("info") or {}
                    tu = info.get("total_token_usage")
                    if isinstance(tu, dict):
                        tot = tu
        except OSError:
            continue
        if not tot:
            continue
        con.execute(
            "INSERT OR REPLACE INTO codex_usage "
            "(rollout, session_id, started_epoch, ended_epoch, model, input_tokens, "
            "cached_input_tokens, output_tokens, reasoning_output_tokens, total_tokens, updated_epoch) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (str(f), sid, first, last, model,
             int(tot.get("input_tokens") or 0), int(tot.get("cached_input_tokens") or 0),
             int(tot.get("output_tokens") or 0), int(tot.get("reasoning_output_tokens") or 0),
             int(tot.get("total_tokens") or 0), mtime),
        )
        n += 1
    con.commit()
    return n


def codex_live_refresh(timeout: int = 120):
    """Gasta UM turno MÍNIMO do `codex exec` para forçar um rollout novo com os
    rate_limits atuais. O CLI do Codex não expõe fetch de uso sem turno (o /status da
    TUI só remostra o último valor recebido), então um turno trivial é a forma mais
    barata de obter um número fresco fora de um loop ativo. Retorna (status, leitura)."""
    if not shutil.which("codex"):
        return ("codex ausente no PATH", None)
    try:
        # --skip-git-repo-check: ~ não é repo git "trusted"; sem a flag o codex exec
        # recusa (exit 1). stdin=DEVNULL: o codex exec concatena stdin ao prompt e
        # travaria/lixaria a entrada herdando o TTY do --watch ("Reading additional
        # input from stdin...").
        r = subprocess.run(
            ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check",
             "-C", os.path.expanduser("~"),
             "Não use ferramentas. Responda somente com: ok"],
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        status = "ok" if r.returncode == 0 else f"exit {r.returncode}"
    except subprocess.TimeoutExpired:
        status = f"timeout {timeout}s"
    except Exception as e:  # noqa: BLE001
        status = f"erro: {e}"
    return (status, read_codex_meter())


def _codex_refresh_needed(m, force: bool = False):
    """Vale gastar o turno? Só quando um reset já cruzou desde o último rollout (a
    leitura ficou velha de verdade) ou quando forçado. Antes do reset a leitura ainda é
    válida (se o Codex não rodou, o uso não mudou), então não há o que confirmar."""
    if force:
        return True, "forçado (--force)"
    if not m or m.get("session_pct") is None:
        return True, "sem rollout com rate_limits"
    now = datetime.now(timezone.utc).timestamp()
    ts = m.get("ts_epoch")
    for label, key in (("5h", "session_reset_epoch"), ("semanal", "week_reset_epoch")):
        re = m.get(key)
        if re and ts and now >= re > ts:
            return True, f"reset {label} já cruzou desde o rollout"
    return False, ""


def codex_meter_once(con: sqlite3.Connection, notify: bool = True, as_json: bool = False) -> bool:
    """Uma leitura do rate-limit do Codex: grava na tabela codex_meter e dispara
    notificação de reset/queda/cap (mesma lógica do meter_once do Claude)."""
    m = read_codex_meter()
    if not m or m.get("session_pct") is None:
        if as_json: print(json.dumps({"available": False}))
        else: print("Codex: sem leitura de rate-limit (nenhum rollout recente com rate_limits).")
        return False
    ts_epoch = m["ts_epoch"]
    ts_iso = datetime.fromtimestamp(ts_epoch, timezone.utc).isoformat()
    s_pct = m["session_pct"]; s_re = m["session_reset_epoch"]; s_reset = _fmt_epoch(s_re)
    w_pct = m["week_pct"]; w_re = m["week_reset_epoch"]; w_reset = _fmt_epoch(w_re)
    age = max(0, int(datetime.now(timezone.utc).timestamp() - ts_epoch))

    # Evento vs o último estado gravado. É importante incluir uma linha com o mesmo
    # ts_epoch: um rollout parado é lido novamente a cada poll e deve ser idempotente.
    # Excluí-la faria o mesmo snapshot ser comparado para sempre com o rollout anterior,
    # repetindo o alerta de reset em todo ciclo do watch.
    prev = con.execute(
        "SELECT session_pct, session_reset_epoch, week_pct, week_reset_epoch "
        "FROM codex_meter WHERE ts_epoch <= ? ORDER BY ts_epoch DESC LIMIT 1",
        (ts_epoch,),
    ).fetchone()
    events = []
    if prev:
        p_spct, p_sre, p_wpct, p_wre = prev
        for key, lbl, sound, pct, p_pct, rep, p_rep, reset_txt in (
            ("5h", "5h", "Ping", s_pct, p_spct, s_re, p_sre, s_reset),
            ("week", "SEMANAL", "Submarine", w_pct, p_wpct, w_re, p_wre, w_reset),
        ):
            # O resets_at do Codex é um INSTANTE ABSOLUTO estável dentro da janela (medido
            # nos rollouts reais: fica fixo enquanto o % sobe e só avança num reset de fato).
            # Logo o avanço > tolerância JÁ é reset — mesmo com o % igual (janela semanal
            # re-saturada 100%->100% no reset). A idempotência (não repetir o MESMO reset em
            # rollouts seguintes) vem do prev por `ts_epoch <= ?`, não de exigir mudança de %.
            if pct is not None and rep and p_rep and rep - p_rep > RESET_TOLERANCE:
                events.append(f"reset_{key}")
                _notify(*_meter_alert("Codex", m.get("plan"), "reset", "5h" if key == "5h" else "semanal", pct, reset_txt), tg=notify)
            elif pct is not None and p_pct is not None and pct < p_pct - DROP_THRESHOLD:
                events.append(f"drop_{key}")
                # p_rep None = reset anterior desconhecido: não dá para dizer se a queda
                # coincide com o reset esperado, então NÃO marca como suspeita (⚠️).
                flagged = p_rep is not None and abs(ts_epoch - p_rep) >= 1800
                _notify(*_meter_alert("Codex", m.get("plan"), "drop", "5h" if key == "5h" else "semanal", pct, reset_txt, p_pct, flagged=flagged), tg=notify)
        if s_pct is not None and s_pct >= CREDIT_PCT and p_spct is not None and p_spct < CREDIT_PCT:
            events.append("cap_5h")
            _notify(*_meter_alert("Codex", m.get("plan"), "cap", "5h", s_pct, s_reset), tg=notify)

    con.execute(
        "INSERT OR REPLACE INTO codex_meter VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ts_iso, ts_epoch, s_pct, s_reset, s_re, w_pct, w_reset, w_re, m.get("plan"), ",".join(events) or None),
    )
    con.commit()

    if as_json:
        print(json.dumps({"available": True, "plan": m["plan"], "age_seconds": age,
                          "session_pct": s_pct, "session_reset": s_reset,
                          "week_pct": w_pct, "week_reset": w_reset,
                          "event": ",".join(events) or None}, ensure_ascii=False))
    else:
        print(f"📊 codex 5h: {_pctstr(s_pct)} usado · reset {s_reset}  |  "
              f"semanal: {_pctstr(w_pct)} usado · reset {w_reset}  (rollout {age}s, {m['plan']})"
              + (f"  evento: {','.join(events)}" if events else ""))
    return True


def _make_codex_tick(con: sqlite3.Connection, notify: bool, auto_refresh: bool):
    """Fábrica do passo do Codex para os loops de watch.

    Com auto_refresh (contexto de --watch), confirma ao vivo (1 turno MÍNIMO do
    `codex exec`) UMA vez quando um reset cruza, ANTES de ler — senão o rollout fica
    preso no % anterior ao reset (ele só se atualiza quando o Codex roda de fato).
    O guard por ts_epoch garante "uma vez por snapshot": se o turno falhar (ou não
    nascer rollout novo), o ts não muda e não regastamos cota a cada poll; quando o
    refresh dá certo, nasce um rollout com ts/reset novos e o próximo reset volta a
    disparar naturalmente. Sem auto_refresh é só leitura (igual antes)."""
    confirmed_ts = set()

    def tick() -> bool:
        if auto_refresh:
            m = read_codex_meter()
            need, why = _codex_refresh_needed(m)
            ts = (m or {}).get("ts_epoch")
            if need and ts not in confirmed_ts:
                confirmed_ts.add(ts)
                print(f"… codex: confirmando ao vivo (turno mínimo do codex exec): {why}")
                st, _ = codex_live_refresh()
                print(f"  codex exec: {st}")
        return codex_meter_once(con, notify=notify)

    return tick


def codex_meter_report(con: sqlite3.Connection, args) -> None:
    rows = con.execute(
        "SELECT ts, plan, session_pct, session_reset, week_pct, week_reset, event "
        "FROM codex_meter ORDER BY ts_epoch DESC LIMIT ?", (args.limit,),
    ).fetchall()
    print(f"\n=== Medidor Codex (rollouts) — últimas {len(rows)} leituras ===\n")
    if not rows:
        print("(sem leituras — rode: token_monitor.py codex-meter)\n"); return
    tzname = next((tz for _, _, _, sr, _, wr, _ in rows
                   for tz in (_reset_disp(sr)[1], _reset_disp(wr)[1]) if tz), None)
    if tzname:
        print(f"(horários de reset no fuso {tzname})\n")
    print(f"{'quando (UTC)':<22}{'plano':<7}{'5h':>6}{'  reset 5h':<20}{'sem':>6}{'  reset semanal':<20}{'  evento'}")
    print("-" * 100)
    for ts, plan, sp, sr, wp, wr, ev in rows:
        print(f"{ts[:19]:<22}{(plan or '?'):<7}{_pctstr(sp):>6}  {_reset_disp(sr)[0]:<18}{_pctstr(wp):>6}  {_reset_disp(wr)[0]:<18}{'  '+ev if ev else ''}")
    print()


def _claude_reading(con: sqlite3.Connection, args) -> dict:
    """Leitura Claude (medidor /usage via DB, refaz se velha)."""
    now = datetime.now(timezone.utc).timestamp()
    row = _latest_meter(con)
    age = (now - row[0]) if row else None
    stale = row is None or age > args.max_age
    refreshed = False
    if args.refresh or stale:
        meter_once(con, notify=not args.no_notify)
        row = _latest_meter(con)
        age = 0.0 if row else None
        refreshed = True
    if not row or row[1] is None or row[3] is None:
        return {"label": "claude", "session_pct": None, "session_reset": None,
                "week_pct": None, "week_reset": None, "source": "sem leitura", "refreshed": refreshed}
    _, s_pct, s_reset, w_pct, w_reset = row
    src = "ao vivo" if refreshed else (f"cache {int(age)}s" if age is not None else "—")
    return {"label": "claude", "session_pct": s_pct, "session_reset": s_reset,
            "week_pct": w_pct, "week_reset": w_reset, "source": src, "refreshed": refreshed}


def _codex_reading(args) -> dict:
    m = read_codex_meter()
    if not m or m.get("session_pct") is None:
        return {"label": "codex", "session_pct": None, "session_reset": None,
                "week_pct": None, "week_reset": None, "source": "sem leitura", "refreshed": False}
    now = datetime.now(timezone.utc).timestamp()
    # O rollout do Codex é um snapshot estático da última vez que ele rodou; NÃO se
    # atualiza sozinho. Mas resets_at é epoch absoluto, então uma janela cujo reset já
    # passou está recuperada (~0%) mesmo sem o Codex rodar de novo. Sem isto o gate
    # ficaria preso no % antigo para sempre e um runner gated em codex nunca acordaria.
    s_pct, w_pct = m["session_pct"], m["week_pct"]
    s_re, w_re = m.get("session_reset_epoch"), m.get("week_reset_epoch")
    ts = m.get("ts_epoch")
    note = ""
    # Só zera se o reset é POSTERIOR ao snapshot e já passou (janela realmente recuperada).
    # Um resets_at já vencido NO MOMENTO do rollout é ruído do CLI, não um reset novo — zerar
    # aí faria o gate liberar (GO) com uma leitura de, digamos, 95% ainda válida. Mesmo guard
    # de _codex_refresh_needed (now >= re > ts).
    if s_re and ts and s_re > ts and now >= s_re:
        s_pct = 0.0; note += " · 5h resetou"
    if w_re and ts and w_re > ts and now >= w_re:
        w_pct = 0.0; note += " · semanal resetou"
    age = max(0, int(now - m["ts_epoch"])) if m.get("ts_epoch") else 0
    return {"label": "codex", "session_pct": s_pct, "session_reset": _fmt_epoch(s_re),
            "week_pct": w_pct, "week_reset": _fmt_epoch(w_re),
            "source": f"rollout {age}s{note}", "refreshed": False}


def gate(con: sqlite3.Connection, args) -> int:
    """Veredito de rate limit (GO/PAUSE/UNKNOWN). Com --provider both, PAUSE se
    Claude OU Codex estourar. Retorna o exit code."""
    provider = getattr(args, "provider", "claude")
    readings = []
    if provider in ("claude", "both"):
        readings.append(_claude_reading(con, args))
    if provider in ("codex", "both"):
        readings.append(_codex_reading(args))

    reasons = []
    any_valid = False
    for rd in readings:
        if rd["session_pct"] is None or rd["week_pct"] is None:
            continue
        any_valid = True
        if rd["session_pct"] >= args.max_5h:
            reasons.append(f"{rd['label']} 5h em {round(rd['session_pct'])}% (>= {args.max_5h}%)")
        if rd["week_pct"] >= args.max_week:
            reasons.append(f"{rd['label']} semanal em {round(rd['week_pct'])}% (>= {args.max_week}%)")

    if not any_valid:
        decision, code = "UNKNOWN", GATE_UNKNOWN
        reasons = reasons or ["sem leitura válida do medidor"]
    else:
        decision = "GO" if not reasons else "PAUSE"
        code = GATE_GO if not reasons else GATE_PAUSE

    # binding (pior leitura) para o cabeçalho + compat dos campos de topo no --json
    valids = [r for r in readings if r["session_pct"] is not None]
    worst = max(valids, key=lambda r: r["session_pct"], default=None)
    s_pct = worst["session_pct"] if worst else None
    s_reset = worst["session_reset"] if worst else None
    w_pct = worst["week_pct"] if worst else None
    w_reset = worst["week_reset"] if worst else None
    refreshed = any(r.get("refreshed") for r in readings)

    motivo = "; ".join(reasons) if reasons else ""
    if decision == "GO":
        advice = "Pode iniciar a próxima tarefa. Rode o gate de novo após 1–2 tarefas."
    elif decision == "PAUSE":
        advice = ("Pare. Não inicie novas tarefas. Faça commit do que está validado "
                  "e responda no formato de pausa abaixo (preencha <N> e as listas).")
    else:
        advice = ("Sem leitura válida do medidor — trate como PAUSE (conservador). "
                  "Rode `token_monitor.py meter` (Claude) / `codex-meter` para forçar.")
    pause_header = (
        "pausado: <N> tarefas restantes\n"
        f"motivo: {motivo or 'medidor indisponível'}\n"
        f"reset: {s_reset or '?'}\n"
        f"semanal: {w_pct if w_pct is not None else '?'}%"
    )
    verdict = {"decision": decision, "reasons": reasons, "advice": advice,
               "pause_header": None if decision == "GO" else pause_header,
               "provider": provider,
               "providers": [{"label": r["label"], "session_pct": r["session_pct"],
                              "session_reset": r["session_reset"], "week_pct": r["week_pct"],
                              "week_reset": r["week_reset"], "source": r["source"]} for r in readings],
               "session_pct": s_pct, "week_pct": w_pct, "session_reset": s_reset,
               "week_reset": w_reset, "refreshed": refreshed,
               "max_5h": args.max_5h, "max_week": args.max_week}

    if args.json:
        print(json.dumps(verdict, ensure_ascii=False))
        return code

    multi = len(readings) > 1
    for r in readings:
        pre = f"[{r['label']}] " if multi else ""
        print(f"📊 {pre}5h: {_pctstr(r['session_pct'])} · reset {r['session_reset']}  |  "
              f"semanal: {_pctstr(r['week_pct'])} · reset {r['week_reset']}  ({r['source']})")
    if motivo:
        print(f"motivo: {motivo}")
    print(f"DECISION: {decision}")
    icon = {"GO": "➡️ ", "PAUSE": "🛑", "UNKNOWN": "⚠️ "}[decision]
    print(f"{icon} {advice}")
    if decision != "GO":
        print("─────── cole e complete ───────")
        print(pause_header)
        print("────────────────────────────────")
    return code


# --------------------------- Bursts ---------------------------------------- #
# Detalhamento por "burst" = cluster de atividade separado por gap de inatividade.
# Lê os transcripts crus (gatilho/sidechain/entrypoint não estão na tabela usage)
# e cruza billing_source do banco. Identifica o que disparou cada burst:
# você (prompt manual) | wakeup (ScheduleWakeup/loop) | task-notif (tarefa em bg).

def _classify_user_event(o: dict) -> tuple[str, str] | None:
    """Classifica uma msg type=user como gatilho. Retorna (kind, detalhe) ou None."""
    m = o.get("message", {})
    c = m.get("content") if isinstance(m, dict) else m
    s = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
    if "<task-notification>" in s:
        tid = re.search(r"task-id>([^<]+)", s)
        return ("task-notif", tid.group(1) if tid else "")
    if '"wakeup"' in s or "wakeup>" in s.lower() or "<wakeup" in s.lower():
        return ("wakeup", "")
    # prompt manual: precisa de um bloco text de verdade (não tool_result)
    if isinstance(c, list):
        for x in c:
            if isinstance(x, dict) and x.get("type") == "text" and x.get("text", "").strip():
                return ("você", x["text"].strip().replace("\n", " ")[:80])
    elif isinstance(c, str) and c.strip() and not c.startswith("<"):
        return ("você", c.strip().replace("\n", " ")[:80])
    return None


def bursts_report(con: sqlite3.Connection, args) -> None:
    tz = ZoneInfo(METER_TZ) if (ZoneInfo and not args.utc) else timezone.utc
    tzname = "local" if not args.utc else "UTC"
    gap = args.gap
    # só os uuids em crédito (raros) — evita carregar a tabela usage inteira
    credit_uuids = {r[0] for r in con.execute(
        "SELECT uuid FROM usage WHERE billing_source='credits'")}

    msgs, triggers, banners = [], [], []
    sess = args.session
    for jf in PROJECTS_DIR.rglob("*.jsonl"):
        for line in _iter_lines(jf):
            if sess and sess not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if sess and not (o.get("sessionId") or "").startswith(sess):
                continue
            te = parse_ts(o.get("timestamp", ""))
            if not te:
                continue
            typ = o.get("type")
            m = o.get("message", {})
            if typ == "assistant" and isinstance(m, dict):
                txt = _join_text(m.get("content"))
                is_cap = bool(LIMIT_PATTERNS.search(txt)) if txt else False
                has_wake = any(isinstance(b, dict) and b.get("type") == "tool_use"
                               and b.get("name") == "ScheduleWakeup"
                               for b in (m.get("content") or []))
                msgs.append({
                    "te": te, "uuid": o.get("uuid"), "model": m.get("model"),
                    **_usage_tokens(m.get("usage") or {}),
                    "side": bool(o.get("isSidechain")), "ent": o.get("entrypoint"),
                    "cap": is_cap, "wake": has_wake,
                })
                if is_cap:
                    banners.append(te)
            elif typ == "user":
                ev = _classify_user_event(o)
                if ev:
                    triggers.append((te, ev[0], ev[1]))
    if not msgs:
        print(f"\n(sem mensagens para session={sess!r})\n"); return
    msgs.sort(key=lambda x: x["te"])
    triggers.sort()

    # agrupa em bursts por gap
    groups, cur = [], []
    for mo in msgs:
        if cur and mo["te"] - cur[-1]["te"] > gap:
            groups.append(cur); cur = []
        cur.append(mo)
    if cur:
        groups.append(cur)

    def lt(e, fmt="%d/%m %H:%M:%S"):
        return datetime.fromtimestamp(e, tz).strftime(fmt)

    def _usd(x):
        return cost(x["model"], _cost_row(x))

    print(f"\n=== BURSTS — session={sess or 'TODAS'} — gap>{gap // 60}min — horário {tzname} ===\n")
    for i, g in enumerate(groups, 1):
        a, z = g[0]["te"], g[-1]["te"]
        dur = int((z - a) / 60)
        real = [x for x in g if is_real_model(x["model"])]
        synth = len(g) - len(real)
        side = sum(1 for x in g if x["side"])
        models = collections.Counter(x["model"] for x in real)
        ti = sum(x["in"] for x in g); to = sum(x["out"] for x in g)
        tcr = sum(x["cr"] for x in g); tcw = sum(x["cw"] for x in g)
        usd = sum(_usd(x) for x in g)
        cred_msgs = [x for x in real if x["uuid"] in credit_uuids]
        cred = len(cred_msgs)
        usd_cred = sum(_usd(x) for x in cred_msgs)
        usd_sub = usd - usd_cred
        ents = collections.Counter(x["ent"] for x in g if x["ent"])
        ended_cap = any(x["cap"] for x in g[-8:]) or any(a <= b <= z + 90 for b in banners)
        scheduled = any(x["wake"] for x in g)
        # gatilho: último trigger com ts <= início do burst (+2s)
        trg = [t for t in triggers if t[0] <= a + 2]
        kind, detail = (trg[-1][1], trg[-1][2]) if trg else ("?", "")
        auto = kind in ("wakeup", "task-notif")
        glabel = {"você": "👤 VOCÊ (manual)", "wakeup": "⏰ WAKEUP (autônomo)",
                  "task-notif": "🔄 TASK-NOTIF (autônomo)", "?": "? (indeterminado)"}[kind]

        print(f"┌─ Burst {i}  ·  {lt(a)} → {lt(z, '%H:%M:%S')}  ·  {dur}min")
        print(f"│  gatilho:   {glabel}" + (f"  ·  task {detail}" if detail and kind == 'task-notif' else ""))
        if kind == "você" and detail:
            print(f"│             “{detail}”")
        print(f"│  msgs:      {len(g)}  (reais {len(real)} · sidechain {side} · synthetic {synth})")
        print(f"│  modelos:   " + ", ".join(f"{m.split('claude-')[-1]}×{n}" for m, n in models.most_common()))
        print(f"│  tokens:    in {ti:,} · out {to:,} · cache_r {tcr:,} · cache_w {tcw:,}")
        if cred:
            print(f"│  billing:   💳 {cred} msgs CRÉDITO (~${usd_cred:,.2f})  +  assinatura (~${usd_sub:,.2f})  ·  total ~${usd:,.2f}")
        else:
            print(f"│  billing:   assinatura 100%  ·  ~${usd:,.2f}")
        print(f"│  origem:    {', '.join(ents) or '?'}" + (f"  ·  {'AUTÔNOMO' if auto else 'interativo'}"))
        flags = []
        if scheduled: flags.append("agendou ScheduleWakeup")
        if ended_cap: flags.append("⚠️ terminou em CAP (limite)")
        if flags:
            print(f"│  eventos:   {' · '.join(flags)}")
        print("└" + "─" * 58)
    print()


# ------------------------- Calibração -------------------------------------- #
# Aprende um FATOR por modelo (custo_real = base × fator) a partir de episódios
# reais de gasto de crédito. Cada episódio é 1 equação:
#   Σ_modelo fator_m · base_cost_m(tokens)  =  gasto_real_USD
# Com N episódios de mix variado → mínimos quadrados (ridge p/ 1.0) resolve os
# fatores. Modelo pouco presente fica perto de 1.0 (nominal) — honesto, sem chute.

def _episode_tokens(con: sqlite3.Connection, a: float, b: float) -> dict:
    """Soma tokens por modelo NORMALIZADO numa janela [a,b] (epoch). {model: {in,out,cr,cw}}.
    Normaliza ('[1m]'/datadas -> base) p/ colapsar variantes do MESMO modelo num só fator;
    senão a calibração racha a alavancagem de um modelo em colunas colineares (baixa confiança)."""
    out: dict[str, dict] = {}
    for m, i, o, cr, cw in con.execute(
        f"SELECT model, SUM(input_tokens), SUM(output_tokens), SUM(cache_read), SUM(cache_write) "
        f"FROM usage WHERE ts_epoch > ? AND ts_epoch <= ? AND {REAL_MODEL_SQL} GROUP BY model",
        (a, b),
    ):
        d = out.setdefault(_norm_model(m), {"in": 0, "out": 0, "cr": 0, "cw": 0})
        d["in"] += i or 0; d["out"] += o or 0; d["cr"] += cr or 0; d["cw"] += cw or 0
    return out


def _solve_ridge(A: list, b: list, ncol: int, lam: float) -> list:
    """min ||A f - b||² + lam·||f - 1||²  via equações normais + eliminação de Gauss.
    Resolve (AᵀA + lam·I) f = Aᵀb + lam·1. Stdlib puro."""
    # M = AᵀA + lam·I  ;  v = Aᵀb + lam·1
    M = [[sum(A[r][i] * A[r][j] for r in range(len(A))) + (lam if i == j else 0.0)
          for j in range(ncol)] for i in range(ncol)]
    v = [sum(A[r][i] * b[r] for r in range(len(A))) + lam for i in range(ncol)]
    # Gauss
    for c in range(ncol):
        piv = max(range(c, ncol), key=lambda r: abs(M[r][c]))
        if abs(M[piv][c]) < 1e-12:
            continue
        M[c], M[piv] = M[piv], M[c]; v[c], v[piv] = v[piv], v[c]
        pv = M[c][c]
        M[c] = [x / pv for x in M[c]]; v[c] /= pv
        for r in range(ncol):
            if r != c and M[r][c]:
                f = M[r][c]
                M[r] = [M[r][k] - f * M[c][k] for k in range(ncol)]; v[r] -= f * v[c]
    return v


def calibrate(con: sqlite3.Connection, args) -> None:
    # --list
    if args.list:
        rows = con.execute("SELECT id, ts, note, real_usd, win_from, win_to FROM calibration ORDER BY id").fetchall()
        print(f"\n=== Episódios de calibração ({len(rows)}) ===\n")
        for i, ts, note, usd, wf, wt in rows:
            print(f"  #{i}  {ts[:19]}  US${usd:.2f}  [{(wf or '')[:16]}→{(wt or '')[:16]}]  {note or ''}")
        print()
        return

    # --solve [--apply]
    if args.solve:
        rows = con.execute("SELECT real_usd, tokens_json FROM calibration").fetchall()
        if not rows:
            print("(sem episódios — registre com: calibrate --brl <valor> [--from .. --to ..])"); return
        # Colapsa variantes ('[1m]'/datadas) do MESMO modelo num só fator — inclusive
        # episódios ANTIGOS gravados com o ID cru — p/ 1 modelo = 1 coluna/fator.
        def _norm_toks(tj):
            merged: dict = {}
            for m, t in json.loads(tj).items():
                d = merged.setdefault(_norm_model(m), {"in": 0, "out": 0, "cr": 0, "cw": 0})
                for k in ("in", "out", "cr", "cw"):
                    d[k] += t.get(k, 0)
            return merged
        eps = [(usd, _norm_toks(tj)) for usd, tj in rows]
        models = sorted({m for _, toks in eps for m in toks})
        A, b = [], []
        for usd, toks in eps:
            row = [base_cost(m, _cost_row(toks.get(m, {}))) for m in models]
            A.append(row); b.append(usd)
        lam = max(1e-6, 0.02 * max((A[r][c] for r in range(len(A)) for c in range(len(models))), default=1.0))
        f = _solve_ridge(A, b, len(models), lam)
        factors = {m: round(max(0.0, f[i]), 4) for i, m in enumerate(models)}
        # diagnóstico: erro por episódio com os fatores novos
        print(f"\n=== Fatores resolvidos ({len(eps)} episódios, {len(models)} modelos, ridge λ={lam:.3g}) ===\n")
        if len(eps) < len(models):
            # Menos episódios que modelos => sistema indeterminado: só a regularização (λ)
            # torna resolvível e os fatores tendem a ~1.0 sem separar os modelos.
            print(f"⚠️  {len(eps)} episódios < {len(models)} modelos: sistema indeterminado; "
                  f"fatores de baixa alavancagem serão mantidos em 1.0 (≈nominal).\n")
        # leverage de cada modelo (quanto $ ele aporta no total) p/ sinalizar confiança
        lev = {m: sum(A[r][i] for r in range(len(A))) for i, m in enumerate(models)}
        totlev = sum(lev.values()) or 1.0
        # Modelos que a janela não consegue separar (peso <= 10%): o fator "aprendido" é
        # dominado pelo puxão de λ para 1.0 — gravá-lo fingiria uma calibração que não houve.
        # Mantém-se em 1.0 (nominal) no --apply, em vez de escrever um número sem lastro.
        low_lev = {m for m in models if lev[m] / totlev <= 0.1}
        for m in models:
            conf = "alta" if lev[m] / totlev > 0.3 else ("média" if lev[m] / totlev > 0.1 else "BAIXA (mantido 1.0)")
            print(f"  {m:<22} fator={factors[m]:.3f}   peso nos dados={lev[m]/totlev*100:4.1f}%  confiança={conf}")
        for r, (usd, _toks) in enumerate(eps):
            est = sum(A[r][i] * factors[models[i]] for i in range(len(models)))
            err = f"{abs(est - usd) / usd * 100:.1f}%" if usd else "— (real US$0)"
            print(f"  · episódio {r+1}: estimado US${est:.2f} vs real US${usd:.2f}  (erro {err})")
        if args.apply:
            # não grava fator de baixa alavancagem: mantém 1.0 (nominal) p/ esses modelos.
            applied = {m: (1.0 if m in low_lev else factors[m]) for m in models}
            FACTORS_PATH.write_text(json.dumps(applied, indent=2))
            held = ", ".join(sorted(low_lev))
            note = f" (mantidos em 1.0: {held})" if held else ""
            print(f"\n✅ aplicado em {FACTORS_PATH}{note}")
        else:
            print("\n(rode com --apply para gravar; senão é só simulação)")
        print()
        return

    # registro de um episódio: --brl ou --usd, janela = último credit_episode ou --from/--to
    if args.usd is not None:
        real_usd = args.usd
    elif args.brl is not None:
        if not args.rate or args.rate <= 0:
            print("--rate deve ser > 0 (câmbio US$/R$)."); return
        real_usd = args.brl / args.rate
        if abs(args.rate - 5.9) < 1e-9:
            print("⚠️  usando --rate default 5.9 (US$/R$); passe o câmbio do dia p/ mais precisão.")
    else:
        print("informe --usd <X> ou --brl <Y> (com --rate, default 5.9)"); return
    if real_usd <= 0:
        print("gasto real deve ser > 0 (--usd/--brl positivos)."); return
    if args.from_ and args.to:
        a = datetime.fromisoformat(args.from_).replace(tzinfo=timezone.utc).timestamp()
        b = datetime.fromisoformat(args.to).replace(tzinfo=timezone.utc).timestamp()
    else:
        eps = credit_episodes(con)
        if not eps:
            print("sem credit_episode detectado — passe --from/--to (ISO UTC)"); return
        a, b = eps[-1]
    toks = _episode_tokens(con, a, b)
    if not toks:
        print("janela sem tokens reais — confira --from/--to"); return
    now = datetime.now(timezone.utc).isoformat()
    con.execute("INSERT INTO calibration (ts, note, real_usd, win_from, win_to, tokens_json) VALUES (?,?,?,?,?,?)",
                (now, args.note, real_usd,
                 datetime.fromtimestamp(a, timezone.utc).isoformat(),
                 datetime.fromtimestamp(b, timezone.utc).isoformat(), json.dumps(toks)))
    con.commit()
    est = sum(base_cost(m, _cost_row(t)) for m, t in toks.items())
    print(f"\n✅ episódio registrado: US${real_usd:.2f} real  vs  US${est:.2f} nominal  (modelos: {', '.join(toks)})")
    print("   rode `calibrate --solve` para recalcular os fatores (e --apply p/ gravar)\n")


# ----------------------------- CLI ----------------------------------------- #
def _watch_loop(label: str, interval: int, step) -> None:
    print(f"{label} a cada {interval}s (Ctrl-C para sair)")
    try:
        while True:
            print(f"── {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ──")
            step()
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nencerrado.")


def _fmt_dur(sec) -> str:
    sec = int(sec or 0)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def codex_report(con: sqlite3.Connection, args) -> None:
    """Uso de tokens do Codex por sessão/dia/modelo (tabela codex_usage). Codex é
    assinatura: reporta tokens e tempo ativo, não custo por token."""
    now = datetime.now(timezone.utc)
    if getattr(args, "since", ""):
        since_epoch = _since_epoch_local(args.since)
        label = f"desde {args.since}"
    else:
        delta = WINDOWS.get(args.window, WINDOWS["week"])
        since_epoch = (now - delta).timestamp()
        label = f"últimos {args.window}"
    group_params: list = []
    if args.by == "day":
        group = "substr(datetime(started_epoch + ?, 'unixepoch'), 1, 10)"  # dia no fuso local
        group_params = [_tz_offset_seconds()]
    else:
        group = {
            "session": "session_id",
            "model": "model",
            "none": "'GLOBAL'",
        }.get(args.by, "session_id")
    # Filtra por ended_epoch (último evento) e não por started_epoch: uma sessão longa
    # aberta ANTES da janela mas ainda ativa DENTRO dela produziu tokens no período e
    # não pode sumir do relatório. Ressalva: codex_usage é 1 linha/sessão com totais
    # CUMULATIVOS, então uma sessão que cruza a borda atribui todo o acumulado à janela.
    rows = con.execute(f"""
        SELECT {group} AS g,
               SUM(input_tokens), SUM(cached_input_tokens), SUM(output_tokens),
               SUM(reasoning_output_tokens), SUM(total_tokens),
               SUM(MAX(ended_epoch - started_epoch, 0)), COUNT(*)
        FROM codex_usage WHERE COALESCE(ended_epoch, started_epoch) >= ?
        GROUP BY g ORDER BY SUM(total_tokens) DESC
    """, group_params + [since_epoch]).fetchall()
    print(f"\n=== Uso do Codex — {label} — por {args.by} ===\n")
    if not rows:
        print("(sem dados nesta janela; rode: token_monitor.py ingest)\n")
        return

    def nf(x):
        return f"{int(x or 0):,}".replace(",", ".")

    hdr = (f"{'grupo':<26} {'in':>13} {'cached':>13} {'out':>10} "
           f"{'reason':>9} {'total':>13} {'tempo':>8} {'sess':>5}")
    print(hdr)
    print("-" * len(hdr))
    ti = tc = to = tr = tt = td = ns = 0
    for g, i, c, o, r, tot, dur, n in rows:
        i, c, o, r, tot, dur, n = (int(x or 0) for x in (i, c, o, r, tot, dur, n))
        print(f"{(str(g) if g else '?')[:26]:<26} {nf(i):>13} {nf(c):>13} {nf(o):>10} "
              f"{nf(r):>9} {nf(tot):>13} {_fmt_dur(dur):>8} {n:>5}")
        ti += i; tc += c; to += o; tr += r; tt += tot; td += dur; ns += n
    print("-" * len(hdr))
    print(f"{'TOTAL':<26} {nf(ti):>13} {nf(tc):>13} {nf(to):>10} "
          f"{nf(tr):>9} {nf(tt):>13} {_fmt_dur(td):>8} {ns:>5}")
    print("\nCodex é assinatura (sem custo/token); 'tempo' = ativo (1º->último evento por sessão).\n")


def status(con: sqlite3.Connection, args) -> None:
    """Resumo de um olhar: Claude + Codex (5h/semanal) + veredito do gate (both).

    Usa o MESMO caminho de leitura do gate (_claude_reading refaz se a leitura estiver
    velha; _codex_reading infere reset) e a MESMA regra de veredito — assim status e
    gate nunca discordam. Sem leitura válida => UNKNOWN (trate como PAUSE), nunca GO.
    Cada pct é comparado com guarda de None (um bucket ausente não derruba o comando)."""
    g5 = getattr(args, "max_5h", 80); gw = getattr(args, "max_week", 90)
    cl = _claude_reading(con, args)
    co = _codex_reading(args)
    print(f"\n=== Status — Claude + Codex (gate 5h<{g5}% · semanal<{gw}%) ===\n")

    pauses = []
    any_valid = False
    for rd, name, label in ((cl, "claude", "Claude"), (co, "codex", "Codex ")):
        sp, wp = rd["session_pct"], rd["week_pct"]
        if sp is None or wp is None:
            print(f"📊 {label}   (sem leitura — {rd['source']})")
            continue
        any_valid = True
        hot = sp >= g5 or wp >= gw
        if hot:
            pauses.append(name)
        flag = "  🛑" if hot else ""
        print(f"📊 {label}   5h: {_pctstr(sp)} · reset {rd['session_reset']}   |   "
              f"semanal: {_pctstr(wp)} · reset {rd['week_reset']}{flag}   [{rd['source']}]")

    if not any_valid:
        dec, icon, tail = "UNKNOWN", "⚠️ ", " — sem leitura válida (conservador: trate como PAUSE)"
    elif pauses:
        dec, icon, tail = "PAUSE", "🛑", f" — estourado: {', '.join(pauses)}"
    else:
        dec, icon, tail = "GO", "➡️ ", ""
    print(f"\n{icon} gate(both): {dec}{tail}\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Monitor de uso de tokens do Claude Code")
    sub = ap.add_subparsers(dest="cmd", required=True)
    # comandos que leem o banco herdam --no-ingest (pula a atualização prévia)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--no-ingest", action="store_true", help="não atualizar o banco antes")

    sub.add_parser("ingest", help="varre os .jsonl e popula o banco")

    rp = sub.add_parser("report", help="relatório agregado", parents=[common])
    rp.add_argument("--window", choices=list(WINDOWS), default="week")
    rp.add_argument("--since", help="data ISO (YYYY-MM-DD); sobrepõe --window")
    rp.add_argument("--by", choices=["model", "session", "project", "day", "billing", "none"], default="none")
    rp.add_argument("--model", help="filtra por PREFIXO do model (ex.: claude-haiku-4-5 casa -20251001/[1m]; claude-fable-5)")
    rp.add_argument("--session", help="filtra por prefixo de session_id (ex.: 86e5a22d)")
    rp.add_argument("--project", help="filtra por substring do projeto (ex.: GATSO ou wf_)")
    rp.add_argument("--io-only", dest="io_only", action="store_true",
                    help="só in/out + %% de output, sem cache/custo (comparável ao app do Claude)")

    lp = sub.add_parser("limits", help="lista batidas de limite", parents=[common])
    lp.add_argument("--limit", type=int, default=50)

    wp = sub.add_parser("watch", help="ingest contínuo")
    wp.add_argument("--interval", type=int, default=60)

    mp = sub.add_parser("meter", help="lê o medidor oficial /usage (Claude) — e o Codex junto, se disponível")
    mp.add_argument("--watch", action="store_true", help="loop contínuo")
    mp.add_argument("--interval", type=int, default=300)
    mp.add_argument("--no-notify", action="store_true", help="não notificar (macOS/Telegram)")
    mp.add_argument("--no-codex", action="store_true", help="medir só o Claude (não medir o Codex junto)")

    mr = sub.add_parser("meter-report", help="histórico do medidor oficial")
    mr.add_argument("--limit", type=int, default=30)

    cm = sub.add_parser("codex-meter", help="lê o rate-limit do Codex (rollouts) e grava na tabela codex_meter")
    cm.add_argument("--watch", action="store_true", help="loop contínuo (igual ao meter --watch)")
    cm.add_argument("--interval", type=int, default=300)
    cm.add_argument("--no-notify", action="store_true", help="não notificar (macOS/Telegram)")
    cm.add_argument("--json", action="store_true", help="saída JSON")
    cm.add_argument("--refresh", action="store_true",
                    help="confirma ao vivo com 1 turno MÍNIMO do codex exec, mas só se um reset já cruzou desde o rollout (custo mínimo)")
    cm.add_argument("--force", action="store_true",
                    help="com --refresh, gasta o turno sempre (mesmo sem reset cruzado)")

    cmr = sub.add_parser("codex-meter-report", help="histórico do medidor Codex (tabela codex_meter)")
    cmr.add_argument("--limit", type=int, default=30)

    cxr = sub.add_parser("codex-report", help="uso de tokens do Codex por sessão/dia/modelo (rollouts; tokens + tempo, sem custo)")
    cxr.add_argument("--by", choices=["session", "day", "model", "none"], default="session")
    cxr.add_argument("--window", default="week", help="5h | day | week | month")
    cxr.add_argument("--since", default="", help="data ISO YYYY-MM-DD (sobrepõe --window)")

    stp = sub.add_parser("status", help="resumo de um olhar: Claude + Codex (5h/semanal) + veredito do gate")
    stp.add_argument("--max-5h", dest="max_5h", type=int, default=80)
    stp.add_argument("--max-week", dest="max_week", type=int, default=90)
    # Mesmos knobs de leitura do gate (status reaproveita _claude_reading/_codex_reading):
    stp.add_argument("--max-age", dest="max_age", type=int, default=300,
                     help="segundos: leitura mais velha que isto dispara medida ao vivo (default 300)")
    stp.add_argument("--refresh", action="store_true", help="força leitura ao vivo do /usage agora")
    stp.add_argument("--no-notify", action="store_true", help="não notificar ao refazer a leitura do medidor")

    gp = sub.add_parser("gate", help="veredito GO/PAUSE de rate limit (exit 0/10/2) p/ runners")
    gp.add_argument("--provider", choices=["claude", "codex", "both"], default="claude",
                    help="qual medidor gatear (default claude). 'both' = PAUSE se Claude OU Codex estourar")
    gp.add_argument("--max-5h", dest="max_5h", type=int, default=80, help="teto da janela 5h em %% (default 80)")
    gp.add_argument("--max-week", dest="max_week", type=int, default=90, help="teto semanal em %% (default 90)")
    gp.add_argument("--max-age", dest="max_age", type=int, default=300,
                    help="segundos: leitura mais velha que isto dispara medida ao vivo (default 300)")
    gp.add_argument("--refresh", action="store_true", help="força leitura ao vivo do /usage agora")
    gp.add_argument("--json", action="store_true", help="saída JSON em vez de texto")
    gp.add_argument("--no-notify", action="store_true", help="não notificar ao refazer a leitura")

    cp = sub.add_parser("calibrate", help="aprende fator de preço por modelo a partir de gastos reais de crédito",
                        parents=[common])
    cp.add_argument("--brl", type=float, help="gasto real em R$ (converte por --rate)")
    cp.add_argument("--usd", type=float, help="gasto real em US$ (tem precedência sobre --brl)")
    cp.add_argument("--rate", type=float, default=5.9, help="câmbio US$/R$ (default 5.9)")
    cp.add_argument("--from", dest="from_", help="início da janela (ISO UTC); default = último credit_episode")
    cp.add_argument("--to", help="fim da janela (ISO UTC)")
    cp.add_argument("--note", help="rótulo do episódio")
    cp.add_argument("--list", action="store_true", help="lista episódios registrados")
    cp.add_argument("--solve", action="store_true", help="resolve os fatores por modelo (mínimos quadrados)")
    cp.add_argument("--apply", action="store_true", help="grava os fatores resolvidos (com --solve)")

    bp = sub.add_parser("bursts", help="detalha clusters de atividade (gatilho, billing, modelos, cap)",
                        parents=[common])
    bp.add_argument("--session", help="prefixo do session_id (vazio = todas)")
    bp.add_argument("--gap", type=int, default=1200, help="segundos de inatividade que separam bursts (default 1200=20min)")
    bp.add_argument("--utc", action="store_true", help="exibir em UTC (default: horário local METER_TZ)")

    args = ap.parse_args()
    con = db_connect()

    # ingest prévio centralizado: report/limits/bursts sempre; calibrate só ao
    # registrar episódio (--list/--solve não dependem de dados novos)
    wants_ingest = args.cmd in ("report", "limits", "bursts") or (
        args.cmd == "calibrate" and not (args.list or args.solve))
    if wants_ingest and not args.no_ingest:
        ingest(con, verbose=False)

    if args.cmd == "ingest":
        ingest(con)
    elif args.cmd == "report":
        report(con, args)
    elif args.cmd == "limits":
        limits(con, args)
    elif args.cmd == "watch":
        _watch_loop("watch: ingest", args.interval, lambda: ingest(con))
    elif args.cmd == "meter":
        notify = not args.no_notify
        with_codex = (not args.no_codex) and CODEX_SESSIONS_DIR.exists()
        # no watch, confirma o Codex ao vivo quando um reset cruza (turno mínimo, 1x por
        # snapshot); numa leitura avulsa fica só leitura (use `codex-meter --refresh`).
        codex_tick = (_make_codex_tick(con, notify, auto_refresh=args.watch)
                      if with_codex else None)
        def _meter_tick():
            ok = meter_once(con, notify=notify)
            if codex_tick:
                codex_tick()
            return ok
        if args.watch:
            _watch_loop("meter: /usage" + (" + codex" if with_codex else ""), args.interval, _meter_tick)
        else:
            _meter_tick()
    elif args.cmd == "meter-report":
        meter_report(con, args)
    elif args.cmd == "codex-meter":
        notify = not args.no_notify
        if args.watch:
            _watch_loop("codex-meter", args.interval,
                        _make_codex_tick(con, notify, auto_refresh=True))
        else:
            if getattr(args, "refresh", False):
                m0 = read_codex_meter()
                need, why = _codex_refresh_needed(m0, getattr(args, "force", False))
                if need:
                    print(f"… confirmando ao vivo (codex exec, turno mínimo): {why}")
                    st, _ = codex_live_refresh()
                    print(f"  codex exec: {st}")
                else:
                    age = int(datetime.now(timezone.utc).timestamp() - m0["ts_epoch"]) if m0 and m0.get("ts_epoch") else 0
                    print(f"  leitura ainda válida (rollout {age}s, nenhum reset cruzado desde então) — sem gasto de cota; use --force para forçar")
            codex_meter_once(con, notify=notify, as_json=args.json)
    elif args.cmd == "codex-meter-report":
        codex_meter_report(con, args)
    elif args.cmd == "codex-report":
        codex_report(con, args)
    elif args.cmd == "status":
        status(con, args)
    elif args.cmd == "gate":
        code = gate(con, args)
        con.close()
        sys.exit(code)
    elif args.cmd == "bursts":
        bursts_report(con, args)
    elif args.cmd == "calibrate":
        calibrate(con, args)
    con.close()


if __name__ == "__main__":
    main()
