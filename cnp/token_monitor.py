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
  python3 token_monitor.py ingest            # varre os .jsonl + watch.log + recalcula billing
  python3 token_monitor.py report            # default: últimos 7 dias (global)
  python3 token_monitor.py report --window 5h
  python3 token_monitor.py report --since 2026-06-01 --by billing   # assinatura × crédito
  python3 token_monitor.py report --by model --model claude-fable-5 --session 86e5a22d
  python3 token_monitor.py report --by model --io-only   # só in/out+%out (comparável ao app do Claude)
  python3 token_monitor.py limits            # episódios de batida de limite
  python3 token_monitor.py bursts --session 86e5a22d  # timeline detalhada (gatilho/billing/cap)
  python3 token_monitor.py watch             # ingest contínuo

Uso — medidor oficial (porte do claude-limit-watch.sh, custo zero):
  python3 token_monitor.py meter             # 1 leitura de /usage (grava 5h%/semanal%/eventos)
  python3 token_monitor.py meter --watch --interval 300   # loop; alerta cap_5h/credits_started/reset/drop
  python3 token_monitor.py meter-report      # histórico do medidor
  python3 token_monitor.py gate              # veredito GO/PAUSE p/ runners (exit 0/10/2); usa cache, refaz se velho
  python3 token_monitor.py gate --json --max-5h 80 --max-week 90   # decisão estruturada p/ automação

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
Env úteis: METER_TZ, CREDIT_PCT, RESET_TOLERANCE, DROP_THRESHOLD, FACTORS_PATH.
"""
from __future__ import annotations

import argparse
import collections
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
# telegram.env do próprio limit-watch (mesmo arquivo, reaproveitado)
TG_ENV_FILE = Path(os.environ.get("TG_ENV_FILE", str(Path.home() / ".claude" / "limit-watch" / "telegram.env")))

# Preço-BASE nominal por 1M tokens (USD): input / output / cache_read / cache_write(5m).
# Estes são os valores de referência; o custo final = base × FATOR_DO_MODELO, onde o
# fator é APRENDIDO dos seus gastos reais de crédito (comando `calibrate`). Modelo sem
# dado real fica com fator 1.0 (nominal) — ver `calibrate --solve`.
PRICING = {
    "claude-fable-5":         {"in": 10.0, "out": 50.0, "cache_read": 1.00, "cache_write": 12.50},
    "claude-opus-4-8":        {"in": 5.0,  "out": 25.0, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-7":        {"in": 5.0,  "out": 25.0, "cache_read": 0.50, "cache_write": 6.25},
    "claude-sonnet-4-6":      {"in": 3.0,  "out": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "claude-haiku-4-5":       {"in": 1.0,  "out": 5.0,  "cache_read": 0.10, "cache_write": 1.25},
    "_default":               {"in": 3.0,  "out": 15.0, "cache_read": 0.30, "cache_write": 3.75},
}


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
    con.commit()
    return con


def parse_ts(ts: str) -> float:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


# --------------------------- Ingest ---------------------------------------- #
def ingest(con: sqlite3.Connection, verbose: bool = True) -> tuple[int, int]:
    new_usage = new_limits = 0
    for jf in PROJECTS_DIR.rglob("*.jsonl"):
        project = jf.parent.name
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
    if verbose:
        extra = f" | +{n_log} leituras do watch.log" if n_log else ""
        print(f"ingest: +{new_usage} mensagens de uso, +{new_limits} batidas de limite "
              f"| {n_credits} msgs ≈créditos{extra}")
    return new_usage, new_limits


# Linha do watch.log: "YYYY-MM-DD HH:MM:SS  📊 5h: N% usado · reset R1  |  semanal: M% usado · reset R2"
_WATCHLOG_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+📊 5h:\s*(\d+)% usado · reset (.+?)\s+\|\s+"
    r"semanal:\s*(\d+)% usado · reset (.+?)\s*$"
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


def base_cost(model: str | None, r: dict) -> float:
    """Custo NOMINAL (sem o fator de calibração)."""
    p = PRICING.get(_norm_model(model)) or PRICING["_default"]
    return (
        r["in"] / 1e6 * p["in"]
        + r["out"] / 1e6 * p["out"]
        + r["cread"] / 1e6 * p["cache_read"]
        + r["cwrite"] / 1e6 * p["cache_write"]
    )


def cost(model: str | None, r: dict) -> float:
    """Custo calibrado = base × fator do modelo (1.0 se não calibrado)."""
    factor = FACTORS.get(model, FACTORS.get(_norm_model(model), 1.0))
    return base_cost(model, r) * factor


def _cost_row(t: dict) -> dict:
    """Adapta {in,out,cr,cw} (transcripts/episódios) para o shape de base_cost()."""
    return {"in": t.get("in", 0), "out": t.get("out", 0),
            "cread": t.get("cr", 0), "cwrite": t.get("cw", 0)}


def fmt(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def report(con: sqlite3.Connection, args) -> None:
    now = datetime.now(timezone.utc)
    if args.since:
        since_epoch = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc).timestamp()
        label = f"desde {args.since}"
    else:
        delta = WINDOWS.get(args.window, WINDOWS["week"])
        since_epoch = (now - delta).timestamp()
        label = f"últimos {args.window}"

    group = {
        "model": "model",
        "session": "session_id",
        "project": "project",
        "day": "substr(ts,1,10)",
        "billing": "billing_source",
        "none": "'GLOBAL'",
    }.get(args.by, "'GLOBAL'")

    # Filtros opcionais (--model exato, --session prefixo, --project substring).
    # As mesmas colunas existem em `usage` e `limits`, então o WHERE serve aos dois.
    where = ["ts_epoch >= ?"]
    params: list = [since_epoch]
    for name, val, clause, param in (
        ("model", args.model, "model = ?", args.model),
        ("session", args.session, "session_id LIKE ?", f"{args.session}%"),
        ("project", args.project, "project LIKE ?", f"%{args.project}%"),
    ):
        if val:
            where.append(clause); params.append(param)
            label += f" [{name}={val}]"
    where_sql = " AND ".join(where)

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
    subrows = con.execute(sql, params).fetchall()

    print(f"\n=== Uso de tokens — {label} — por {args.by} ===\n")
    if not subrows:
        print("(sem dados nesta janela)\n")
        return

    # Agrega sub-linhas (grupo × modelo) em grupos de exibição, somando o custo
    # por modelo. agg[grupo] = {in,out,cread,cwrite,msgs,usd}
    agg: dict[str, dict] = {}
    for g, model, i, o, cr, cw, ws, wf, n in subrows:
        key = (str(g)[:32]) if g else "?"
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
            print(f"{g_disp:<34} {fmt(d['in']):>12} {fmt(d['out']):>12} {d['out'] / tot_out * 100:>6.1f}% {d['msgs']:>6}")
            for k in tot:
                tot[k] += d[k]
        print("-" * len(header))
        print(f"{'TOTAL':<34} {fmt(tot['in']):>12} {fmt(tot['out']):>12} {'100.0%':>7} {tot['msgs']:>6}")
        print(f"\nTotal in+out: {fmt(tot['in'] + tot['out'])}  (o app mostra estes números, não o cache)\n")
        return

    ordered = sorted(agg.items(), key=lambda kv: kv[1]["in"] + kv[1]["out"] + kv[1]["cread"] + kv[1]["cwrite"], reverse=True)

    header = f"{'grupo':<34} {'in':>12} {'out':>12} {'cache_r':>13} {'cache_w':>12} {'msgs':>6} {'~USD':>9}"
    print(header)
    print("-" * len(header))
    tot = {"in": 0, "out": 0, "cread": 0, "cwrite": 0, "msgs": 0, "usd": 0.0}
    for g_disp, d in ordered:
        print(f"{g_disp:<34} {fmt(d['in']):>12} {fmt(d['out']):>12} {fmt(d['cread']):>13} {fmt(d['cwrite']):>12} {d['msgs']:>6} {d['usd']:>9.2f}")
        for k in tot:
            tot[k] += d[k]
    print("-" * len(header))
    print(f"{'TOTAL':<34} {fmt(tot['in']):>12} {fmt(tot['out']):>12} "
          f"{fmt(tot['cread']):>13} {fmt(tot['cwrite']):>12} {tot['msgs']:>6} {tot['usd']:>9.2f}")
    grand = tot["in"] + tot["out"] + tot["cread"] + tot["cwrite"]
    print(f"\nTotal de tokens (todos os tipos): {fmt(grand)}")
    nlim = con.execute(f"SELECT COUNT(*) FROM limits WHERE {where_sql}", params).fetchone()[0]
    print(f"Batidas de limite nesta janela: {nlim}\n")


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


def _notify(title: str, msg: str, sound: str = "Glass", tg: bool = True) -> None:
    print(f"🔔 {title} — {msg}")
    if sys.platform == "darwin" and shutil.which("osascript"):
        safe_t = title.replace('"', '\\"'); safe_m = msg.replace('"', '\\"')
        subprocess.run(["osascript", "-e",
                        f'display notification "{safe_m}" with title "{safe_t}" sound name "{sound}"'],
                       capture_output=True)
    if tg:
        tok, chat = _load_tg()
        if tok and chat and shutil.which("curl"):
            for cid in (c.strip() for c in chat.split(",") if c.strip()):
                subprocess.run(["curl", "-s", "-m", "10", "-o", "/dev/null",
                                "--data-urlencode", f"chat_id={cid}",
                                "--data-urlencode", f"text=🤖 {title}\n{msg}",
                                f"https://api.telegram.org/bot{tok}/sendMessage"],
                               capture_output=True)


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


def _parse_line(usage: str, prefix: str) -> tuple[int | None, str | None]:
    line = next((ln for ln in usage.splitlines() if prefix.lower() in ln.lower()), None)
    if not line:
        return None, None
    mp = re.search(r"(\d+)%\s*used", line, re.IGNORECASE)
    mr = re.search(r"resets\s+(.+)$", line, re.IGNORECASE)
    pct = int(mp.group(1)) if mp else None
    reset = mr.group(1).strip() if mr else None
    return pct, reset


def _reset_to_epoch(s: str | None) -> float | None:
    """'Jun 10 at 3pm (America/Sao_Paulo)' -> epoch (na METER_TZ)."""
    if not s or ZoneInfo is None:
        return None
    s = re.sub(r"\s*\(.*\)$", "", s).strip()              # tira "(timezone)"
    m = re.match(r"([A-Za-z]{3})\s+(\d{1,2})\s+at\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)", s, re.IGNORECASE)
    if not m:
        return None
    mon, day, hh, mm, ap = m.groups()
    mon_n = _MONTHS.get(mon[:3].title())
    if not mon_n:
        return None
    hh = int(hh) % 12 + (12 if ap.lower() == "pm" else 0)
    mm = int(mm) if mm else 0
    try:
        dt = datetime(datetime.now(timezone.utc).year, mon_n, int(day), hh, mm, tzinfo=ZoneInfo(METER_TZ))
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
    w_pct, w_reset = _parse_line(usage, "Current week (all")
    now = datetime.now(timezone.utc)
    print(f"📊 5h: {s_pct}% usado · reset {s_reset}  |  semanal: {w_pct}% usado · reset {w_reset}")

    # estado anterior p/ detectar reset (horário avançou) ou queda (% caiu)
    prev = con.execute(
        "SELECT session_pct, session_reset_epoch, week_pct, week_reset_epoch FROM meter ORDER BY ts_epoch DESC LIMIT 1"
    ).fetchone()
    s_re = _reset_to_epoch(s_reset)
    w_re = _reset_to_epoch(w_reset)
    events = []
    if prev:
        p_spct, p_sre, p_wpct, p_wre = prev
        for key, lbl, sound, pct, p_pct, rep, p_rep, reset_txt in (
            ("5h", "5h", "Ping", s_pct, p_spct, s_re, p_sre, s_reset),
            ("week", "SEMANAL", "Submarine", w_pct, p_wpct, w_re, p_wre, w_reset),
        ):
            if rep and p_rep and rep - p_rep > RESET_TOLERANCE:
                events.append(f"reset_{key}")
                _notify(f"Claude Code: {lbl} resetou ✅", f"Nova janela. Próximo reset: {reset_txt}", sound, notify)
            elif pct is not None and p_pct is not None and pct < p_pct - DROP_THRESHOLD:
                events.append(f"drop_{key}")
                _notify(f"Claude Code: cota {lbl} liberou ⬇️", f"Uso caiu de {p_pct}% para {pct}%", sound, notify)

        # (1) janela 5h ATINGIU 100% (transição <100 -> 100)
        if s_pct is not None and s_pct >= CREDIT_PCT and p_spct is not None and p_spct < CREDIT_PCT:
            events.append("cap_5h")
            _notify("Claude Code: 5h em 100% 🚫", "Janela capada. A partir daqui, todo uso é CRÉDITO (se o excedente estiver ligado).", "Sosumi", notify)

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
            _notify("Claude Code: CRÉDITOS iniciados 💳", f"{tok:,} tokens de output produzidos após o cap das 5h.", "Glass", notify)

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
    print(f"{'quando (UTC)':<22}{'5h':>5}{'  reset 5h':<26}{'sem':>5}{'  reset semanal':<24}{'  evento'}")
    print("-" * 100)
    for ts, sp, sr, wp, wr, ev in rows:
        print(f"{ts[:19]:<22}{(str(sp)+'%'):>5}  {(sr or '?')[:22]:<24}{(str(wp)+'%'):>5}  {(wr or '?')[:20]:<22}{'  '+ev if ev else ''}")
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


def gate(con: sqlite3.Connection, args) -> int:
    """Veredito de rate limit. Retorna o exit code (GO/PAUSE/UNKNOWN)."""
    now = datetime.now(timezone.utc).timestamp()
    row = _latest_meter(con)
    age = (now - row[0]) if row else None
    stale = row is None or age > args.max_age
    refreshed = False
    if args.refresh or stale:
        # leitura velha/ausente → busca ao vivo (custo zero de cota) e relê o banco
        meter_once(con, notify=not args.no_notify)
        row = _latest_meter(con)
        age = (now - row[0]) if row else None
        refreshed = True

    if not row or row[1] is None or row[3] is None:
        s_pct = w_pct = s_reset = w_reset = None
        reasons = ["sem leitura válida do medidor"]
        decision, code = "UNKNOWN", GATE_UNKNOWN
    else:
        _, s_pct, s_reset, w_pct, w_reset = row
        reasons = []
        if s_pct >= args.max_5h:
            reasons.append(f"5h em {s_pct}% (>= {args.max_5h}%)")
        if w_pct >= args.max_week:
            reasons.append(f"semanal em {w_pct}% (>= {args.max_week}%)")
        decision = "GO" if not reasons else "PAUSE"
        code = GATE_GO if not reasons else GATE_PAUSE

    # Tudo mastigado: a própria ferramenta decide, explica e já entrega o bloco de
    # pausa pronto pra colar. A SKILL só executa e segue o texto literalmente.
    motivo = "; ".join(reasons) if reasons else ""
    if decision == "GO":
        advice = "Pode iniciar a próxima tarefa. Rode o gate de novo após 1–2 tarefas."
    elif decision == "PAUSE":
        advice = ("Pare. Não inicie novas tarefas. Faça commit do que está validado "
                  "e responda no formato de pausa abaixo (preencha <N> e as listas).")
    else:
        advice = ("Sem leitura válida do medidor — trate como PAUSE (conservador). "
                  "Rode `token_monitor.py meter` para forçar uma leitura ao vivo.")
    # Cabeçalho de pausa pronto (relevante em PAUSE/UNKNOWN)
    pause_header = (
        "pausado: <N> tarefas restantes\n"
        f"motivo: {motivo or 'medidor indisponível'}\n"
        f"reset: {s_reset or '?'}\n"
        f"semanal: {w_pct if w_pct is not None else '?'}%"
    )
    verdict = {"decision": decision, "reasons": reasons, "advice": advice,
               "pause_header": None if decision == "GO" else pause_header,
               "session_pct": s_pct, "week_pct": w_pct, "session_reset": s_reset,
               "week_reset": w_reset, "age_seconds": int(age) if age is not None else None,
               "refreshed": refreshed, "max_5h": args.max_5h, "max_week": args.max_week}

    if args.json:
        print(json.dumps(verdict, ensure_ascii=False))
        return code

    src = "ao vivo" if refreshed else (f"cache {verdict['age_seconds']}s" if verdict["age_seconds"] is not None else "—")
    print(f"📊 5h: {s_pct}% · reset {s_reset}  |  semanal: {w_pct}% · reset {w_reset}  ({src})")
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
    """Soma tokens por modelo numa janela [a,b] (epoch). {model: {in,out,cr,cw}}."""
    out = {}
    for m, i, o, cr, cw in con.execute(
        f"SELECT model, SUM(input_tokens), SUM(output_tokens), SUM(cache_read), SUM(cache_write) "
        f"FROM usage WHERE ts_epoch > ? AND ts_epoch <= ? AND {REAL_MODEL_SQL} GROUP BY model",
        (a, b),
    ):
        out[m] = {"in": i or 0, "out": o or 0, "cr": cr or 0, "cw": cw or 0}
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
        eps = con.execute("SELECT real_usd, tokens_json FROM calibration").fetchall()
        if not eps:
            print("(sem episódios — registre com: calibrate --brl <valor> [--from .. --to ..])"); return
        models = sorted({m for _, tj in eps for m in json.loads(tj)})
        A, b = [], []
        for usd, tj in eps:
            toks = json.loads(tj)
            row = [base_cost(m, _cost_row(toks.get(m, {}))) for m in models]
            A.append(row); b.append(usd)
        lam = max(1e-6, 0.02 * max((A[r][c] for r in range(len(A)) for c in range(len(models))), default=1.0))
        f = _solve_ridge(A, b, len(models), lam)
        factors = {m: round(max(0.0, f[i]), 4) for i, m in enumerate(models)}
        # diagnóstico: erro por episódio com os fatores novos
        print(f"\n=== Fatores resolvidos ({len(eps)} episódios, {len(models)} modelos, ridge λ={lam:.3g}) ===\n")
        # leverage de cada modelo (quanto $ ele aporta no total) p/ sinalizar confiança
        lev = {m: sum(A[r][i] for r in range(len(A))) for i, m in enumerate(models)}
        totlev = sum(lev.values()) or 1.0
        for m in models:
            conf = "alta" if lev[m] / totlev > 0.3 else ("média" if lev[m] / totlev > 0.1 else "BAIXA (≈nominal)")
            print(f"  {m:<22} fator={factors[m]:.3f}   peso nos dados={lev[m]/totlev*100:4.1f}%  confiança={conf}")
        for r, (usd, tj) in enumerate(eps):
            est = sum(A[r][i] * factors[models[i]] for i in range(len(models)))
            print(f"  · episódio {r+1}: estimado US${est:.2f} vs real US${usd:.2f}  (erro {abs(est-usd)/usd*100:.1f}%)")
        if args.apply:
            FACTORS_PATH.write_text(json.dumps(factors, indent=2))
            print(f"\n✅ aplicado em {FACTORS_PATH}")
        else:
            print("\n(rode com --apply para gravar; senão é só simulação)")
        print()
        return

    # registro de um episódio: --brl ou --usd, janela = último credit_episode ou --from/--to
    real_usd = args.usd if args.usd is not None else (args.brl / args.rate if args.brl is not None else None)
    if real_usd is None:
        print("informe --usd <X> ou --brl <Y> (com --rate, default 5.9)"); return
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
            step()
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nencerrado.")


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
    rp.add_argument("--model", help="filtra por model exato (ex.: claude-fable-5)")
    rp.add_argument("--session", help="filtra por prefixo de session_id (ex.: 86e5a22d)")
    rp.add_argument("--project", help="filtra por substring do projeto (ex.: GATSO ou wf_)")
    rp.add_argument("--io-only", dest="io_only", action="store_true",
                    help="só in/out + %% de output, sem cache/custo (comparável ao app do Claude)")

    lp = sub.add_parser("limits", help="lista batidas de limite", parents=[common])
    lp.add_argument("--limit", type=int, default=50)

    wp = sub.add_parser("watch", help="ingest contínuo")
    wp.add_argument("--interval", type=int, default=60)

    mp = sub.add_parser("meter", help="lê o medidor oficial /usage (custo zero) e grava na tabela meter")
    mp.add_argument("--watch", action="store_true", help="loop contínuo")
    mp.add_argument("--interval", type=int, default=300)
    mp.add_argument("--no-notify", action="store_true", help="não notificar (macOS/Telegram)")

    mr = sub.add_parser("meter-report", help="histórico do medidor oficial")
    mr.add_argument("--limit", type=int, default=30)

    gp = sub.add_parser("gate", help="veredito GO/PAUSE de rate limit (exit 0/10/2) p/ runners")
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
        if args.watch:
            _watch_loop("meter: /usage", args.interval, lambda: meter_once(con, notify=notify))
        else:
            meter_once(con, notify=notify)
    elif args.cmd == "meter-report":
        meter_report(con, args)
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
