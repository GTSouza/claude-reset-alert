import contextlib
import datetime as _dt
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

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


def _future_reset(days: int, hour_txt: str, tz: str = "America/Sao_Paulo") -> str:
    """Texto de reset SEMPRE no futuro (~days à frente), no formato do /usage.
    Datas fixas ('Jul 3 at 3pm') quebravam todo ano: quando a data literal fica >12h no
    passado, _reset_to_epoch rola +1 ano e os asserts de delta/fuso explodem — o teste
    falhava numa janela anual (Jul 3 18:00→Jul 4 06:00 UTC) sem nenhuma mudança de código."""
    lt = _dt.datetime.now(ZoneInfo(tz)) + _dt.timedelta(days=days)
    return f"{lt.strftime('%b')} {lt.day} at {hour_txt} ({tz})"


S_RESET = _future_reset(2, "3pm")
W_RESET = _future_reset(5, "9pm")


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

    def test_estimated_epoch_advance_is_not_reset(self):
        """Regressão: o horário REAL que chega num poll DEPOIS da estimativa '≈' não pode
        disparar um 2º 'resetou' (a janela 5h ancora no 1º prompt; o real difere do ≈)."""
        self._prev(99, self.now + 7200)
        degraded = "Current session: 0% used\n" + self.WEEK_STABLE
        healthy = f"Current session: 5% used · resets {S_RESET}\n" + self.WEEK_STABLE
        # poll 1 gasta 2 fetches (principal + reconfirm, ambos degradados => estimativa ≈)
        with patch.object(tm, "_fetch_usage", side_effect=[degraded, degraded, healthy]), \
             patch.object(tm, "_notify") as notify:
            with contextlib.redirect_stdout(io.StringIO()):
                tm.meter_once(self.con, notify=False)     # poll 1: reset antecipado + estimativa ≈
                n1 = notify.call_count
                tm.meter_once(self.con, notify=False)     # poll 2: horário REAL >> estimativa, pct=5
        self.assertEqual(notify.call_count, n1)           # poll 2 NÃO alertou de novo
        sr, ev = self.con.execute(
            "SELECT session_reset, event FROM meter ORDER BY ts_epoch DESC LIMIT 1").fetchone()
        self.assertFalse(sr.startswith("≈"))              # horário real substituiu a estimativa
        self.assertIsNone(ev)                             # sem evento novo

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

    # data nominal ÚNICA, ~30 dias no futuro: longe da borda de rolagem de ano nos dois
    # fusos, então o delta entre eles é o offset puro (sem um lado rolar +1 ano).
    _base = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=30)
    DATE = f"{_base.strftime('%b')} {_base.day}"

    def test_line_timezone_is_honored(self):
        # mesmo horário nominal, fusos diferentes => epochs diferentes (offset real).
        sp = tm._reset_to_epoch(f"{self.DATE} at 3pm (America/Sao_Paulo)")   # UTC-3
        tk = tm._reset_to_epoch(f"{self.DATE} at 3pm (Asia/Tokyo)")          # UTC+9
        self.assertIsNotNone(sp)
        self.assertIsNotNone(tk)
        self.assertAlmostEqual((sp - tk) / 3600.0, 12.0, places=1)

    def test_24h_clock_equals_12h(self):
        self.assertEqual(
            tm._reset_to_epoch(f"{self.DATE} at 15:00 (America/Sao_Paulo)"),
            tm._reset_to_epoch(f"{self.DATE} at 3pm (America/Sao_Paulo)"),
        )

    def test_relative_form(self):
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc).timestamp()
        r = tm._reset_to_epoch("in 2 hours")
        self.assertIsNotNone(r)
        self.assertAlmostEqual((r - now) / 3600.0, 2.0, places=1)

    def test_unparseable_returns_none(self):
        self.assertIsNone(tm._reset_to_epoch("whenever soon"))


