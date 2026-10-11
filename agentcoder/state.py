"""Shared workflow state, ordered messages and atomic durable checkpoints."""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .errors import AgentError
from .graph import default_graph, validate_graph


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class WorkflowState:
    id: str
    task: str
    mode: str
    max_cycles: int = 3
    allow_exec: bool = False
    scenario: str = "buggy"
    initial_source: str = ""
    graph: dict = field(default_factory=default_graph)
    status: str = "created"
    phase: str = "coding"
    cycle: int = 0
    nodes: dict = field(default_factory=lambda: {"coding": "pending", "review": "pending", "repair": "pending"})
    artifacts: dict = field(default_factory=dict)
    baseline_tests: str = ""
    reviews: list = field(default_factory=list)
    messages: list = field(default_factory=list)
    events: list = field(default_factory=list)
    error: str = ""
    created_at: str = field(default_factory=timestamp)
    updated_at: str = field(default_factory=timestamp)
    version: int = 1

    @classmethod
    def create(cls, task, mode="demo", **options):
        if not isinstance(task, str) or not 1 <= len(task.strip()) <= 4000:
            raise AgentError("任务必须是 1 至 4000 个字符。")
        if mode not in {"demo", "llm"}:
            raise AgentError("请选择真实模型或离线演示。")
        state = cls(uuid.uuid4().hex, task.strip(), mode, **options)
        state.phase = state.graph.get("entry", "coding")
        state.nodes = {node["id"]: "pending" for node in state.graph.get("nodes", [])}
        state.validate()
        return state

    def validate(self):
        graph_nodes = validate_graph(self.graph)
        if (not isinstance(self.id, str) or not re.fullmatch(r"[a-f0-9]{32}", self.id)
                or self.version != 1 or self.mode not in {"demo", "llm"}
                or self.phase not in {*graph_nodes, "done"}
                or self.status not in {"created", "running", "completed", "failed", "exhausted", "cancelled"}
                or type(self.max_cycles) is not int or not 1 <= self.max_cycles <= 5
                or type(self.cycle) is not int or not 0 <= self.cycle <= self.max_cycles
                or type(self.allow_exec) is not bool or self.scenario not in {"buggy", "clean"}
                or not isinstance(self.initial_source, str) or len(self.initial_source.encode()) > 65536
                or not isinstance(self.task, str) or not 1 <= len(self.task) <= 4000):
            raise AgentError("工作流状态或运行参数无效。")
        if (not isinstance(self.nodes, dict) or set(self.nodes) != set(graph_nodes)
                or any(v not in {"pending", "running", "completed", "failed", "skipped"} for v in self.nodes.values())
                or not isinstance(self.artifacts, dict) or set(self.artifacts) - {"solution.py", "test_solution.py"}
                or any(not isinstance(v, str) or len(v.encode()) > 65536 for v in self.artifacts.values())
                or not isinstance(self.baseline_tests, str)
                or not all(isinstance(v, list) for v in (self.reviews, self.messages, self.events))):
            raise AgentError("工作流数据格式无效。")
        if self.baseline_tests and self.artifacts.get("test_solution.py") != self.baseline_tests:
            raise AgentError("原始测试被修改，不能继续工作流。")

    def public(self):
        return asdict(self)

    def message(self, sender, recipient, kind, payload):
        item = {"seq": len(self.messages) + 1, "time": timestamp(), "sender": sender,
                "recipient": recipient, "kind": kind, "payload": payload}
        self.messages.append(item)
        return item


class RunStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.folder = self.root / ".agentcoder" / "runs"

    def directory(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise AgentError("运行 ID 无效。")
        folder = self.folder / run_id
        try:
            folder.resolve().relative_to(self.root)
        except (ValueError, OSError, RuntimeError) as exc:
            raise AgentError("运行目录超出项目范围。") from exc
        return folder

    def save(self, state):
        state.validate()
        state.updated_at = timestamp()
        self.write(state.id, "state.json", json.dumps(state.public(), ensure_ascii=False, indent=2))

    def write(self, run_id, name, text):
        if name not in {"state.json", "report.md", "solution.py", "test_solution.py"}:
            raise AgentError("输出文件名无效。")
        folder = self.directory(run_id)
        folder.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=folder, delete=False) as f:
                temporary = Path(f.name)
                f.write(text)
            os.replace(temporary, folder / name)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()

    def load(self, run_id):
        path = self.directory(run_id) / "state.json"
        try:
            if path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("oversized checkpoint")
            state = WorkflowState(**json.loads(path.read_text(encoding="utf-8")))
            state.validate()
            return state
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
            raise AgentError("无法读取工作流检查点。") from exc

    def history(self):
        if not self.folder.exists():
            return []
        rows = []
        for folder in sorted(self.folder.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:50]:
            try:
                state = self.load(folder.name)
                rows.append({k: getattr(state, k) for k in ("id", "task", "mode", "status", "phase", "cycle", "updated_at")})
            except AgentError:
                continue
        return rows
