import contextlib
import io
import json
import sqlite3
import tempfile
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

S_RESET = "Jul 3 at 3pm (America/Sao_Paulo)"
W_RESET = "Jul 6 at 9pm (America/Sao_Paulo)"


def _usage(s_pct, w_pct):
    """Texto /usage sintético que _parse_line consegue ler (linha agregada, sem 'only')."""
    s = f"Current session: {s_pct}% used · resets {S_RESET}"
    w = f"Current week (all models): {w_pct}% used · resets {W_RESET}"
    return s + "\n" + w


class MeterOnceRobustnessTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.execute(CREATE_METER)
        # tabela mínima usada pela ramificação de créditos (s_pct >= 100) via _real_output_tokens
        self.con.execute("CREATE TABLE usage (ts_epoch REAL, output_tokens INTEGER, model TEXT)")
        self.s_re = tm._reset_to_epoch(S_RESET)
        self.w_re = tm._reset_to_epoch(W_RESET)

    def tearDown(self):
        self.con.close()

    def _row(self, ts_epoch, s_pct, s_re, w_pct, w_re):
        self.con.execute(
            "INSERT INTO meter VALUES (?,?,?,?,?,?,?,?,?)",
            (f"t{ts_epoch}", ts_epoch, s_pct, S_RESET, s_re, w_pct, W_RESET, w_re, None),
        )

    def _count(self):
        return self.con.execute("SELECT COUNT(*) FROM meter").fetchone()[0]

    def _latest_event(self):
        return self.con.execute(
            "SELECT event FROM meter ORDER BY ts_epoch DESC LIMIT 1"
        ).fetchone()[0]

    def test_all_null_reading_is_discarded(self):
        """C15: /usage sem NENHUM percentual parseável não grava linha all-NULL (que
        poluiria o histórico e reiniciaria a idade da leitura no gate sem informação)."""
        with patch.object(tm, "_fetch_usage", return_value="banner sem medidor 5% used"), \
             patch.object(tm, "_notify") as notify:
            with contextlib.redirect_stdout(io.StringIO()):
                ok = tm.meter_once(self.con, notify=False)
        self.assertFalse(ok)
        self.assertEqual(self._count(), 0)
        notify.assert_not_called()

    def test_cap_fires_across_null_previous_reading(self):
        """C16: a transição <100 -> 100 é detectada mesmo quando a leitura imediatamente
        anterior teve session_pct NULL (parse falhou) — usa a última leitura NÃO-NULA."""
        self._row(1000, 90, self.s_re, 50, self.w_re)     # última <100 conhecida
        self._row(2000, None, self.s_re, 50, self.w_re)   # poll anterior: 5h NULL
        with patch.object(tm, "_fetch_usage", return_value=_usage(100, 50)), \
             patch.object(tm, "ingest"), \
             patch.object(tm, "_notify"):
            with contextlib.redirect_stdout(io.StringIO()):
                ok = tm.meter_once(self.con, notify=False)
        self.assertTrue(ok)
        self.assertIn("cap_5h", self._latest_event() or "")

    def test_drop_not_flagged_when_prev_reset_unknown(self):
        """C14: p_rep None (horário de reset anterior não parseou) => a queda por reset
        NÃO deve vir marcada como suspeita (⚠️)."""
        self._row(2000, 50, None, 50, self.w_re)          # session_reset_epoch NULL => p_rep None
        with patch.object(tm, "_fetch_usage", return_value=_usage(40, 50)), \
             patch.object(tm, "_notify") as notify:
            with contextlib.redirect_stdout(io.StringIO()):
                ok = tm.meter_once(self.con, notify=False)
        self.assertTrue(ok)
        self.assertIn("drop_5h", self._latest_event() or "")
        titles = " ".join(c.args[0] for c in notify.call_args_list)
        self.assertNotIn("⚠️", titles)


class ResetToEpochTest(unittest.TestCase):
    """P1/P2: _reset_to_epoch tolera fuso da linha, 24h e forma relativa."""

    def test_line_timezone_is_honored(self):
        # mesmo horário nominal, fusos diferentes => epochs diferentes (offset real).
        sp = tm._reset_to_epoch("Jul 3 at 3pm (America/Sao_Paulo)")   # UTC-3
        tk = tm._reset_to_epoch("Jul 3 at 3pm (Asia/Tokyo)")          # UTC+9
        self.assertIsNotNone(sp)
        self.assertIsNotNone(tk)
        self.assertAlmostEqual((sp - tk) / 3600.0, 12.0, places=1)

    def test_24h_clock_equals_12h(self):
        self.assertEqual(
            tm._reset_to_epoch("Jul 3 at 15:00 (America/Sao_Paulo)"),
            tm._reset_to_epoch("Jul 3 at 3pm (America/Sao_Paulo)"),
        )

    def test_relative_form(self):
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc).timestamp()
        r = tm._reset_to_epoch("in 2 hours")
        self.assertIsNotNone(r)
        self.assertAlmostEqual((r - now) / 3600.0, 2.0, places=1)

    def test_unparseable_returns_none(self):
        self.assertIsNone(tm._reset_to_epoch("whenever soon"))


class CalibrateApplyGuardTest(unittest.TestCase):
    """P0: --apply não grava fator de baixa alavancagem — mantém 1.0 (nominal)."""

    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.execute(
            "CREATE TABLE calibration (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, note TEXT, "
            "real_usd REAL, win_from TEXT, win_to TEXT, tokens_json TEXT)"
        )
        self._orig_path = tm.FACTORS_PATH
        self.tmp = Path(tempfile.mkstemp(suffix=".json")[1])
        tm.FACTORS_PATH = self.tmp

    def tearDown(self):
        tm.FACTORS_PATH = self._orig_path
        self.con.close()
        self.tmp.unlink(missing_ok=True)

    def test_low_leverage_model_held_at_one(self):
        # 1 episódio, 2 modelos: sistema indeterminado; opus domina o $, haiku é desprezível.
        toks = {"claude-opus-4-8": {"in": 5_000_000, "out": 2_000_000, "cr": 0, "cw": 0},
                "claude-haiku-4-5": {"in": 1000, "out": 100, "cr": 0, "cw": 0}}
        self.con.execute("INSERT INTO calibration (real_usd, tokens_json) VALUES (?,?)",
                         (80.0, json.dumps(toks)))
        with contextlib.redirect_stdout(io.StringIO()):
            tm.calibrate(self.con, SimpleNamespace(list=False, solve=True, apply=True))
        applied = json.loads(self.tmp.read_text())
        self.assertEqual(applied["claude-haiku-4-5"], 1.0)   # alavancagem desprezível => nominal
        self.assertNotEqual(applied["claude-opus-4-8"], 1.0)  # dominante => fator aprendido


if __name__ == "__main__":
    unittest.main()
