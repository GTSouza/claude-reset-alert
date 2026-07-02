"""Instância única dos modos watch (flock + takeover), versão publicada (re-exec)
e plano/renovação (plan_renewals refinando credit_episodes)."""
import contextlib
import datetime as _dt
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
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
CREATE_USAGE = ("CREATE TABLE usage (ts_epoch REAL, output_tokens INTEGER, model TEXT, "
                "billing_source TEXT DEFAULT 'subscription')")
CREATE_RENEWALS = "CREATE TABLE plan_renewals (ts_epoch REAL PRIMARY KEY, ts TEXT, plan TEXT, note TEXT)"

# Processo que segura o flock do lock e dorme — simula um watcher já rodando.
# O nome do arquivo decide se o takeover o reconhece como token_monitor ou não.
HOLDER_SRC = textwrap.dedent("""
    import fcntl, json, os, sys, time
    fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.ftruncate(fd, 0)
    os.write(fd, json.dumps({"pid": os.getpid()}).encode())
    print("held", flush=True)
    time.sleep(120)
""")


@unittest.skipIf(tm.fcntl is None, "plataforma sem fcntl/flock")
class WatchLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lock = Path(self.tmp.name) / "watch.lock"
        self.procs = []
        os.environ.pop(tm._LOCK_FD_ENV, None)

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait()
            if p.stdout:
                p.stdout.close()
        os.environ.pop(tm._LOCK_FD_ENV, None)
        self.tmp.cleanup()

    def _spawn_holder(self, script_name: str) -> subprocess.Popen:
        script = Path(self.tmp.name) / script_name
        script.write_text(HOLDER_SRC)
        p = subprocess.Popen([sys.executable, str(script), str(self.lock)],
                             stdout=subprocess.PIPE, text=True)
        self.procs.append(p)
        self.assertEqual(p.stdout.readline().strip(), "held")
        return p

    def test_acquire_livre_grava_meta(self):
        with patch.object(tm, "WATCH_LOCK_PATH", self.lock):
            fd = tm.acquire_watch_lock(takeover_timeout=5, sweep=False)
        try:
            meta = json.loads(self.lock.read_text())
            self.assertEqual(meta["pid"], os.getpid())
            self.assertTrue(os.get_inheritable(fd))  # precisa sobreviver ao re-exec
        finally:
            os.close(fd)

    def test_takeover_encerra_watcher_antigo(self):
        holder = self._spawn_holder("token_monitor_fake.py")
        with patch.object(tm, "WATCH_LOCK_PATH", self.lock):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                fd = tm.acquire_watch_lock(takeover_timeout=15, sweep=False)
        try:
            self.assertIsNotNone(holder.wait(timeout=10))   # antigo morreu
            self.assertEqual(json.loads(self.lock.read_text())["pid"], os.getpid())
            self.assertIn("instância nova vence", buf.getvalue())
        finally:
            os.close(fd)

    def test_nao_mata_processo_alheio(self):
        holder = self._spawn_holder("innocent_app.py")
        with patch.object(tm, "WATCH_LOCK_PATH", self.lock):
            with self.assertRaises(SystemExit):
                tm.acquire_watch_lock(takeover_timeout=5, sweep=False)
        self.assertIsNone(holder.poll())                     # segue vivo

    def test_adota_fd_herdado_do_reexec(self):
        fd = os.open(str(self.lock), os.O_RDWR | os.O_CREAT, 0o644)
        tm.fcntl.flock(fd, tm.fcntl.LOCK_EX | tm.fcntl.LOCK_NB)
        os.environ[tm._LOCK_FD_ENV] = str(fd)
        with patch.object(tm, "WATCH_LOCK_PATH", self.lock):
            got = tm.acquire_watch_lock(takeover_timeout=5, sweep=False)
        try:
            self.assertEqual(got, fd)                        # mesmo fd, lock nunca solto
            self.assertNotIn(tm._LOCK_FD_ENV, os.environ)    # env consumida
            self.assertEqual(json.loads(self.lock.read_text())["pid"], os.getpid())
        finally:
            os.close(fd)


