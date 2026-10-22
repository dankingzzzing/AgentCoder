"""Sequential orchestration, shared state, branches, bounded repair loops and recovery."""

from __future__ import annotations

import hashlib
import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .agents import CodingAgent, RepairAgent, ReviewAgent, DeliveryAudit
from .evidence import build_evidence, record_revision
from .analysis import analyze_source
from .errors import AgentError, Cancelled
from .graph import validate_graph
from .report import make_report
from .state import RunStore, timestamp
from .testing import execute_tests
from .inputs import normalize_test_input


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
                 on_event=None, on_preview=None, cancelled=None):
        self.store, self.provider, self.test_timeout = store, provider, test_timeout
        self.registry = registry or (AgentRegistry().register("coding", CodingAgent(provider))
            .register("review", ReviewAgent(provider)).register("repair", RepairAgent(provider)).register("audit", DeliveryAudit()))
        self.on_event = on_event
        self.on_preview = on_preview
        self.cancelled = cancelled or threading.Event()
        self.state = None
        self._last_activity = 0.0
        self._last_preview = 0.0
        self._last_progress = 0.0

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
            progress = self.state.progress
            signals = progress.get("signals", 0)
            self.emit("activity", f"等待当前 Agent 交付；已收到 {signals} 次响应信号，当前输出尚未提交。可随时停止。", agent=self.state.phase)
            self._last_activity = now

    def model_progress(self, value, force=False):
        self.check_cancelled()
        if force:
            self.state.drafts = {}
            self.state.progress = {}
            if self.on_preview:
                self.on_preview({"agent":self.state.phase, "drafts":{}, "summary":"开始当前请求；此前未提交草稿仅作参考。"})
        self.state.progress.update(value)
        now = time.monotonic()
        if force or now - self._last_progress >= 1:
            self.emit("model_progress", "正在连接模型服务。" if value.get("stage") == "connecting" else
                      f"模型正在处理请求；已收到 {value.get('signals', 0)} 次响应信号、{value.get('public_chars', 0)} 个公开交付字符。", agent=self.state.phase,
                      data=self.state.progress.copy())
            self._last_progress = now

    def preview(self, values):
        self.check_cancelled()
        for field, name in (("solution", "solution.py"), ("tests", "test_solution.py")):
            if field in values:
                raw = values[field].encode("utf-8")[:65536]
                self.state.drafts[name] = raw.decode("utf-8", errors="ignore")
        if values.get("explanation") or values.get("summary"):
            self.state.progress["summary"] = (values.get("explanation") or values.get("summary"))[:1000]
        now = time.monotonic()
        if now - self._last_preview >= 1:
            self.store.save(self.state)
            if self.on_preview:
                self.on_preview({"agent": self.state.phase, "drafts": self.state.drafts.copy(), "summary": self.state.progress.get("summary", ""), "progress":self.state.progress.copy()})
            self._last_preview = now

    def materialize(self):
        for name, text in self.state.artifacts.items():
            self.store.write(self.state.id, name, text)
        if self.state.test_module != 'solution' and 'solution.py' in self.state.artifacts:
            self.store.write(self.state.id,self.state.test_module+'.py',self.state.artifacts['solution.py'])
        for index, item in enumerate(self.state.inherited_tests, 1):
            self.store.write(self.state.id, f"test_inherited_{index:02d}.py", item["source"])

    def inspect(self, source, state):
        self.check_cancelled()
        self.materialize()
        self.emit("validation", "并行进行 Python AST 检查和回归测试。", agent=state.phase,
                  data={"allow_exec": state.allow_exec})
        with ThreadPoolExecutor(max_workers=2) as pool:
            syntax = pool.submit(analyze_source, source, "solution.py")
            tests = pool.submit(execute_tests, self.store.directory(state.id),
                                allowed=state.allow_exec, timeout=self.test_timeout, cancelled=self.cancelled)
            static, test_result = syntax.result(), tests.result()
        self.check_cancelled()
        self.emit("validation_result", f"静态候选 {len(static['findings'])} 项；测试状态 {test_result['status']}。",
                  agent=state.phase, data={"tests": test_result, "static_count": len(static["findings"])})
        return static, test_result

    def run(self, state):
        if state.phase == state.graph.get('entry') and not state.artifacts:
            normalize_test_input(state)
        state.validate()
        if state.status in {"completed", "exhausted"}:
            return state
        if state.mode == "llm" and self.provider is None:
            raise AgentError("真实模式需要已配置的模型。")
        self.state = state
        mapping = validate_graph(state.graph)
        for node in mapping.values():
            self.registry.get(node["agent"])
        if state.artifacts and not state.revisions:
            record_revision(state, state.phase, "旧检查点的当前代码；此前版本未保存。", origin="legacy_checkpoint")
        state.status, state.error, state.error_code = "running", "", ""
        visits = 0
        try:
            self.emit("workflow_start", "工作流已开始，从当前检查点继续。", data={"id": state.id, "phase": state.phase})
            if state.parent_run_id and not state.artifacts:
                self.emit("run_inherited", f"继承运行 {state.parent_run_id[:8]} 的源码与 {len(state.inherited_tests)} 组回归测试；编码节点保留源码，为新增要求补充测试。",
                          data={"parent_run_id":state.parent_run_id, "initial_source_from":state.initial_source_from})
            if state.initial_tests and not state.artifacts:
                self.emit("input_detected", f"识别到用户回归测试，生成 {state.test_module} 对应实现；原始测试将冻结不修改。")
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
                state.progress = {}
                state.drafts = {}
                self.emit("node_start", f"{node_id} Agent 开始执行。", agent=node_id)
                observed = copy.deepcopy(state) if node["kind"] == "observer" else state
                observation_before = observed.public() if node["kind"] == "observer" else None
                result = self.registry.get(node["agent"]).run(observed, self)
                if node["kind"] == "observer" and observed.public() != observation_before:
                    raise AgentError("观察节点只能读取状态，不能修改代码、测试或流程。")
                self.check_cancelled()
                if node["kind"] == "code":
                    state.artifacts = {"solution.py": result["solution"], "test_solution.py": result["tests"]}
                    state.baseline_tests = result["tests"]
                    state.message(node_id, node["next"], "code_ready", {
                        "files": list(state.artifacts), "code_sha256": digest(result["solution"]),
                        "tests_sha256": digest(state.baseline_tests), "summary": result["explanation"]})
                    record_revision(state, node_id, result["explanation"])
                    next_phase = node["next"]
                elif node["kind"] == "review":
                    if not result["static"]["syntax_ok"] or result["tests"]["passed"] is False or result["issues"]:
                        result["approved"] = False
                    result["revision"] = state.revisions[-1]["index"]
                    result["code_sha256"] = digest(state.artifacts["solution.py"])
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
                        self.emit("limit", "未配置修复分支，保留未解决问题。" if next_phase == "done" else
                                  "达到修复轮次上限，保留未解决问题。", agent=node_id)
                        break
                elif node["kind"] == "repair":
                    before = digest(state.artifacts["solution.py"])
                    state.artifacts["solution.py"] = result["solution"]
                    state.cycle += 1
                    state.message(node_id, node["next"], "repair_ready", {
                        "summary": result["explanation"], "previous_code_sha256": before,
                        "code_sha256": digest(result["solution"]), "tests_sha256": digest(state.baseline_tests)})
                    record_revision(state, node_id, result["explanation"])
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
            if state.phase != "done":
                state.status = "cancelled"
                state.error = "客户端连接已关闭，检查点已保存。"
                state.error_code = "cancelled"
                if state.phase in state.nodes:
                    state.nodes[state.phase] = "pending"
            self.store.save(state)
            self.store.write(state.id, "report.md", make_report(state))
            self.store.write(state.id, "evidence.json", json.dumps(build_evidence(state), ensure_ascii=False, indent=2))
            raise
        except Exception as exc:
            state.status = "cancelled" if isinstance(exc, Cancelled) else "failed"
            key = getattr(getattr(self.provider, "config", None), "api_key", "")
            state.error = str(exc).replace(key, "[已隐藏密钥]") if key else str(exc)
            state.error_code = "cancelled" if isinstance(exc, Cancelled) else getattr(exc, "code", "node_error")
            if state.phase in state.nodes:
                state.nodes[state.phase] = "pending" if isinstance(exc, Cancelled) else "failed"
            self.emit("workflow_cancelled" if isinstance(exc, Cancelled) else "workflow_error", state.error, agent=state.phase, data={"error_code": state.error_code})
        self.store.save(state)
        self.store.write(state.id, "report.md", make_report(state))
        self.store.write(state.id, "evidence.json", json.dumps(build_evidence(state), ensure_ascii=False, indent=2))
        return state
