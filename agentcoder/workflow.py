"""Sequential orchestration, shared state, branches, bounded repair loops and recovery."""

from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .agents import CodingAgent, RepairAgent, ReviewAgent
from .analysis import analyze_source
from .errors import AgentError
from .graph import validate_graph
from .report import make_report
from .state import RunStore, timestamp
from .testing import execute_tests


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Cancelled(AgentError):
    pass


class AgentRegistry:
    def __init__(self):
        self.agents = {}

    def register(self, name, agent):
        if not hasattr(agent, "run"):
            raise AgentError("Agent 必须实现 run(state, context)。")
        self.agents[name] = agent
        return self

    def get(self, name):
        if name not in self.agents:
            raise AgentError(f"Agent {name} 未注册。")
        return self.agents[name]


class WorkflowRunner:
    def __init__(self, store: RunStore, *, provider=None, registry=None, test_timeout=10,
                 on_event=None, cancelled=None):
        self.store, self.provider, self.test_timeout = store, provider, test_timeout
        self.registry = registry or (AgentRegistry().register("coding", CodingAgent(provider))
            .register("review", ReviewAgent(provider)).register("repair", RepairAgent(provider)))
        self.on_event = on_event
        self.cancelled = cancelled or threading.Event()
        self.state = None
        self._last_activity = 0.0

    def check_cancelled(self):
        if self.cancelled.is_set():
            raise Cancelled("已停止，检查点已保存，可以继续当前节点。")

    def emit(self, kind, message, *, agent=None, data=None):
        state = self.state
        event = {"seq": len(state.events) + 1, "time": timestamp(), "kind": kind,
                 "agent": agent, "message": message, "data": data or {}, "cycle": state.cycle}
        state.events.append(event)
        self.store.save(state)
        if self.on_event:
            self.on_event(event)

    def activity(self, agent):
        self.check_cancelled()
        now = time.monotonic()
        if now - self._last_activity >= 5:
            self.emit("activity", "模型服务正在响应，当前 Agent 正在处理任务。", agent=agent)
            self._last_activity = now

    def materialize(self):
        for name, text in self.state.artifacts.items():
            self.store.write(self.state.id, name, text)

    def inspect(self, source, state):
        self.check_cancelled()
        self.materialize()
        self.emit("validation", "并行进行 Python AST 检查和回归测试。", agent=state.phase,
                  data={"allow_exec": state.allow_exec})
        with ThreadPoolExecutor(max_workers=2) as pool:
            syntax = pool.submit(analyze_source, source, "solution.py")
            tests = pool.submit(execute_tests, self.store.directory(state.id),
                                allowed=state.allow_exec, timeout=self.test_timeout)
            static, test_result = syntax.result(), tests.result()
        self.check_cancelled()
        self.emit("validation_result", f"静态候选 {len(static['findings'])} 项；测试状态 {test_result['status']}。",
                  agent=state.phase, data={"tests": test_result, "static_count": len(static["findings"])})
        return static, test_result

    def run(self, state):
        state.validate()
        if state.status in {"completed", "exhausted"}:
            return state
        if state.mode == "llm" and self.provider is None:
            raise AgentError("真实模式需要已配置的模型。")
        self.state = state
        mapping = validate_graph(state.graph)
        state.status, state.error = "running", ""
        self.emit("workflow_start", "工作流已开始，从当前检查点继续。", data={"id": state.id, "phase": state.phase})
        visits = 0
        try:
            while state.phase != "done":
                self.check_cancelled()
                if visits >= 40:
                    state.status = "exhausted"
                    self.emit("limit", "流程节点执行次数达到上限。")
                    break
                visits += 1
                node_id = state.phase
                node = mapping[node_id]
                state.nodes[node_id] = "running"
                self.emit("node_start", f"{node_id} Agent 开始执行。", agent=node_id)
                result = self.registry.get(node["agent"]).run(state, self)
                self.check_cancelled()
                if node["kind"] == "code":
                    state.artifacts = {"solution.py": result["solution"], "test_solution.py": result["tests"]}
                    state.baseline_tests = result["tests"]
                    state.message(node_id, node["next"], "code_ready", {
                        "files": list(state.artifacts), "code_sha256": digest(result["solution"]),
                        "tests_sha256": digest(state.baseline_tests), "summary": result["explanation"]})
                    next_phase = node["next"]
                elif node["kind"] == "review":
                    state.reviews.append(result)
                    next_phase = node["next"] if result["approved"] else node["revise"]
                    state.message(node_id, next_phase, "review_result", {
                        "approved": result["approved"], "issues": result["issues"], "test_status": result["tests"]["status"],
                        "summary": result["summary"], "code_sha256": digest(state.artifacts["solution.py"])})
                    self.emit("branch", "审查通过，进入下一节点。" if result["approved"] else "发现问题，进入修复分支。",
                              agent=node_id, data={"approved": result["approved"], "next": next_phase})
                    if not result["approved"] and (state.cycle >= state.max_cycles or next_phase == "done"):
                        state.status = "exhausted"
                        state.nodes[node_id] = "completed"
                        self.emit("limit", "达到修复轮次上限，保留未解决问题。", agent=node_id)
                        break
                elif node["kind"] == "repair":
                    before = digest(state.artifacts["solution.py"])
                    state.artifacts["solution.py"] = result["solution"]
                    state.cycle += 1
                    state.message(node_id, node["next"], "repair_ready", {
                        "summary": result["explanation"], "previous_code_sha256": before,
                        "code_sha256": digest(result["solution"]), "tests_sha256": digest(state.baseline_tests)})
                    next_phase = node["next"]
                else:
                    state.message(node_id, node["next"], "observation", result)
                    next_phase = node["next"]
                state.nodes[node_id] = "completed"
                state.phase = next_phase
                self.materialize()
                self.emit("node_done", f"{node_id} Agent 已交付，消息已传给 {next_phase}。", agent=node_id,
                          data={"next": next_phase, "message_count": len(state.messages), "nodes": state.nodes.copy()})
            if state.phase == "done":
                state.status = "completed"
                for name in state.nodes:
                    if state.nodes[name] == "pending":
                        state.nodes[name] = "skipped"
                self.emit("workflow_done", "流程完成：代码、审查证据与交付报告已生成。", data={"nodes": state.nodes.copy()})
        except (BrokenPipeError, ConnectionResetError):
            state.status = "cancelled"
            state.error = "客户端连接已关闭，检查点已保存。"
            state.nodes[state.phase] = "pending"
            self.store.save(state)
            raise
        except Exception as exc:
            state.status = "cancelled" if isinstance(exc, Cancelled) else "failed"
            key = getattr(getattr(self.provider, "config", None), "api_key", "")
            state.error = str(exc).replace(key, "[已隐藏密钥]") if key else str(exc)
            if state.phase in state.nodes:
                state.nodes[state.phase] = "failed"
            self.emit("workflow_error", state.error, agent=state.phase)
        self.store.save(state)
        self.store.write(state.id, "report.md", make_report(state))
        return state
