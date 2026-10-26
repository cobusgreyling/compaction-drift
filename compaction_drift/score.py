"""Fold kept facts. Compaction drifts when the final answer changes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

MODES = ("no-compaction", "drop-tools", "head-only", "summary")


@dataclass(frozen=True)
class Turn:
    role: str
    text: str
    sets: tuple[tuple[str, str], ...]
    ask: bool


@dataclass(frozen=True)
class Session:
    title: str
    ask: tuple[str, ...]
    head_turns: int
    turns: tuple[Turn, ...]


@dataclass(frozen=True)
class Summary:
    text: str
    sets: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ModeResult:
    mode: str
    tokens: int
    answer: tuple[tuple[str, str], ...]
    matches: bool


@dataclass(frozen=True)
class Report:
    session_path: str
    summary_path: str
    title: str
    modes: tuple[ModeResult, ...]


def score_paths(session_path: Path, summary_path: Path) -> Report:
    session = _load_session(session_path)
    summary = _load_summary(summary_path, session.ask)
    question = session.turns[-1]
    prior = session.turns[:-1]
    if not (0 < session.head_turns < len(prior)):
        raise ValueError(f"{session_path}: head_turns must drop at least one turn before the question")
    head = prior[: session.head_turns]
    summary_turn = Turn("summary", summary.text, summary.sets, False)
    kept = {
        "no-compaction": prior + (question,),
        "drop-tools": tuple(turn for turn in prior if turn.role != "tool") + (question,),
        "head-only": head + (question,),
        "summary": head + (summary_turn, question),
    }
    reference = _answer(kept["no-compaction"], session.ask)
    modes = tuple(_mode_result(mode, kept[mode], session.ask, reference) for mode in MODES)
    return Report(str(session_path), str(summary_path), session.title, modes)


def format_table(report: Report) -> str:
    reference = _reference(report)
    header = ("mode", "tokens kept", "matches", "drifted facts", "final answer")
    rows = [
        (
            mode.mode,
            str(mode.tokens),
            "yes" if mode.matches else "no",
            _drifted(mode, reference),
            _format_answer(mode.answer),
        )
        for mode in report.modes
    ]
    widths = [max(len(header[index]), *(len(row[index]) for row in rows)) for index in range(5)]

    def line(cells: tuple[str, ...]) -> str:
        parts = []
        for index, cell in enumerate(cells):
            if index == len(cells) - 1:
                parts.append(cell)
            elif index == 1:
                parts.append(cell.rjust(widths[index]))
            else:
                parts.append(cell.ljust(widths[index]))
        return "  ".join(parts)

    body = [line(header), line(tuple("-" * width for width in widths))]
    body.extend(line(row) for row in rows)
    note = "Tokens are whitespace-separated words in the text that mode keeps."
    return "\n".join(body) + "\n\n" + note


def format_details(report: Report) -> str:
    reference = _reference(report)
    blocks = [report.title]
    for mode in report.modes:
        if mode.mode == "no-compaction":
            continue
        lines = [mode.mode]
        lines.extend(_drift_lines(mode, reference) or ["  no drift"])
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _mode_result(
    mode: str,
    turns: tuple[Turn, ...],
    ask: tuple[str, ...],
    reference: tuple[tuple[str, str], ...],
) -> ModeResult:
    answer = _answer(turns, ask)
    return ModeResult(
        mode=mode,
        tokens=sum(_tokens(turn.text) for turn in turns),
        answer=answer,
        matches=answer == reference,
    )


def _reference(report: Report) -> ModeResult:
    return next(mode for mode in report.modes if mode.mode == "no-compaction")


def _drifted(mode: ModeResult, reference: ModeResult) -> str:
    names = [key for key, _, _ in _fact_deltas(mode, reference)]
    return ", ".join(names) if names else "none"


def _drift_lines(mode: ModeResult, reference: ModeResult) -> list[str]:
    return [f"  {key} {value} != {ref}" for key, value, ref in _fact_deltas(mode, reference)]


def _fact_deltas(mode: ModeResult, reference: ModeResult) -> list[tuple[str, str, str]]:
    return [
        (key, value, ref)
        for (key, value), (_, ref) in zip(mode.answer, reference.answer, strict=True)
        if value != ref
    ]


def _answer(turns: tuple[Turn, ...], ask: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    state: dict[str, str] = {}
    for turn in turns:
        state.update(turn.sets)
    return tuple((key, state.get(key, "missing")) for key in ask)


def _format_answer(answer: tuple[tuple[str, str], ...]) -> str:
    return " ".join(f"{key}={value}" for key, value in answer)


def _tokens(text: str) -> int:
    return len(text.split())


def _load_session(path: Path) -> Session:
    records = _records(path)
    if not records or not isinstance(records[0], dict) or records[0].get("type") != "session":
        raise ValueError(f"{path}: first record must be type session")
    header = records[0]
    title = header.get("title")
    ask = header.get("ask")
    head_turns = header.get("head_turns")
    if not isinstance(title, str) or not title:
        raise ValueError(f"{path}: session title must be a string")
    if (
        not isinstance(ask, list)
        or not ask
        or any(not isinstance(key, str) or not key for key in ask)
        or len(ask) != len(set(ask))
    ):
        raise ValueError(f"{path}: ask must be unique strings")
    if isinstance(head_turns, bool) or not isinstance(head_turns, int) or head_turns < 1:
        raise ValueError(f"{path}: head_turns must be a positive integer")
    turns = tuple(_turn(record, path, tuple(ask)) for record in records[1:])
    if len(turns) < 2 or not turns[-1].ask or any(turn.ask for turn in turns[:-1]):
        raise ValueError(f"{path}: the last turn must be the only question")
    return Session(title, tuple(ask), head_turns, turns)


def _turn(record: object, path: Path, ask: tuple[str, ...]) -> Turn:
    if not isinstance(record, dict) or record.get("type") != "turn":
        raise ValueError(f"{path}: expected a turn record")
    role = record.get("role")
    text = record.get("text")
    if role not in {"user", "agent", "tool"}:
        raise ValueError(f"{path}: turn role must be user, agent, or tool")
    if not isinstance(text, str) or not text.split():
        raise ValueError(f"{path}: turn text must contain words")
    is_ask = record.get("ask") is True
    sets = _sets(record.get("set"), path, ask)
    if is_ask and sets:
        raise ValueError(f"{path}: the question cannot set facts")
    return Turn(str(role), text, sets, is_ask)


def _load_summary(path: Path, ask: tuple[str, ...]) -> Summary:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON") from exc
    if not isinstance(record, dict):
        raise ValueError(f"{path}: summary must be an object")
    text = record.get("text")
    if not isinstance(text, str) or not text.split():
        raise ValueError(f"{path}: summary text must contain words")
    return Summary(text, _sets(record.get("set"), path, ask))


def _sets(value: object, path: Path, ask: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, dict):
        raise ValueError(f"{path}: set must be an object")
    pairs: list[tuple[str, str]] = []
    for key, item in value.items():
        if key not in ask:
            raise ValueError(f"{path}: set key {key} is not in ask")
        if not isinstance(item, str) or not item:
            raise ValueError(f"{path}: set value for {key} must be a string")
        pairs.append((str(key), item))
    return tuple(pairs)


def _records(path: Path) -> list[object]:
    try:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        raise
    if not lines:
        raise ValueError(f"{path}: empty session")
    try:
        return [json.loads(line) for line in lines]
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON") from exc