class ScriptStampTest(unittest.TestCase):
    def test_stamp_muda_quando_o_script_muda(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "token_monitor.py"
            f.write_text("v1")
            with patch.object(tm, "SCRIPT_PATH", f):
                s1 = tm._script_stamp()
                self.assertIsNotNone(s1)
                f.write_text("v2 maior")
                os.utime(f, (time.time() + 2, time.time() + 2))
                self.assertNotEqual(tm._script_stamp(), s1)

    def test_stamp_none_quando_script_sumiu(self):
        with patch.object(tm, "SCRIPT_PATH", Path("/nao/existe/token_monitor.py")):
            self.assertIsNone(tm._script_stamp())


class RenewalCutTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.execute(CREATE_METER)
        self.con.execute(CREATE_USAGE)
        self.con.execute(CREATE_RENEWALS)
        self.t0 = 1_000_000.0
        # 90% -> 100% (t0+300) -> 100% (t0+600) -> 0% (t0+900); tokens reais em t0+400
        for i, (e, pct) in enumerate([(self.t0, 90), (self.t0 + 300, 100),
                                      (self.t0 + 600, 100), (self.t0 + 900, 0)]):
            self.con.execute("INSERT INTO meter VALUES (?,?,?,?,?,?,?,?,?)",
                             (f"t{i}", e, pct, None, None, 0, None, None, None))
        self.con.execute("INSERT INTO usage VALUES (?,?,?,'subscription')",
                         (self.t0 + 400, 1000, "claude-fable-5"))

    def test_sem_renovacao_fim_no_ponto_medio(self):
        self.assertEqual(tm.credit_episodes(self.con),
                         [(self.t0 + 300, self.t0 + 750)])

    def test_renovacao_dentro_do_episodio_corta_o_fim(self):
        self.con.execute("INSERT INTO plan_renewals VALUES (?,?,?,?)",
                         (self.t0 + 700, "iso", "max_20x", None))
        self.assertEqual(tm.credit_episodes(self.con),
                         [(self.t0 + 300, self.t0 + 700)])

    def test_renovacao_na_segunda_metade_do_gap_tambem_corta(self):
        # entre o ponto médio (t0+750) e a 1ª leitura <100% (t0+900): a renovação
        # segue sendo o fim real do episódio — não pode ser ignorada.
        self.con.execute("INSERT INTO plan_renewals VALUES (?,?,?,?)",
                         (self.t0 + 800, "iso", "max_20x", None))
        self.assertEqual(tm.credit_episodes(self.con),
                         [(self.t0 + 300, self.t0 + 800)])

    def test_renovacao_fora_do_episodio_nao_afeta(self):
        self.con.execute("INSERT INTO plan_renewals VALUES (?,?,?,?)",
                         (self.t0 + 2000, "iso", "max_20x", None))
        self.assertEqual(tm.credit_episodes(self.con),
                         [(self.t0 + 300, self.t0 + 750)])

    def test_banco_sem_tabela_plan_renewals_segue_funcionando(self):
        con = sqlite3.connect(":memory:")
        con.execute(CREATE_METER)
        con.execute(CREATE_USAGE)
        self.assertEqual(tm.credit_episodes(con), [])        # sem meter rows
        self.assertEqual(tm.plan_renewal_epochs(con), [])
        self.assertEqual(tm.current_plan(con), (None, None))


class PlanCmdTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.execute(CREATE_METER)
        self.con.execute(CREATE_USAGE)
        self.con.execute(CREATE_RENEWALS)

    def test_set_registra_no_fuso_local_e_reclassifica(self):
        args = SimpleNamespace(set_plan="max_20x", renewed="2026-07-02 12:12",
                               note="renovação Max 20x")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            tm.plan_cmd(self.con, args)
        plan, when = tm.current_plan(self.con)
        self.assertEqual(plan, "max_20x")
        expected = _dt.datetime(2026, 7, 2, 12, 12, tzinfo=tm._meter_tz()).timestamp()
        self.assertAlmostEqual(when, expected)
        self.assertIn("renovação registrada", buf.getvalue())

    def test_listagem_mostra_plano_atual(self):
        with contextlib.redirect_stdout(io.StringIO()):
            tm.plan_cmd(self.con, SimpleNamespace(set_plan="max_20x",
                                                  renewed="2026-07-02 12:12", note=None))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            tm.plan_cmd(self.con, SimpleNamespace(set_plan=None))
        out = buf.getvalue()
        self.assertIn("plano atual: max_20x", out)
        self.assertIn("2026-07-02 12:12", out)

    def test_sem_registro_orienta_o_uso(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            tm.plan_cmd(self.con, SimpleNamespace(set_plan=None))
        self.assertIn("nenhuma renovação registrada", buf.getvalue())

    def test_renewed_sem_set_e_erro(self):
        with self.assertRaises(SystemExit):
            tm.plan_cmd(self.con, SimpleNamespace(set_plan=None,
                                                  renewed="2026-07-02 12:12", note=None))
        self.assertEqual(tm.current_plan(self.con), (None, None))   # nada gravado

    def test_remove_desfaz_renovacao_errada(self):
        with contextlib.redirect_stdout(io.StringIO()):
            tm.plan_cmd(self.con, SimpleNamespace(set_plan="max_20x",
                                                  renewed="2026-07-02 12:12", note=None))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            tm.plan_cmd(self.con, SimpleNamespace(set_plan=None, renewed=None,
                                                  note=None, remove="2026-07-02 12:12"))
        self.assertIn("renovação removida", buf.getvalue())
        self.assertEqual(tm.current_plan(self.con), (None, None))

    def test_remove_sem_match_e_erro(self):
        with self.assertRaises(SystemExit):
            tm.plan_cmd(self.con, SimpleNamespace(set_plan=None, renewed=None,
                                                  note=None, remove="2026-01-01 00:00"))


if __name__ == "__main__":
    unittest.main()
