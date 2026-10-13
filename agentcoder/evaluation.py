"""Reproducible offline workflow acceptance, not an LLM quality benchmark."""

import time

from .agents import CodingAgent, ReviewAgent, RepairAgent, DeliveryAudit
from .errors import ProviderError
from .evidence import build_evidence
from .fixtures import get_case
from .graph import audited_graph
from .state import RunStore, WorkflowState
from .workflow import AgentRegistry, WorkflowRunner


class UnchangedRepair:
    def run(self, state, context):
        return {"solution": state.artifacts["solution.py"], "explanation": "验收故障注入：修复未改变代码。"}


class StagedRepair(RepairAgent):
    def run(self, state, context):
        if state.cycle == 0:
            return UnchangedRepair().run(state, context)
        return super().run(state, context)


class OptimisticReview(ReviewAgent):
    def run(self, state, context):
        result = super().run(state, context)
        result.update(approved=True, decision="approve", issues=[])
        return result


class InterruptedReview(ReviewAgent):
    def __init__(self):
        super().__init__()
        self.interrupted = False

    def run(self, state, context):
        if not self.interrupted:
            self.interrupted = True
            raise ProviderError("验收故障注入：审查节点临时不可用。")
        return super().run(state, context)


def run_evaluation(root):
    store = RunStore(root / ".agentcoder" / "evaluation")
    definitions = [
        ("contracts_repair", "契约缺陷 → 修复", "contracts", "repair"),
        ("prices_repair", "金额精度 → 修复", "prices", "repair"),
        ("words_repair", "分词排序 → 修复", "words", "repair"),
        ("clean_approval", "正确代码 → 直接交付", "contracts", "clean"),
        ("two_repairs", "无效首修 → 第二次修复", "contracts", "staged"),
        ("test_gate", "错误批准 → 实测否决", "contracts", "gate"),
        ("repair_budget", "无效修复 → 预算终止", "contracts", "exhausted"),
        ("checkpoint_resume", "审查中断 → 检查点恢复", "contracts", "resume"),
        ("static_only", "未运行测试 → 不宣称验收", "contracts", "static"),
        ("extension_node", "新增交付验收规则节点", "words", "audit"),
    ]
    rows = []
    for case_id, title, example, behavior in definitions:
        case = get_case(example)
        options = {"example": example, "allow_exec": behavior != "static",
                   "scenario": "clean" if behavior in {"clean", "static"} else "buggy", "max_cycles": 2}
        if behavior == "audit":
            options["graph"] = audited_graph()
        state = WorkflowState.create(case.task, "demo", **options)
        registry = AgentRegistry().register("coding", CodingAgent()).register("review", ReviewAgent()).register("repair", RepairAgent()).register("audit", DeliveryAudit())
        if behavior == "staged":
            registry.register("repair", StagedRepair())
        elif behavior == "exhausted":
            registry.register("repair", UnchangedRepair())
        elif behavior == "gate":
            registry.register("review", OptimisticReview())
        elif behavior == "resume":
            registry.register("review", InterruptedReview())
        started = time.monotonic()
        WorkflowRunner(store, registry=registry).run(state)
        recovered = False
        if behavior == "resume":
            interrupted = state.status == "failed" and state.phase == "review"
            state = store.load(state.id)
            WorkflowRunner(store, registry=registry).run(state)
            recovered = interrupted and sum(m["kind"] == "code_ready" for m in state.messages) == 1
        evidence = build_evidence(state)
        expected = "exhausted" if behavior == "exhausted" else "unverified" if behavior == "static" else "verified"
        passed = evidence["tests_frozen"] and (state.status == "exhausted" and evidence["acceptance"] == "unfinished"
            if behavior == "exhausted" else evidence["acceptance"] == expected)
        if behavior == "resume":
            passed = passed and recovered
        if behavior == "staged":
            passed = passed and state.cycle == 2 and evidence["review_history"][1]["tests"] == "failed"
        if behavior == "gate":
            passed = passed and state.cycle == 1 and not state.reviews[0]["approved"]
        if behavior == "clean":
            passed = passed and state.nodes["repair"] == "skipped"
        if behavior == "audit":
            passed = passed and state.nodes["delivery"] == "completed" and state.messages[-1]["kind"] == "observation"
        rows.append({"id": case_id, "title": title, "example": example, "expected": expected, "passed": bool(passed),
            "run_id": state.id, "status": state.status, "acceptance": evidence["acceptance"],
            "repairs": state.cycle, "reviews": evidence["review_history"], "tests_frozen": evidence["tests_frozen"],
            "revisions": len(state.revisions), "messages": len(state.messages), "recovered_without_recoding": recovered,
            "seconds": round(time.monotonic() - started, 3)})
    return {"mode": "offline_workflow_acceptance", "description": "规则 Agent + 真实 unittest；衡量编排行为，不代表 LLM 缺陷检测能力。",
            "cases": rows, "passed": sum(row["passed"] for row in rows), "total": len(rows),
            "all_passed": all(row["passed"] for row in rows)}


def evaluation_report(value):
    lines = ["# 多场景工作流验收", "", value["description"], "",
             f"预期行为通过：{value['passed']}/{value['total']}。预算终止、未实测等场景按各自预期判断，不能作为代码验收通过率。", "",
             "| 场景 | 运行结果 | 验收结论 | 修复 | 测试历史 | 冻结测试 | 预期行为 |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for row in value["cases"]:
        tests = " → ".join(review["tests"] for review in row["reviews"])
        lines.append(f"| {row['title']} | {row['status']} | {row['acceptance']} | {row['repairs']} | {tests} | {'一致' if row['tests_frozen'] else '变更'} | {'通过' if row['passed'] else '失败'} |")
    return "\n".join(lines) + "\n"
