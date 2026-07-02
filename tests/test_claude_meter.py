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


class ResetClassificationTest(unittest.TestCase):
    """Reset natural (no/depois do horário) x ANTECIPADO (Anthropic zerou antes do previsto),
    e o reconfirm que separa reset real de glitch transitório."""

    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.execute(CREATE_METER)
        self.con.execute("CREATE TABLE usage (ts_epoch REAL, output_tokens INTEGER, model TEXT)")
        import datetime as _dt
        self.now = _dt.datetime.now(_dt.timezone.utc).timestamp()
        self.s_re = tm._reset_to_epoch(S_RESET)   # horário NOVO (futuro, ~2 dias)
        self.w_re = tm._reset_to_epoch(W_RESET)
        # desliga o warm-up por padrão nos testes (senão faria um `claude -p` real); o teste
        # dedicado do warm-up religa localmente e mocka _warmup_usage.
        self._orig_warmup = tm.METER_WARMUP
        tm.METER_WARMUP = False

    def tearDown(self):
        tm.METER_WARMUP = self._orig_warmup
        self.con.close()

    def _prev(self, s_pct, s_re):
        # janela semanal estável (mesmo % e horário) p/ isolar a 5h.
        self.con.execute(
            "INSERT INTO meter VALUES (?,?,?,?,?,?,?,?,?)",
            ("prev", self.now - 300, s_pct, S_RESET, s_re, 71, W_RESET, self.w_re, None),
        )

    def _titles(self, notify):
        return " || ".join(c.args[0] for c in notify.call_args_list)

    def _event(self):
        return self.con.execute("SELECT event FROM meter ORDER BY ts_epoch DESC LIMIT 1").fetchone()[0] or ""

    WEEK_STABLE = f"Current week (all models): 71% used · resets {W_RESET}"

    def test_natural_reset_on_schedule(self):
        # horário previsto JÁ passou (now-1h) => reset no horário => natural, sem ANTECIPADO.
        self._prev(99, self.now - 3600)
        usage = f"Current session: 0% used · resets {S_RESET}\n" + self.WEEK_STABLE
        with patch.object(tm, "_fetch_usage", return_value=usage), \
             patch.object(tm, "_notify") as notify:
            with contextlib.redirect_stdout(io.StringIO()):
                tm.meter_once(self.con, notify=False)
        self.assertIn("reset_5h", self._event())
        self.assertNotIn("_early", self._event())
        t = self._titles(notify)
        self.assertIn("resetou", t)
        self.assertNotIn("ANTECIPADO", t)

    def test_early_reset_before_schedule(self):
        # horário previsto ainda no FUTURO (now+2h) mas a janela já zerou => ANTECIPADO.
        self._prev(99, self.now + 7200)
        usage = f"Current session: 0% used · resets {S_RESET}\n" + self.WEEK_STABLE
        with patch.object(tm, "_fetch_usage", return_value=usage), \
             patch.object(tm, "_notify") as notify:
            with contextlib.redirect_stdout(io.StringIO()):
                tm.meter_once(self.con, notify=False)
        self.assertIn("reset_5h_early", self._event())
        self.assertIn("ANTECIPADO", self._titles(notify))

    def test_early_reset_without_time_still_fires(self):
        # reset ANTECIPADO em que o /usage veio degradado (0% + reset None) e assim persiste no
        # reconfirm: mesmo sem horário novo, dispara reset_5h_early (o incidente real).
        self._prev(99, self.now + 7200)
        degraded = "Current session: 0% used\n" + self.WEEK_STABLE
        with patch.object(tm, "_fetch_usage", return_value=degraded), \
             patch.object(tm, "_notify") as notify:
            with contextlib.redirect_stdout(io.StringIO()):
                tm.meter_once(self.con, notify=False)
        self.assertIn("reset_5h_early", self._event())
        self.assertIn("ANTECIPADO", self._titles(notify))

    def test_reconfirm_cooldown_blocks_flapping(self):
        # /usage oscilando degradado<->saudável não deve gastar 1 haiku por poll: o cooldown
        # (marca 'reconfirm') bloqueia a 2ª releitura logo em seguida.
        self._prev(90, self.s_re)
        degraded = "Current session: 90% used\n" + self.WEEK_STABLE
        healthy = f"Current session: 90% used · resets {S_RESET}\n" + self.WEEK_STABLE
        # poll1: degraded -> reconfirm adota healthy (grava epoch não-nulo + marca reconfirm)
        # poll2: degraded de novo -> p_sre não-nulo re-armaria, mas o cooldown bloqueia.
        # 3 valores só: se o poll2 tentasse reconfirmar, faltaria um 4º (StopIteration).
        with patch.object(tm, "_fetch_usage", side_effect=[degraded, healthy, degraded]) as fetch, \
             patch.object(tm, "_notify"):
            with contextlib.redirect_stdout(io.StringIO()):
                tm.meter_once(self.con, notify=False)
                tm.meter_once(self.con, notify=False)
        self.assertEqual(fetch.call_count, 3)   # poll1 main+reconfirm, poll2 main (sem reconfirm)
        last = self.con.execute("SELECT event FROM meter ORDER BY ts_epoch DESC LIMIT 1").fetchone()[0] or ""
        self.assertNotIn("reconfirm", last)     # 2º poll não reconfirmou (cooldown)

    def test_partial_drop_flagged_far_after_reset(self):
        # queda parcial (não zerou) 1h DEPOIS do reset previsto => ⚠️ (drop suspeito). O bug
        # trocava o critério abs() por 'early' (só ANTES do horário), perdendo justamente este
        # caso. Sem linha 'resets' (rep None) p/ cair no ramo de queda, não no de avanço de epoch.
        self._prev(70, self.now - 3600)   # reset previsto foi 1h atrás (longe, depois)
        degraded = "Current session: 40% used\n" + self.WEEK_STABLE
        with patch.object(tm, "_fetch_usage", side_effect=[degraded, degraded]), \
             patch.object(tm, "_notify") as notify:
            with contextlib.redirect_stdout(io.StringIO()):
                tm.meter_once(self.con, notify=False)
        self.assertIn("drop_5h", self._event())
        self.assertIn("liberou", self._titles(notify))
        self.assertIn("⚠️", self._titles(notify))

    def test_infers_reset_time_when_missing(self):
        # reset ANTECIPADO sem horário e reconfirm não recupera => estima ~now + 5h e marca '≈'.
        self._prev(99, self.now + 7200)
        degraded = "Current session: 0% used\n" + self.WEEK_STABLE
        with patch.object(tm, "_fetch_usage", return_value=degraded), \
             patch.object(tm, "_notify") as notify:
            with contextlib.redirect_stdout(io.StringIO()):
                tm.meter_once(self.con, notify=False)
        sr, sre = self.con.execute(
            "SELECT session_reset, session_reset_epoch FROM meter ORDER BY ts_epoch DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(sre)
        self.assertAlmostEqual(sre - self.now, 5 * 3600, delta=180)   # ~ agora + 5h
        self.assertTrue(sr.startswith("≈"))                          # marcado como estimativa
        self.assertIn("reset_5h_early", self._event())
        self.assertIn("ANTECIPADO", self._titles(notify))

    def test_inferred_reset_carried_forward_stable(self):
        # o horário estimado é CARREGADO adiante (estável, sem re-inferir/drift) e não re-alerta.
        self._prev(99, self.now + 7200)
        degraded = "Current session: 0% used\n" + self.WEEK_STABLE
        with patch.object(tm, "_fetch_usage", return_value=degraded), \
             patch.object(tm, "_notify") as notify:
            with contextlib.redirect_stdout(io.StringIO()):
                tm.meter_once(self.con, notify=False)               # poll1: infere
                first = self.con.execute(
                    "SELECT session_reset_epoch FROM meter ORDER BY ts_epoch DESC LIMIT 1").fetchone()[0]
                n1 = notify.call_count
                tm.meter_once(self.con, notify=False)               # poll2: carrega
                second = self.con.execute(
                    "SELECT session_reset_epoch FROM meter ORDER BY ts_epoch DESC LIMIT 1").fetchone()[0]
        self.assertEqual(first, second)             # estável, não re-inferido
        self.assertEqual(notify.call_count, n1)     # poll2 não re-alertou

    def test_warmup_recovers_real_reset_time(self):
        # reconfirm re-lê degradado; o warm-up gera uso e a 3ª leitura traz o horário REAL,
        # que é adotado em vez da estimativa '≈'.
        self._prev(99, self.now + 7200)
        degraded = "Current session: 0% used\n" + self.WEEK_STABLE
        recovered = f"Current session: 1% used · resets {S_RESET}\n" + self.WEEK_STABLE
        with patch.object(tm, "METER_WARMUP", True), \
             patch.object(tm, "_warmup_usage") as warm, \
             patch.object(tm, "_fetch_usage", side_effect=[degraded, degraded, recovered]), \
             patch.object(tm, "_notify") as notify:
            with contextlib.redirect_stdout(io.StringIO()):
                tm.meter_once(self.con, notify=False)
        warm.assert_called_once()                       # gerou o warm-up
        sr, sre = self.con.execute(
            "SELECT session_reset, session_reset_epoch FROM meter ORDER BY ts_epoch DESC LIMIT 1"
        ).fetchone()
        self.assertFalse(sr.startswith("≈"))            # horário REAL, não estimativa
        self.assertEqual(sre, self.s_re)                # == _reset_to_epoch(S_RESET)
        self.assertIn("reset_5h_early", self._event())

    def test_reconfirm_recovers_glitch(self):
        # glitch transitório: 1ª leitura 0%+reset None; o reconfirm traz de volta o valor ANTIGO
        # (99% + MESMO horário) => adota, NÃO vira reset falso.
        self._prev(99, self.s_re)   # horário anterior == horário que volta (glitch não muda o reset)
        degraded = "Current session: 0% used\n" + self.WEEK_STABLE
        healthy = f"Current session: 99% used · resets {S_RESET}\n" + self.WEEK_STABLE
        with patch.object(tm, "_fetch_usage", side_effect=[degraded, healthy]), \
             patch.object(tm, "_notify") as notify:
            with contextlib.redirect_stdout(io.StringIO()):
                tm.meter_once(self.con, notify=False)
        self.assertNotIn("reset", self._event())        # nenhum reset
        self.assertNotIn("ANTECIPADO", self._titles(notify))
        # e o valor recuperado (99%) foi o gravado, não o 0% do glitch
        sp = self.con.execute("SELECT session_pct FROM meter ORDER BY ts_epoch DESC LIMIT 1").fetchone()[0]
        self.assertEqual(sp, 99)


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
