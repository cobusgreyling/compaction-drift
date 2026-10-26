import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from compaction_drift.__main__ import main
from compaction_drift.score import score_paths

ROOT = Path(__file__).resolve().parents[1]
SESSION = ROOT / "fixtures" / "session.jsonl"
SUMMARY = ROOT / "fixtures" / "summary.json"

FULL = (
    ("model", "big"),
    ("limit_zar", "2500"),
    ("blocking_field", "incident_date"),
)
HEAD = (
    ("model", "small"),
    ("limit_zar", "500"),
    ("blocking_field", "incident_date"),
)
LOSSY = (
    ("model", "small"),
    ("limit_zar", "2500"),
    ("blocking_field", "incident_date"),
)


def _write(directory: Path, name: str, records: list[dict] | dict) -> Path:
    path = directory / name
    if isinstance(records, dict):
        path.write_text(json.dumps(records), encoding="utf-8")
    else:
        path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


class ScoreTests(unittest.TestCase):
    def test_checked_in_session_drifts(self) -> None:
        report = score_paths(SESSION, SUMMARY)
        modes = {mode.mode: mode for mode in report.modes}
        self.assertEqual(modes["no-compaction"].answer, FULL)
        self.assertTrue(modes["no-compaction"].matches)
        self.assertEqual(modes["head-only"].answer, HEAD)
        self.assertFalse(modes["head-only"].matches)
        self.assertEqual(modes["summary"].answer, LOSSY)
        self.assertFalse(modes["summary"].matches)
        self.assertEqual(modes["drop-tools"].answer, FULL)
        self.assertTrue(modes["drop-tools"].matches)
        self.assertGreater(modes["no-compaction"].tokens, modes["drop-tools"].tokens)
        self.assertGreater(modes["drop-tools"].tokens, modes["summary"].tokens)
        self.assertGreater(modes["summary"].tokens, modes["head-only"].tokens)

    def test_summary_that_keeps_the_late_fact_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = _write(
                root,
                "session.jsonl",
                [
                    {
                        "type": "session",
                        "title": "Tiny",
                        "ask": ["model"],
                        "head_turns": 1,
                    },
                    {
                        "type": "turn",
                        "role": "user",
                        "text": "Use model small.",
                        "set": {"model": "small"},
                    },
                    {
                        "type": "turn",
                        "role": "user",
                        "text": "Correction. Use model big.",
                        "set": {"model": "big"},
                    },
                    {"type": "turn", "role": "user", "text": "What model?", "ask": True},
                ],
            )
            summary = _write(
                root,
                "summary.json",
                {"text": "The correction set the model to big.", "set": {"model": "big"}},
            )
            report = score_paths(session, summary)
        modes = {mode.mode: mode for mode in report.modes}
        self.assertEqual(modes["no-compaction"].answer, (("model", "big"),))
        self.assertEqual(modes["head-only"].answer, (("model", "small"),))
        self.assertFalse(modes["head-only"].matches)
        self.assertEqual(modes["summary"].answer, (("model", "big"),))
        self.assertTrue(modes["summary"].matches)
        self.assertEqual(modes["drop-tools"].answer, (("model", "big"),))
        self.assertTrue(modes["drop-tools"].matches)
        self.assertEqual(modes["head-only"].tokens, len("Use model small.".split()) + len("What model?".split()))
        self.assertEqual(
            modes["summary"].tokens,
            modes["head-only"].tokens + len("The correction set the model to big.".split()),
        )

    def test_question_cannot_smuggle_a_fact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = _write(
                root,
                "session.jsonl",
                [
                    {"type": "session", "title": "Tiny", "ask": ["model"], "head_turns": 1},
                    {"type": "turn", "role": "user", "text": "Use model small.", "set": {"model": "small"}},
                    {
                        "type": "turn",
                        "role": "user",
                        "text": "What model?",
                        "ask": True,
                        "set": {"model": "big"},
                    },
                ],
            )
            summary = _write(root, "summary.json", {"text": "No change.", "set": {}})
            with self.assertRaises(ValueError):
                score_paths(session, summary)

    def test_drop_tools_loses_a_fact_that_only_a_tool_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = _write(
                root,
                "session.jsonl",
                [
                    {"type": "session", "title": "Tiny", "ask": ["model"], "head_turns": 1},
                    {"type": "turn", "role": "user", "text": "Use model small.", "set": {"model": "small"}},
                    {
                        "type": "turn",
                        "role": "tool",
                        "text": "A later tool sets the model.",
                        "set": {"model": "big"},
                    },
                    {"type": "turn", "role": "user", "text": "What model?", "ask": True},
                ],
            )
            summary = _write(root, "summary.json", {"text": "No change.", "set": {}})
            report = score_paths(session, summary)
        modes = {mode.mode: mode for mode in report.modes}
        self.assertEqual(modes["no-compaction"].answer, (("model", "big"),))
        self.assertEqual(modes["drop-tools"].answer, (("model", "small"),))
        self.assertFalse(modes["drop-tools"].matches)

    def test_cli_prints_the_drift(self) -> None:
        buf = StringIO()
        with redirect_stdout(buf):
            code = main([str(SESSION), str(SUMMARY), "--verbose"])
        self.assertEqual(code, 0)
        text = buf.getvalue()
        self.assertIn("no-compaction", text)
        self.assertIn("drop-tools", text)
        self.assertIn("head-only", text)
        self.assertIn("summary", text)
        self.assertIn("model, limit_zar", text)
        self.assertIn("model=big limit_zar=2500 blocking_field=incident_date", text)
        self.assertIn("model=small limit_zar=500 blocking_field=incident_date", text)
        self.assertIn("model=small limit_zar=2500 blocking_field=incident_date", text)
        self.assertIn("model small != big", text)
        self.assertIn("limit_zar 500 != 2500", text)
        self.assertIn("whitespace-separated words", text)


if __name__ == "__main__":
    unittest.main()
