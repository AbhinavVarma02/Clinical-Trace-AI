"""Local durable LangGraph state, distinct from identifier-only diagnostic traces.

Only explicit application dataclasses may be revived. No pickle fallback or
arbitrary module imports. The adapter owns its schema and uses a separate
SQLite connection so LangGraph worker writes cannot commit app transactions.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import sqlite3
from pathlib import Path
from typing import Any

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from src.agent.composer import Draft
from src.agent.planner import Plan
from src.agent.tools import ToolResult
from src.config import PROJECT_ROOT
from src.safety.guards import GuardResult
from src.safety.verifier import EvidenceItem


def open_checkpoints(path: str) -> SqliteSaver:
    serializer = JsonPlusSerializer(
        pickle_fallback=False,
        allowed_msgpack_modules=[Draft, Plan, ToolResult, GuardResult, EvidenceItem],
    )
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    saver = SqliteSaver(conn, serde=serializer)
    saver.setup()
    return saver


def execution_revision(provider: Any, model_available: bool) -> str:
    """Prevent resuming checkpoints after code or inference configuration changes."""
    digest = hashlib.sha256()
    for package in ("langgraph", "langgraph-checkpoint", "langgraph-checkpoint-sqlite", "langchain-core", "pydantic"):
        digest.update(f"{package}:{importlib.metadata.version(package)}".encode())
    for folder in ("src/agent", "src/safety", "src/clinical", "src/retrieval"):
        for path in sorted((PROJECT_ROOT / folder).glob("*.py")):
            digest.update(path.relative_to(PROJECT_ROOT).as_posix().encode())
            digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    if provider:
        digest.update(f"{provider.name}:{provider.model}:{provider.base_url}".encode())
    digest.update(str(model_available).encode())
    if model_available:
        # A changed model must never mix with earlier risk evidence in a resumed run.
        from src import config

        for path in (config.MODEL_PATH, config.PIPELINE_PATH, config.MODEL_METADATA_PATH):
            if Path(path).exists():
                digest.update(Path(path).read_bytes())
    return digest.hexdigest()
