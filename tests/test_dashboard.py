"""Dashboard HTML: seções derivadas dos eixos do report (janelas por modelo,
custo/dia por fonte, sessões/projetos, créditos+renovações, Codex)."""
import contextlib
import io
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cnp import token_monitor as tm

CREATE_METER = """
CREATE TABLE meter (
    ts TEXT PRIMARY KEY,
    ts_epoch REAL,
    session_pct INTEGER,
    session_reset TEXT,
    session_reset_epoch REAL,
    week_pct INTEGER,
    week_reset TEXT,
    week_reset_epoch REAL,
    event TEXT
)
"""
CREATE_USAGE = """
CREATE TABLE usage (
    uuid TEXT PRIMARY KEY, ts TEXT, ts_epoch REAL, session_id TEXT, project TEXT,
    git_branch TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER,
    cache_read INTEGER, cache_write INTEGER, web_search INTEGER, web_fetch INTEGER,
    service_tier TEXT, billing_source TEXT DEFAULT 'subscription'
)
"""
CREATE_CODEX_METER = CREATE_METER.replace("TABLE meter", "TABLE codex_meter")
CREATE_RENEWALS = "CREATE TABLE plan_renewals (ts_epoch REAL PRIMARY KEY, ts TEXT, plan TEXT, note TEXT)"
CREATE_CODEX_USAGE = """
CREATE TABLE codex_usage (
    rollout TEXT PRIMARY KEY, session_id TEXT, started_epoch REAL, ended_epoch REAL,
    model TEXT, input_tokens INTEGER, cached_input_tokens INTEGER, output_tokens INTEGER,
    reasoning_output_tokens INTEGER, total_tokens INTEGER, updated_epoch REAL,
    cwd TEXT, title TEXT
)
"""
CREATE_CODEX_MODEL_USAGE = """
CREATE TABLE codex_model_usage (
    rollout TEXT, model TEXT, input_tokens INTEGER, cached_input_tokens INTEGER,
    output_tokens INTEGER, reasoning_output_tokens INTEGER, total_tokens INTEGER,
    PRIMARY KEY (rollout, model)
)
"""
CREATE_LIMITS = """
CREATE TABLE limits (uuid TEXT PRIMARY KEY, ts TEXT, ts_epoch REAL, session_id TEXT,
    project TEXT, model TEXT, message TEXT)
"""


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        for ddl in (CREATE_METER, CREATE_CODEX_METER, CREATE_USAGE, CREATE_RENEWALS,
                    CREATE_CODEX_USAGE, CREATE_CODEX_MODEL_USAGE, CREATE_LIMITS):
            self.con.execute(ddl)
        now = time.time()
        # medidor: 90% -> 100% (cap) -> renovação zera; episódio de crédito no meio
        for i, (dt, pct, ev) in enumerate([(-7200, 90, None), (-5400, 100, "cap_5h"),
                                           (-3600, 0, "reset_5h_early,reset_week_early")]):
            self.con.execute("INSERT INTO meter VALUES (?,?,?,?,?,?,?,?,?)",
                             (f"t{i}", now + dt, pct, "5pm", now + 3600, 10, "Oct 1", now + 86400, ev))
        rows = [
            ("u1", now - 5000, "s1-aaaa", "projA", "claude-fable-5", 100, 900, "credits"),
            ("u2", now - 3000, "s1-aaaa", "projA", "claude-fable-5", 200, 1800, "subscription"),
            ("u3", now - 2000, "s2-bbbb", "projB", "claude-opus-4-8", 50, 400, "subscription"),
            ("u4", now - 3 * 86400, "s3-cccc", "projB", "claude-opus-4-8", 10, 100, "subscription"),
        ]
        for uid, e, sid, proj, m, i, o, src in rows:
            self.con.execute("INSERT INTO usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (uid, "iso", e, sid, proj, "main", m, i, o, 50_000, 2_000, 0, 0, None, src))
        self.con.execute("INSERT INTO plan_renewals VALUES (?,?,?,?)",
                         (now - 3600, "iso", "max_20x", "renovação"))
        self.con.execute("INSERT INTO limits VALUES (?,?,?,?,?,?,?)",
                         ("l1", "2026-07-01T12:00:00Z", now - 86400, "s1-aaaa", "projA",
                          "<synthetic>", "You've hit your session limit"))
        self.con.execute("INSERT INTO codex_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         ("r1", "cx1-dddd", now - 4000, now - 3500, "gpt-5", 10, 0, 20, 5, 35, now,
                          "/Users/x/Workspace/outro-app", "Refatora o pipeline"))
        for mdl, i, o, r, t in (("gpt-5", 6, 12, 3, 21), ("gpt-6", 4, 8, 2, 14)):
            self.con.execute("INSERT INTO codex_model_usage VALUES (?,?,?,?,?,?,?)",
                             ("r1", mdl, i, 0, o, r, t))

    def _render(self) -> str:
        out = Path(self.tmpdir) / "dash.html"
        # transcript fake: dá título (summary) e workspace à sessão s1-aaaa
        proj_dir = Path(self.tmpdir) / "projects" / "-Users-x-Workspace-meu-app"
        proj_dir.mkdir(parents=True)
        (proj_dir / "s1-aaaa.jsonl").write_text(
            '{"type":"summary","summary":"Título da sessão de teste"}\n'
            '{"type":"user","message":{"role":"user","content":"oi"}}\n')
        args = SimpleNamespace(out=str(out), open=False)
        with patch.object(tm, "read_codex_meter", return_value=None), \
                patch.object(tm, "PROJECTS_DIR", Path(self.tmpdir) / "projects"), \
                contextlib.redirect_stdout(io.StringIO()):
            tm.dashboard(self.con, args)
        return out.read_text()

    def test_dashboard_renderiza_todas_as_secoes(self):
        with tempfile.TemporaryDirectory() as d:
            self.tmpdir = d
            html = self._render()
        # abas por janela (hoje/7d/30d) e colunas novas (inclui cache na tabela)
        for token in ('data-w="mw0"', ">hoje<", ">7 dias<", ">30 dias<", ">%out<",
                      ">cache_r<", ">cache_w<"):
            self.assertIn(token, html)
        # rankings dos eixos do report
        self.assertIn("Top sessões", html)
        self.assertIn("s1-aaaa"[:8], html)
        self.assertIn("Por projeto", html)
        self.assertIn("projA", html)
        # tooltips ricos (data-tip com HTML escapado + JS do card flutuante)
        self.assertIn("Título da sessão de teste", html)          # título no data-tip da sessão
        self.assertIn("Users-x-Workspace-meu-app", html)          # workspace no card
        self.assertIn("data-tip=", html)
        self.assertIn("tip.innerHTML = el.dataset.tip", html)
        # decomposição do ~USD: preço por componente no tooltip do modelo
        self.assertIn("cache leitura", html)
        self.assertIn("/M", html)
        # custo/dia empilhado: barra âmbar quando há crédito no dia
        self.assertIn('fill="#f59e0b"', html)
        # créditos & renovações
        self.assertIn("Créditos &amp; renovações", html)
        self.assertIn("max_20x", html)
        self.assertIn("💳", html)
        # seções novas do painel enriquecido
        self.assertIn("Medidor Claude semanal", html)
        self.assertIn("Billing — assinatura × créditos", html)
        self.assertIn("Batidas de limite", html)
        self.assertIn("hit your session limit", html)
        self.assertIn("Preços &amp; calibração", html)
        self.assertIn('class="tot"', html)                 # linha TOTAL por janela
        self.assertIn("🌿", html)                          # branch no card da sessão
        # Codex enriquecido: por modelo, top sessões (tooltip com título/cwd) e por dia
        self.assertIn("Codex — por modelo", html)
        self.assertIn("gpt-6", html)
        self.assertIn("Codex — top sessões", html)
        self.assertIn("Refatora o pipeline", html)         # título no data-tip
        self.assertIn("Workspace/outro-app", html)         # cwd no card
        self.assertIn("Codex — sessões por dia", html)
        self.assertNotIn("sem sessões Codex", html)

    def test_dashboard_tolera_banco_sem_tabelas_extras(self):
        con = sqlite3.connect(":memory:")
        for ddl in (CREATE_METER, CREATE_CODEX_METER, CREATE_USAGE):
            con.execute(ddl)
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "dash.html"
            args = SimpleNamespace(out=str(out), open=False)
            with patch.object(tm, "read_codex_meter", return_value=None), \
                    contextlib.redirect_stdout(io.StringIO()):
                tm.dashboard(con, args)
            html = out.read_text()
        self.assertIn("nenhuma renovação registrada", html)
        self.assertIn("sem sessões Codex", html)


if __name__ == "__main__":
    unittest.main()
