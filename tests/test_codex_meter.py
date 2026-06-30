import contextlib
import io
import sqlite3
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cnp import token_monitor


CREATE_CODEX_METER = """
CREATE TABLE codex_meter (
    ts TEXT PRIMARY KEY,
    ts_epoch REAL,
    session_pct REAL,
    session_reset TEXT,
    session_reset_epoch REAL,
    week_pct REAL,
    week_reset TEXT,
    week_reset_epoch REAL,
    plan TEXT,
    event TEXT
)
"""


class CodexMeterAlertsTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.execute(CREATE_CODEX_METER)
        self.con.execute(
            "INSERT INTO codex_meter VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("1970-01-01T00:16:40+00:00", 1000, 98, "old", 1100,
             50, "week", 9000, "plus", None),
        )

    def tearDown(self):
        self.con.close()

    def test_static_rollout_notifies_reset_only_once(self):
        snapshot = {
            "ts_epoch": 2000,
            "session_pct": 7,
            "session_reset_epoch": 3000,
            "week_pct": 50,
            "week_reset_epoch": 9000,
            "plan": "plus",
        }

        with patch.object(token_monitor, "read_codex_meter", return_value=snapshot), \
             patch.object(token_monitor, "_notify") as notify:
            with contextlib.redirect_stdout(io.StringIO()):
                token_monitor.codex_meter_once(self.con, notify=False)
                token_monitor.codex_meter_once(self.con, notify=False)

        self.assertEqual(notify.call_count, 1)
        self.assertIn("resetou", notify.call_args.args[0])

    def test_reset_epoch_without_percentage_change_does_not_notify(self):
        snapshot = {
            "ts_epoch": 2000,
            "session_pct": 98,
            "session_reset_epoch": 3000,
            "week_pct": 50,
            "week_reset_epoch": 9000,
            "plan": "plus",
        }

        with patch.object(token_monitor, "read_codex_meter", return_value=snapshot), \
             patch.object(token_monitor, "_notify") as notify:
            with contextlib.redirect_stdout(io.StringIO()):
                token_monitor.codex_meter_once(self.con, notify=False)

        notify.assert_not_called()
        event = self.con.execute(
            "SELECT event FROM codex_meter WHERE ts_epoch = 2000"
        ).fetchone()[0]
        self.assertIsNone(event)


class CodexWatchTickRefreshTest(unittest.TestCase):
    """O tick do watch confirma ao vivo só quando um reset cruza — e uma vez por snapshot."""

    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.execute(CREATE_CODEX_METER)

    def tearDown(self):
        self.con.close()

    # epochs absolutos: "passado" e "futuro" são relativos ao now() real do código.
    PAST, FUTURE = 1000, 10**12

    def _snap(self, ts_epoch, session_reset_epoch, week_reset_epoch):
        return {"ts_epoch": ts_epoch, "session_pct": 100, "session_reset_epoch": session_reset_epoch,
                "week_pct": 100, "week_reset_epoch": week_reset_epoch, "plan": "plus"}

    def test_refresh_fires_once_per_stale_snapshot(self):
        # rollout antigo cujo reset semanal já cruzou (no passado, > ts) → precisa confirmar
        stale = self._snap(ts_epoch=self.PAST, session_reset_epoch=self.FUTURE,
                           week_reset_epoch=self.PAST + 500)
        with patch.object(token_monitor, "read_codex_meter", return_value=stale), \
             patch.object(token_monitor, "codex_live_refresh", return_value=("ok", None)) as live, \
             patch.object(token_monitor, "codex_meter_once", return_value=True):
            with contextlib.redirect_stdout(io.StringIO()):
                tick = token_monitor._make_codex_tick(self.con, notify=False, auto_refresh=True)
                tick(); tick(); tick()
        # mesmo snapshot velho (codex exec falhou em produzir rollout novo) → gasta 1 turno só
        self.assertEqual(live.call_count, 1)

    def test_no_refresh_when_no_reset_crossed(self):
        # ambos os resets no futuro → leitura ainda válida, não gasta turno
        fresh = self._snap(ts_epoch=self.PAST, session_reset_epoch=self.FUTURE,
                           week_reset_epoch=self.FUTURE)
        with patch.object(token_monitor, "read_codex_meter", return_value=fresh), \
             patch.object(token_monitor, "codex_live_refresh") as live, \
             patch.object(token_monitor, "codex_meter_once", return_value=True):
            with contextlib.redirect_stdout(io.StringIO()):
                tick = token_monitor._make_codex_tick(self.con, notify=False, auto_refresh=True)
                tick(); tick()
        live.assert_not_called()

    def test_no_refresh_without_auto_refresh(self):
        stale = self._snap(ts_epoch=self.PAST, session_reset_epoch=self.FUTURE,
                           week_reset_epoch=self.PAST + 500)
        with patch.object(token_monitor, "read_codex_meter", return_value=stale), \
             patch.object(token_monitor, "codex_live_refresh") as live, \
             patch.object(token_monitor, "codex_meter_once", return_value=True):
            with contextlib.redirect_stdout(io.StringIO()):
                tick = token_monitor._make_codex_tick(self.con, notify=False, auto_refresh=False)
                tick()
        live.assert_not_called()


class CodexLiveRefreshCommandTest(unittest.TestCase):
    """Trava os flags do codex exec: ~ não é repo trusted e o stdin não pode herdar o TTY."""

    def test_uses_skip_git_check_and_closed_stdin(self):
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        with patch.object(token_monitor.shutil, "which", return_value="/usr/bin/codex"), \
             patch.object(token_monitor.subprocess, "run", side_effect=fake_run), \
             patch.object(token_monitor, "read_codex_meter", return_value=None):
            status, _ = token_monitor.codex_live_refresh()

        self.assertEqual(status, "ok")
        self.assertIn("--skip-git-repo-check", captured["argv"])
        self.assertEqual(captured["kwargs"].get("stdin"), subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