class GateTest(unittest.TestCase):
    """O veredito GO/PAUSE/UNKNOWN consumido pelos runners (exit 0/10/2)."""

    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.execute(CREATE_METER)
        self.now = _dt.datetime.now(_dt.timezone.utc).timestamp()

    def tearDown(self):
        self.con.close()

    def _args(self, **kw):
        base = dict(provider="claude", max_5h=80, max_week=90, max_age=300,
                    refresh=False, no_notify=True, json=False)
        base.update(kw)
        return SimpleNamespace(**base)

    def _row(self, s_pct, w_pct, age=10):
        self.con.execute(
            "INSERT INTO meter VALUES (?,?,?,?,?,?,?,?,?)",
            (f"g{age}", self.now - age, s_pct, S_RESET, tm._reset_to_epoch(S_RESET),
             w_pct, W_RESET, tm._reset_to_epoch(W_RESET), None))

    def _gate(self, args):
        with contextlib.redirect_stdout(io.StringIO()):
            return tm.gate(self.con, args)

    def test_go_below_thresholds(self):
        self._row(20, 30)
        self.assertEqual(self._gate(self._args()), tm.GATE_GO)

    def test_pause_when_5h_over(self):
        self._row(95, 30)
        self.assertEqual(self._gate(self._args()), tm.GATE_PAUSE)

    def test_pause_when_week_over(self):
        self._row(20, 95)
        self.assertEqual(self._gate(self._args()), tm.GATE_PAUSE)

    def test_unknown_without_any_reading(self):
        # banco vazio + releitura falhando => UNKNOWN (exit 2), nunca GO
        with patch.object(tm, "meter_once", return_value=False):
            self.assertEqual(self._gate(self._args()), tm.GATE_UNKNOWN)

    def test_both_pauses_when_codex_over(self):
        self._row(20, 30)                                  # claude ok
        codex = {"ts_epoch": self.now - 60, "session_pct": 99,
                 "session_reset_epoch": self.now + 3600,
                 "week_pct": 50, "week_reset_epoch": self.now + 86400, "plan": "plus"}
        with patch.object(tm, "read_codex_meter", return_value=codex):
            self.assertEqual(self._gate(self._args(provider="both")), tm.GATE_PAUSE)

    def test_codex_reading_zeroes_only_after_crossed_reset(self):
        # reset POSTERIOR ao snapshot e já vencido => janela recuperada (~0%)
        crossed = {"ts_epoch": self.now - 7200, "session_pct": 95,
                   "session_reset_epoch": self.now - 3600,   # ts < re <= now
                   "week_pct": 50, "week_reset_epoch": self.now + 86400, "plan": "plus"}
        with patch.object(tm, "read_codex_meter", return_value=crossed):
            rd = tm._codex_reading(self._args(provider="codex"))
        self.assertEqual(rd["session_pct"], 0.0)
        # resets_at já vencido NO snapshot (re <= ts) = ruído do CLI, NÃO zera (senão
        # o gate liberaria GO com uma leitura de 95% ainda válida)
        stale_re = {"ts_epoch": self.now - 60, "session_pct": 95,
                    "session_reset_epoch": self.now - 3600,   # re < ts
                    "week_pct": 50, "week_reset_epoch": self.now + 86400, "plan": "plus"}
        with patch.object(tm, "read_codex_meter", return_value=stale_re):
            rd = tm._codex_reading(self._args(provider="codex"))
        self.assertEqual(rd["session_pct"], 95)

    def test_stale_reading_with_failed_refresh_reports_real_age(self):
        # releitura falha => a leitura velha NÃO pode virar 'ao vivo'/age=0 (falso GO fresco)
        self._row(20, 30, age=4000)
        with patch.object(tm, "meter_once", return_value=False):
            rd = tm._claude_reading(self.con, self._args())
        self.assertFalse(rd["refreshed"])
        self.assertGreaterEqual(rd["age_seconds"], 3999)
        self.assertIn("cache", rd["source"])


class CreditsTest(unittest.TestCase):
    """credits_started (💳 gasto real além da cota) e credit_episodes/billing."""

    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.execute(CREATE_METER)
        self.con.execute("CREATE TABLE usage (ts_epoch REAL, output_tokens INTEGER, model TEXT)")
        self.now = _dt.datetime.now(_dt.timezone.utc).timestamp()

    def tearDown(self):
        self.con.close()

    def _meter_row(self, ts_epoch, pct):
        self.con.execute(
            "INSERT INTO meter VALUES (?,?,?,?,?,?,?,?,?)",
            (f"m{ts_epoch}", ts_epoch, pct, S_RESET, tm._reset_to_epoch(S_RESET),
             50, W_RESET, tm._reset_to_epoch(W_RESET), None))

    def test_credits_started_fires_once(self):
        # cap confirmado (90 -> 100) + token real DEPOIS do 1º 100% => 💳 uma vez, sem repetir
        self._meter_row(self.now - 600, 90)
        self._meter_row(self.now - 300, 100)
        self.con.execute("INSERT INTO usage VALUES (?,?,?)", (self.now - 100, 500, "claude-opus-4-8"))
        usage = f"Current session: 100% used · resets {S_RESET}\nCurrent week (all models): 50% used · resets {W_RESET}"
        with patch.object(tm, "_fetch_usage", return_value=usage), \
             patch.object(tm, "ingest"), \
             patch.object(tm, "_notify") as notify:
            with contextlib.redirect_stdout(io.StringIO()):
                tm.meter_once(self.con, notify=False)
                ev = self.con.execute("SELECT event FROM meter ORDER BY ts_epoch DESC LIMIT 1").fetchone()[0]
                tm.meter_once(self.con, notify=False)     # re-poll: NÃO repete
        self.assertIn("credits_started", ev or "")
        titles = " ".join(c.args[0] for c in notify.call_args_list)
        self.assertEqual(titles.count("crédito"), 1)

    def test_credit_episodes_midpoint_and_token_gate(self):
        # streak 100% com token real => 1 episódio com cap_end no PONTO MÉDIO do gap
        self._meter_row(1000, 90)
        self._meter_row(1300, 100)
        self._meter_row(1600, 100)
        self._meter_row(1900, 40)
        self.con.execute("INSERT INTO usage VALUES (?,?,?)", (1500, 100, "claude-opus-4-8"))
        eps = tm.credit_episodes(self.con)
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0], (1300, (1600 + 1900) / 2.0))
        # sem token real no cap => nenhum episódio
        self.con.execute("DELETE FROM usage")
        self.assertEqual(tm.credit_episodes(self.con), [])


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
