import contextlib
import io
import sqlite3
import unittest
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


if __name__ == "__main__":
    unittest.main()
