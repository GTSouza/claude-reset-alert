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
  python3 token_monitor.py limits            # episódios de batida de limite
  python3 token_monitor.py bursts --session 86e5a22d  # timeline detalhada (gatilho/billing/cap)
  python3 token_monitor.py watch             # ingest contínuo

Uso — medidor oficial (porte do claude-limit-watch.sh, custo zero):
  python3 token_monitor.py meter             # 1 leitura de /usage (grava 5h%/semanal%/eventos)
  python3 token_monitor.py meter --watch --interval 300   # loop; alerta cap_5h/credits_started/reset/drop
  python3 token_monitor.py meter-report      # histórico do medidor

Uso — calibração de custo (aprende fator por modelo dos gastos REAIS):
  python3 token_monitor.py calibrate --brl 47.85          # registra episódio (janela=último crédito)
  python3 token_monitor.py calibrate --solve              # resolve fatores por modelo (simula)
  python3 token_monitor.py calibrate --solve --apply      # grava em pricing_factors.json
  python3 token_monitor.py calibrate --list

Eixos (--by): model | session | project | day | billing | none(global)
Janelas (--window): 5h | day | week | month
Custo: base PRICING × fator do modelo (calibrado); ~USD. Modelo sem dado real = fator 1.0.
Billing: token real enquanto medidor 5h==100% (após cap confirmado) = crédito; senão assinatura.
Env úteis: METER_TZ, CREDIT_PCT, METER_TOL, RESET_TOLERANCE, DROP_THRESHOLD, FACTORS_PATH.
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
    "claude-fable-5[1m]":     {"in": 10.0, "out": 50.0, "cache_read": 1.00, "cache_write": 12.50},
    "claude-opus-4-8":        {"in": 5.0,  "out": 25.0, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-8[1m]":    {"in": 5.0,  "out": 25.0, "cache_read": 0.50, "cache_write": 6.25},
    "claude-sonnet-4-6":      {"in": 3.0,  "out": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "claude-haiku-4-5":       {"in": 1.0,  "out": 5.0,  "cache_read": 0.10, "cache_write": 1.25},
    "_default":               {"in": 3.0,  "out": 15.0, "cache_read": 0.30, "cache_write": 3.75},
}

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
        for line in _iter_lines(jf):
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
                try:
                    con.execute(
                        "INSERT OR IGNORE INTO limits VALUES (?,?,?,?,?,?,?)",
                        (uuid, ts, ts_epoch, session_id, project, model, text[:300]),
                    )
                    if con.total_changes:
                        new_limits += con.total_changes and 1
                except sqlite3.IntegrityError:
                    pass

            # --- usage ---
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            sv = usage.get("server_tool_use") or {}
            row = (
                uuid, ts, ts_epoch, session_id, project, o.get("gitBranch"),
                model,
                int(usage.get("input_tokens", 0) or 0),
                int(usage.get("output_tokens", 0) or 0),
                int(usage.get("cache_read_input_tokens", 0) or 0),
                int(usage.get("cache_creation_input_tokens", 0) or 0),
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
            new_usage += cur.rowcount if cur.rowcount > 0 else 0
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
    via PRIMARY KEY (ts). Convive com as leituras feitas pelo subcomando `meter`.
    """
    if not WATCHLOG_PATH.is_file() or ZoneInfo is None:
        return 0
    tz = ZoneInfo(METER_TZ)
    n = 0
    for line in _iter_lines(WATCHLOG_PATH):
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
        n += cur.rowcount if cur.rowcount > 0 else 0
    con.commit()
    return n


# Limiar de % do medidor 5h a partir do qual consideramos a janela CAPADA.
CREDIT_PCT = int(os.environ.get("CREDIT_PCT", "100"))
# Tolerância (s) entre a msg e a leitura de medidor mais próxima.
METER_TOL = int(os.environ.get("METER_TOL", "600"))


def compute_billing(con: sqlite3.Connection) -> int:
    """Marca usage.billing_source via MEDIDOR OFICIAL (tabela meter), não por banners.

    Regra robusta: um token é 'credits' se a leitura de medidor 5h mais próxima (até
    METER_TOL) mostra a janela capada (>= CREDIT_PCT). Com o medidor a 100%, qualquer
    token real só existe porque o excedente estava ligado. Sem medidor próximo no
    tempo → 'subscription' (não há como afirmar crédito).
    """
    con.execute("UPDATE usage SET billing_source='subscription'")
    intervals = credit_episodes(con)
    for a, b in intervals:
        con.execute(
            "UPDATE usage SET billing_source='credits' WHERE ts_epoch > ? AND ts_epoch <= ?",
            (a, b),
        )
    n = con.execute("SELECT COUNT(*) FROM usage WHERE billing_source='credits'").fetchone()[0]
    con.commit()
    return n


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
    episodes = []
    i, n = 0, len(meters)
    while i < n:
        if meters[i][1] >= CREDIT_PCT:
            cap_start = meters[i][0]                       # 1ª leitura 100% da sequência
            j = i
            while j < n and meters[j][1] >= CREDIT_PCT:    # fim do streak de 100%
                j += 1
            # Termina na ÚLTIMA leitura 100% confirmada — NÃO na 1ª <100%. O reset real
            # cai dentro do gap de poll (last_100 -> first_<100); ir até first_<100
            # pescaria trabalho PÓS-reset (assinatura nova). Conservador e correto.
            cap_end = meters[j - 1][0]
            # só é crédito se houve token real ESTRITAMENTE após o 100% confirmado
            tok = con.execute(
                "SELECT COALESCE(SUM(output_tokens), 0) FROM usage "
                "WHERE ts_epoch > ? AND ts_epoch <= ? AND model LIKE 'claude%'",
                (cap_start, cap_end),
            ).fetchone()[0]
            if tok > 0:
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


def base_cost(model: str, r: dict) -> float:
    """Custo NOMINAL (sem o fator de calibração)."""
    p = PRICING.get(model) or PRICING["_default"]
    return (
        r["in"] / 1e6 * p["in"]
        + r["out"] / 1e6 * p["out"]
        + r["cread"] / 1e6 * p["cache_read"]
        + r["cwrite"] / 1e6 * p["cache_write"]
    )


def cost(model: str, r: dict) -> float:
    """Custo calibrado = base × fator do modelo (1.0 se não calibrado)."""
    return base_cost(model, r) * FACTORS.get(model, 1.0)


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
    if getattr(args, "model", None):
        where.append("model = ?"); params.append(args.model)
    if getattr(args, "session", None):
        where.append("session_id LIKE ?"); params.append(args.session + "%")
    if getattr(args, "project", None):
        where.append("project LIKE ?"); params.append("%" + args.project + "%")
    where_sql = " AND ".join(where)
    filt = "".join(
        f" [{k}={v}]" for k, v in (
            ("model", getattr(args, "model", None)),
            ("session", getattr(args, "session", None)),
            ("project", getattr(args, "project", None)),
        ) if v
    )
    label += filt

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
        d["usd"] += cost(model or "_default", {"in": i, "out": o, "cread": cr, "cwrite": cw})
        d["in"] += i; d["out"] += o; d["cread"] += cr; d["cwrite"] += cw; d["msgs"] += n

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
        if s_re and p_sre and s_re - p_sre > RESET_TOLERANCE:
            events.append("reset_5h"); _notify("Claude Code: 5h resetou ✅", f"Nova janela. Próximo reset: {s_reset}", "Ping", notify)
        elif s_pct is not None and p_spct is not None and s_pct < p_spct - DROP_THRESHOLD:
            events.append("drop_5h"); _notify("Claude Code: cota 5h liberou ⬇️", f"Uso caiu de {p_spct}% para {s_pct}%", "Ping", notify)
        if w_re and p_wre and w_re - p_wre > RESET_TOLERANCE:
            events.append("reset_week"); _notify("Claude Code: SEMANAL resetou ✅", f"Nova janela. Próximo reset: {w_reset}", "Submarine", notify)
        elif w_pct is not None and p_wpct is not None and w_pct < p_wpct - DROP_THRESHOLD:
            events.append("drop_week"); _notify("Claude Code: cota SEMANAL liberou ⬇️", f"Uso caiu de {p_wpct}% para {w_pct}%", "Submarine", notify)

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
        tok = con.execute(
            "SELECT COALESCE(SUM(output_tokens),0) FROM usage "
            "WHERE ts_epoch > ? AND ts_epoch <= ? AND model LIKE 'claude%'",
            (cap_start, now.timestamp()),
        ).fetchone()[0]
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
    billing = dict(con.execute("SELECT uuid, billing_source FROM usage"))

    msgs, triggers, banners = [], [], []
    sess = args.session
    for jf in PROJECTS_DIR.rglob("*.jsonl"):
        try:
            fh = open(jf, errors="replace")
        except OSError:
            continue
        for line in fh:
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
                u = m.get("usage") or {}
                txt = _join_text(m.get("content"))
                is_cap = bool(LIMIT_PATTERNS.search(txt)) if txt else False
                has_wake = any(isinstance(b, dict) and b.get("type") == "tool_use"
                               and b.get("name") == "ScheduleWakeup"
                               for b in (m.get("content") or []))
                msgs.append({
                    "te": te, "uuid": o.get("uuid"), "model": m.get("model"),
                    "in": int(u.get("input_tokens", 0) or 0), "out": int(u.get("output_tokens", 0) or 0),
                    "cr": int(u.get("cache_read_input_tokens", 0) or 0),
                    "cw": int(u.get("cache_creation_input_tokens", 0) or 0),
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

    print(f"\n=== BURSTS — session={sess or 'TODAS'} — gap>{gap // 60}min — horário {tzname} ===\n")
    for i, g in enumerate(groups, 1):
        a, z = g[0]["te"], g[-1]["te"]
        dur = int((z - a) / 60)
        real = [x for x in g if x["model"] and x["model"].startswith("claude")]
        synth = len(g) - len(real)
        side = sum(1 for x in g if x["side"])
        models = collections.Counter(x["model"] for x in real)
        ti = sum(x["in"] for x in g); to = sum(x["out"] for x in g)
        tcr = sum(x["cr"] for x in g); tcw = sum(x["cw"] for x in g)
        def _usd(x):
            return cost(x["model"] or "_default", {"in": x["in"], "out": x["out"], "cread": x["cr"], "cwrite": x["cw"]})
        usd = sum(_usd(x) for x in g)
        cred_msgs = [x for x in real if billing.get(x["uuid"]) == "credits"]
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
        "SELECT model, SUM(input_tokens), SUM(output_tokens), SUM(cache_read), SUM(cache_write) "
        "FROM usage WHERE ts_epoch > ? AND ts_epoch <= ? AND model LIKE 'claude%' GROUP BY model",
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
            row = [base_cost(m, {"in": toks.get(m, {}).get("in", 0), "out": toks.get(m, {}).get("out", 0),
                                 "cread": toks.get(m, {}).get("cr", 0), "cwrite": toks.get(m, {}).get("cw", 0)})
                   for m in models]
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
    est = sum(base_cost(m, {"in": t["in"], "out": t["out"], "cread": t["cr"], "cwrite": t["cw"]}) for m, t in toks.items())
    print(f"\n✅ episódio registrado: US${real_usd:.2f} real  vs  US${est:.2f} nominal  (modelos: {', '.join(toks)})")
    print("   rode `calibrate --solve` para recalcular os fatores (e --apply p/ gravar)\n")


# ----------------------------- CLI ----------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="Monitor de uso de tokens do Claude Code")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ingest", help="varre os .jsonl e popula o banco")

    rp = sub.add_parser("report", help="relatório agregado")
    rp.add_argument("--window", choices=list(WINDOWS), default="week")
    rp.add_argument("--since", help="data ISO (YYYY-MM-DD); sobrepõe --window")
    rp.add_argument("--by", choices=["model", "session", "project", "day", "billing", "none"], default="none")
    rp.add_argument("--model", help="filtra por model exato (ex.: claude-fable-5)")
    rp.add_argument("--session", help="filtra por prefixo de session_id (ex.: 86e5a22d)")
    rp.add_argument("--project", help="filtra por substring do projeto (ex.: GATSO ou wf_)")
    rp.add_argument("--no-ingest", action="store_true", help="não atualizar o banco antes")

    lp = sub.add_parser("limits", help="lista batidas de limite")
    lp.add_argument("--limit", type=int, default=50)

    wp = sub.add_parser("watch", help="ingest contínuo")
    wp.add_argument("--interval", type=int, default=60)

    mp = sub.add_parser("meter", help="lê o medidor oficial /usage (custo zero) e grava na tabela meter")
    mp.add_argument("--watch", action="store_true", help="loop contínuo")
    mp.add_argument("--interval", type=int, default=300)
    mp.add_argument("--no-notify", action="store_true", help="não notificar (macOS/Telegram)")

    mr = sub.add_parser("meter-report", help="histórico do medidor oficial")
    mr.add_argument("--limit", type=int, default=30)

    cp = sub.add_parser("calibrate", help="aprende fator de preço por modelo a partir de gastos reais de crédito")
    cp.add_argument("--brl", type=float, help="gasto real em R$ (converte por --rate)")
    cp.add_argument("--usd", type=float, help="gasto real em US$ (tem precedência sobre --brl)")
    cp.add_argument("--rate", type=float, default=5.9, help="câmbio US$/R$ (default 5.9)")
    cp.add_argument("--from", dest="from_", help="início da janela (ISO UTC); default = último credit_episode")
    cp.add_argument("--to", help="fim da janela (ISO UTC)")
    cp.add_argument("--note", help="rótulo do episódio")
    cp.add_argument("--list", action="store_true", help="lista episódios registrados")
    cp.add_argument("--solve", action="store_true", help="resolve os fatores por modelo (mínimos quadrados)")
    cp.add_argument("--apply", action="store_true", help="grava os fatores resolvidos (com --solve)")

    bp = sub.add_parser("bursts", help="detalha clusters de atividade (gatilho, billing, modelos, cap)")
    bp.add_argument("--session", help="prefixo do session_id (vazio = todas)")
    bp.add_argument("--gap", type=int, default=1200, help="segundos de inatividade que separam bursts (default 1200=20min)")
    bp.add_argument("--utc", action="store_true", help="exibir em UTC (default: horário local METER_TZ)")
    bp.add_argument("--no-ingest", action="store_true")

    args = ap.parse_args()
    con = db_connect()

    if args.cmd == "ingest":
        ingest(con)
    elif args.cmd == "report":
        if not getattr(args, "no_ingest", False):
            ingest(con, verbose=False)
        report(con, args)
    elif args.cmd == "limits":
        ingest(con, verbose=False)
        limits(con, args)
    elif args.cmd == "watch":
        print(f"watch: ingest a cada {args.interval}s (Ctrl-C para sair)")
        try:
            while True:
                ingest(con)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nencerrado.")
    elif args.cmd == "meter":
        notify = not args.no_notify
        if args.watch:
            print(f"meter: /usage a cada {args.interval}s (Ctrl-C para sair)")
            try:
                while True:
                    meter_once(con, notify=notify)
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                print("\nencerrado.")
        else:
            meter_once(con, notify=notify)
    elif args.cmd == "meter-report":
        meter_report(con, args)
    elif args.cmd == "bursts":
        if not getattr(args, "no_ingest", False):
            ingest(con, verbose=False)
        bursts_report(con, args)
    elif args.cmd == "calibrate":
        if not (args.list or args.solve):
            ingest(con, verbose=False)
        calibrate(con, args)
    con.close()


if __name__ == "__main__":
    main()
