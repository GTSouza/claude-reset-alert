"""Ingest dos rollouts do Codex: cwd/título, deltas por modelo (codex_model_usage)
e migração de bancos antigos (reparse único via updated_epoch=0)."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cnp import token_monitor as tm

SID = "019e3bf9-ccbf-73e2-a04c-eefebd2aba34"


def _tc(totals):
    return {"timestamp": "2026-07-01T10:05:00.000Z", "type": "event_msg",
            "payload": {"type": "token_count", "info": {
                "total_token_usage": dict(zip(
                    ("input_tokens", "cached_input_tokens", "output_tokens",
                     "reasoning_output_tokens", "total_tokens"), totals))}}}


ROLLOUT_LINES = [
    {"timestamp": "2026-07-01T10:00:00.000Z", "type": "session_meta",
     "payload": {"id": SID, "cwd": "/Users/x/Workspace/meu-app"}},
    {"timestamp": "2026-07-01T10:00:01.000Z", "type": "turn_context",
     "payload": {"model": "gpt-5.5", "cwd": "/Users/x/Workspace/meu-app"}},
    {"timestamp": "2026-07-01T10:00:02.000Z", "type": "response_item",
     "payload": {"type": "message", "role": "user", "content": [
         {"type": "input_text", "text": "<environment_context>ruído</environment_context>"},
         {"type": "input_text", "text": "Audita   o repositório\ne propõe melhorias"}]}},
    _tc((100, 10, 20, 5, 135)),
    {"timestamp": "2026-07-01T10:10:00.000Z", "type": "turn_context",
     "payload": {"model": "gpt-6"}},                       # troca de modelo no meio
    _tc((300, 30, 60, 15, 405)),
    _tc((50, 5, 10, 2, 67)),                               # contador REINICIOU (delta<0)
]


class CodexIngestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.sessions = root / "sessions"
        day = self.sessions / "2026" / "07" / "01"
        day.mkdir(parents=True)
        self.rollout = day / f"rollout-2026-07-01T10-00-00-{SID}.jsonl"
        self.rollout.write_text("\n".join(json.dumps(l) for l in ROLLOUT_LINES) + "\n")
        self.db = root / "db.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def _connect(self) -> sqlite3.Connection:
        with patch.object(tm, "DB_PATH", self.db):
            con = tm.db_connect()
        self.addCleanup(con.close)
        return con

    def _ingest(self, con) -> int:
        with patch.object(tm, "CODEX_SESSIONS_DIR", self.sessions):
            return tm.ingest_codex(con)

    def test_ingest_extrai_cwd_titulo_e_totais(self):
        con = self._connect()
        self.assertEqual(self._ingest(con), 1)
        sid, cwd, title, i, tot = con.execute(
            "SELECT session_id, cwd, title, input_tokens, total_tokens FROM codex_usage").fetchone()
        self.assertEqual(sid, SID)
        self.assertEqual(cwd, "/Users/x/Workspace/meu-app")
        self.assertEqual(title, "Audita o repositório e propõe melhorias")  # pula o <environment_context>
        self.assertEqual((i, tot), (50, 67))               # último cumulativo (pós-reinício)

    def test_deltas_por_modelo_com_troca_e_reinicio(self):
        con = self._connect()
        self._ingest(con)
        rows = dict((m, (i, o, r, t)) for m, i, o, r, t in con.execute(
            "SELECT model, input_tokens, output_tokens, reasoning_output_tokens, "
            "total_tokens FROM codex_model_usage"))
        self.assertEqual(rows["gpt-5.5"], (100, 20, 5, 135))
        # gpt-6: delta normal (200/40/10/270) + reinício de contador soma o valor cheio (50/10/2/67)
        self.assertEqual(rows["gpt-6"], (250, 50, 12, 337))

    def test_reingestao_nao_duplica_codex_model_usage(self):
        con = self._connect()
        self._ingest(con)
        con.execute("UPDATE codex_usage SET updated_epoch = 0")   # força reparse
        self._ingest(con)
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM codex_model_usage").fetchone()[0], 2)

    def test_migracao_de_banco_antigo_forca_reparse(self):
        con = sqlite3.connect(self.db)
        con.execute("""CREATE TABLE codex_usage (
            rollout TEXT PRIMARY KEY, session_id TEXT, started_epoch REAL, ended_epoch REAL,
            model TEXT, input_tokens INTEGER, cached_input_tokens INTEGER, output_tokens INTEGER,
            reasoning_output_tokens INTEGER, total_tokens INTEGER, updated_epoch REAL)""")
        con.execute("INSERT INTO codex_usage VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (str(self.rollout), SID, 1.0, 2.0, "gpt-5.5", 1, 1, 1, 1, 1, 9e12))
        con.commit()
        con.close()
        con = self._connect()                              # migra: +cwd/title, updated_epoch=0
        self.assertEqual(self._ingest(con), 1)             # mtime antigo não bloqueia o backfill
        cwd, title = con.execute("SELECT cwd, title FROM codex_usage").fetchone()
        self.assertEqual(cwd, "/Users/x/Workspace/meu-app")
        self.assertTrue(title.startswith("Audita"))


if __name__ == "__main__":
    unittest.main()
