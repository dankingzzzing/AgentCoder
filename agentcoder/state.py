"""Shared workflow state, ordered messages and atomic durable checkpoints."""

from __future__ import annotations

import json
import hashlib
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
    example: str = "contracts"
    initial_source: str = ""
    initial_tests: str = ""
    parent_run_id: str = ""
    origin: str = "new"
    initial_source_from: dict = field(default_factory=dict)
    inherited_tests: list = field(default_factory=list)
    test_module: str = "solution"
    graph: dict = field(default_factory=default_graph)
    status: str = "created"
    phase: str = "coding"
    cycle: int = 0
    nodes: dict = field(default_factory=lambda: {"coding": "pending", "review": "pending", "repair": "pending"})
    artifacts: dict = field(default_factory=dict)
    baseline_tests: str = ""
    reviews: list = field(default_factory=list)
    revisions: list = field(default_factory=list)
    messages: list = field(default_factory=list)
    events: list = field(default_factory=list)
    error: str = ""
    error_code: str = ""
    workdir: str = ""
    drafts: dict = field(default_factory=dict)
    progress: dict = field(default_factory=dict)
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
        from .inputs import normalize_test_input
        normalize_test_input(state)
        state.validate()
        return state

    def validate(self):
        from .fixtures import get_case
        from .inputs import module_name
        get_case(self.example)
        module_name(self.test_module)
        if (self.origin not in {"new", "continue"} or not isinstance(self.parent_run_id, str)
                or not isinstance(self.initial_source_from, dict) or not isinstance(self.inherited_tests, list)
                or len(self.inherited_tests) > 16):
            raise AgentError("运行继承信息无效。")
        if self.origin == "new":
            if self.parent_run_id or self.initial_source_from or self.inherited_tests:
                raise AgentError("新运行不能携带未确认的父运行信息。")
        elif (not re.fullmatch(r"[a-f0-9]{32}", self.parent_run_id) or self.parent_run_id == self.id
                or not self.initial_source or self.initial_source_from.get("run_id") != self.parent_run_id
                or set(self.initial_source_from) != {"run_id", "version", "code_sha256"}
                or (self.initial_source_from["version"] is not None and
                    (type(self.initial_source_from["version"]) is not int or self.initial_source_from["version"] < 0))
                or self.initial_source_from["code_sha256"] != hashlib.sha256(self.initial_source.encode()).hexdigest()):
            raise AgentError("继承源码与父运行版本不一致。")
        for item in self.inherited_tests:
            if (not isinstance(item, dict) or set(item) != {"run_id", "source", "sha256"}
                    or not isinstance(item["run_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", item["run_id"])
                    or not isinstance(item["source"], str) or not 0 < len(item["source"].encode()) <= 65536
                    or item["sha256"] != hashlib.sha256(item["source"].encode()).hexdigest()):
                raise AgentError("继承测试内容或哈希无效。")
        graph_nodes = validate_graph(self.graph)
        if (not isinstance(self.id, str) or not re.fullmatch(r"[a-f0-9]{32}", self.id)
                or self.version != 1 or self.mode not in {"demo", "llm"}
                or self.phase not in {*graph_nodes, "done"}
                or self.status not in {"created", "running", "completed", "failed", "exhausted", "cancelled"}
                or type(self.max_cycles) is not int or not 1 <= self.max_cycles <= 5
                or type(self.cycle) is not int or not 0 <= self.cycle <= self.max_cycles
                or type(self.allow_exec) is not bool or self.scenario not in {"buggy", "clean"}
                or not isinstance(self.initial_source, str) or len(self.initial_source.encode()) > 65536
                or not isinstance(self.initial_tests,str) or len(self.initial_tests.encode()) > 65536
                or not isinstance(self.task, str) or not 1 <= len(self.task) <= 4000):
            raise AgentError("工作流状态或运行参数无效。")
        if (not isinstance(self.nodes, dict) or set(self.nodes) != set(graph_nodes)
                or any(v not in {"pending", "running", "completed", "failed", "skipped"} for v in self.nodes.values())
                or not isinstance(self.artifacts, dict) or set(self.artifacts) - {"solution.py", "test_solution.py"}
                or any(not isinstance(v, str) or len(v.encode()) > 65536 for v in self.artifacts.values())
                or not isinstance(self.baseline_tests, str)
                or not isinstance(self.error_code, str) or not isinstance(self.workdir, str)
                or len(self.workdir) > 4096 or not isinstance(self.progress, dict)
                or not isinstance(self.drafts, dict) or set(self.drafts) - {"solution.py", "test_solution.py"}
                or any(not isinstance(v, str) or len(v.encode()) > 65536 for v in self.drafts.values())
                or not all(isinstance(v, list) for v in (self.reviews, self.revisions, self.messages, self.events))):
            raise AgentError("工作流数据格式无效。")
        if self.baseline_tests and self.artifacts.get("test_solution.py") != self.baseline_tests:
            raise AgentError("原始测试被修改，不能继续工作流。")
        if len(self.revisions) > self.max_cycles + 1:
            raise AgentError("代码快照数量超过修复预算。")
        for index, revision in enumerate(self.revisions):
            if (not isinstance(revision, dict) or type(revision.get("index")) is not int or revision["index"] != index
                    or type(revision.get("cycle")) is not int or not 0 <= revision["cycle"] <= self.cycle
                    or revision.get("node") not in graph_nodes or revision.get("origin") not in {"agent", "legacy_checkpoint"}
                    or not isinstance(revision.get("source"), str) or len(revision["source"].encode()) > 65536
                    or any(not isinstance(revision.get(k), str) for k in ("summary", "time"))
                    or type(revision.get("message_seq")) is not int or not 0 <= revision["message_seq"] <= len(self.messages)
                    or revision.get("code_sha256") != hashlib.sha256(revision["source"].encode()).hexdigest()
                    or revision.get("tests_sha256") != hashlib.sha256(self.baseline_tests.encode()).hexdigest()):
                raise AgentError("代码快照或测试哈希不一致，不能继续工作流。")
        if self.revisions and self.artifacts.get("solution.py") != self.revisions[-1]["source"]:
            raise AgentError("当前代码与最新快照不一致。")

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
        if name not in {"state.json", "report.md", "evidence.json", "solution.py", "test_solution.py", *(f"test_inherited_{index:02d}.py" for index in range(1, 17))}:
            from .inputs import module_name
            if not isinstance(name,str) or not name.endswith('.py'):
                raise AgentError("输出文件名无效。")
            module_name(name[:-3])
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
                row = {k: getattr(state, k) for k in ("id", "task", "mode", "status", "phase", "cycle", "updated_at", "parent_run_id", "origin")}
                row["has_source"] = bool(state.artifacts.get("solution.py"))
                rows.append(row)
            except AgentError:
                continue
        return rows
