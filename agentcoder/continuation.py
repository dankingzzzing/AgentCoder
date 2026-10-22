"""Create a new run from an immutable, explicitly selected parent version."""

import copy
import hashlib

from .errors import AgentError
from .state import WorkflowState


def continue_run(parent, requirement, *, mode="llm", **options):
    parent.validate()
    if mode != "llm":
        raise AgentError("继续迭代新增需求需要真实模型，请先切换模型设置。")
    if parent.status not in {"completed", "exhausted"} or not parent.artifacts.get("solution.py"):
        raise AgentError("请选择已结束且已经交付源码的运行；中断的运行请先恢复检查点。")
    if not isinstance(requirement, str) or not requirement.strip():
        raise AgentError("请填写这一轮新增或调整的要求。")
    task = parent.task + "\n\n本轮新增要求：\n" + requirement.strip()
    if len(task) > 4000:
        raise AgentError("累计功能契约超过 4000 字符，请精简本轮要求。")
    inherited = copy.deepcopy(parent.inherited_tests)
    if parent.baseline_tests:
        inherited.append({"run_id":parent.id, "source":parent.baseline_tests,
                          "sha256":hashlib.sha256(parent.baseline_tests.encode()).hexdigest()})
    if len(inherited) > 16:
        raise AgentError("最多继承 16 组回归测试，请先整理测试后再创建新运行。")
    source = parent.artifacts["solution.py"]
    return WorkflowState.create(task, mode, parent_run_id=parent.id, origin="continue",
        initial_source=source, test_module=parent.test_module, inherited_tests=inherited,
        initial_source_from={"run_id":parent.id, "version":parent.revisions[-1]["index"] if parent.revisions else None,
                             "code_sha256":hashlib.sha256(source.encode()).hexdigest()},
        **{"graph":copy.deepcopy(parent.graph), "example":parent.example, **options})
