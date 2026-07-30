"""Fixture builders shared by every test module.

Hand-built tiny JSONL records that mirror the real Claude Code transcript
format (see ccmetrics/ingest.py header notes for the verified field names).
No real ~/.claude data is ever read here -- everything is synthetic.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path


def iso(dt: _dt.datetime) -> str:
    """corpus timestamp shape: 2026-07-07T20:41:46.723Z"""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def ts_at(base: _dt.datetime, seconds: float) -> str:
    return iso(base + _dt.timedelta(seconds=seconds))


# --- record builders (dicts; caller serializes) ------------------------------


def assistant_rec(
    session_id: str,
    msg_id: str,
    ts: str,
    model: str | None,
    *,
    cw5m: int = 0,
    cw1h: int = 0,
    cread: int = 0,
    raw_in: int = 0,
    raw_out: int = 0,
    text: str | None = None,
    tool_uses: list[dict] | None = None,
    sidechain: bool = False,
    version: str = "1.0.5",
) -> dict:
    content: list[dict] = []
    if text is not None:
        content.append({"type": "text", "text": text})
    for tu in tool_uses or []:
        content.append(
            {"type": "tool_use", "id": tu["id"], "name": tu["name"], "input": tu.get("input", {})}
        )
    return {
        "type": "assistant",
        "sessionId": session_id,
        "timestamp": ts,
        "version": version,
        "isSidechain": sidechain,
        "message": {
            "id": msg_id,
            "model": model,
            "content": content,
            "usage": {
                "input_tokens": raw_in,
                "output_tokens": raw_out,
                "cache_creation_input_tokens": cw5m + cw1h,
                "cache_read_input_tokens": cread,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": cw5m,
                    "ephemeral_1h_input_tokens": cw1h,
                },
            },
        },
    }


def user_prompt_rec(session_id: str, ts: str, text: str) -> dict:
    return {
        "type": "user",
        "sessionId": session_id,
        "timestamp": ts,
        "message": {"content": text},
    }


def tool_result_rec(
    session_id: str,
    ts: str,
    tool_use_id: str,
    content,
    *,
    is_error: bool = False,
    denial_kind: str | None = None,
) -> dict:
    rec = {
        "type": "user",
        "sessionId": session_id,
        "timestamp": ts,
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": content,
                    "is_error": is_error,
                }
            ]
        },
    }
    if denial_kind:
        rec["toolDenialKind"] = denial_kind
    return rec


def compact_rec(
    session_id: str, ts: str, pre_tokens: int, post_tokens: int = 0, dropped: int = 0,
    trigger: str = "auto",
) -> dict:
    return {
        "type": "system",
        "subtype": "compact_boundary",
        "sessionId": session_id,
        "timestamp": ts,
        "compactMetadata": {
            "preTokens": pre_tokens,
            "postTokens": post_tokens,
            "cumulativeDroppedTokens": dropped,
            "trigger": trigger,
        },
    }


def hook_rec(session_id: str, ts: str, errors: list[str] | None = None, prevented: bool = False) -> dict:
    return {
        "type": "system",
        "subtype": "stop_hook_summary",
        "sessionId": session_id,
        "timestamp": ts,
        "hookErrors": errors or [],
        "preventedContinuation": prevented,
    }


# --- serialization / file IO -------------------------------------------------


def dumps(rec: dict) -> str:
    # compact separators: the byte prefilter in ingest.py matches literal
    # substrings like b'"type":"user"' -- no space after the colon.
    return json.dumps(rec, separators=(",", ":"))


def write_lines(path: Path, recs: list[dict]) -> None:
    with open(path, "ab") as f:
        for r in recs:
            f.write(dumps(r).encode("utf-8"))
            f.write(b"\n")


def write_raw(path: Path, data: bytes) -> None:
    with open(path, "ab") as f:
        f.write(data)


def make_project(projects_dir: Path, key: str = "-Users-test-proj") -> Path:
    d = projects_dir / key
    d.mkdir(parents=True, exist_ok=True)
    return d


def session_path(project_dir: Path, session_id: str = "sess1") -> Path:
    return project_dir / f"{session_id}.jsonl"
