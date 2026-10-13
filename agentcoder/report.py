"""Reports derive execution facts from durable state, not model claims."""

from .evidence import build_evidence


def cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def bounded_log(output):
    lines = output.strip().splitlines()
    if len(lines) > 120:
        lines = lines[:20] + ["…中间日志省略，完整输出保存在 state.json…"] + lines[-90:]
    return "\n".join(lines)


def make_report(state):
    evidence = build_evidence(state)
    modes = {"demo": "离线教学演示（规则 Agent，未调用 LLM）", "llm": "真实模型多 Agent 协作"}
    conclusions = {"verified": "验收通过：最终代码通过实际回归测试，原始测试保持不变。",
                   "unverified": "流程完成，尚未获得实际测试通过的验收结论。",
                   "unfinished": "流程尚未验收通过，请查看未解决问题或恢复检查点。"}
    lines = ["# AgentCoder 工作流报告", "", f"运行：`{state.id}`；模式：{modes[state.mode]}；状态：**{state.status}**。", "",
             "## 验收结论", "", f"**{conclusions[evidence['acceptance']]}**", "",
             "| 指标 | 实际记录 |", "| --- | --- |",
             f"| 最终测试 | {evidence['final_tests']}；测试数 {evidence['tests_run'] if evidence['tests_run'] is not None else '未运行'} |",
             f"| 冻结测试 | {'一致' if evidence['tests_frozen'] else '尚未交付'} |",
             f"| 修复 / 审查 | {state.cycle} 次修复 / {len(state.reviews)} 次审查 |",
             f"| 交接 / 代码快照 | {len(state.messages)} 条消息 / {len(state.revisions)} 个快照 |", "",
             "## 用户需求", "", state.task, "", "## Agent 协作与状态", "",
             f"流程：{cell(state.graph['name'])}。本次运行保存了完整流程定义，后续编排修改不影响该记录。", "",
             "| 节点 | 执行状态 | 已记录执行耗时（秒） |", "| --- | --- | --- |"]
    lines += [f"| {cell(name)} | {status} | {evidence['node_seconds'].get(name, '—')} |" for name, status in state.nodes.items()]
    lines += ["", f"修复轮次：{state.cycle}/{state.max_cycles}；消息数量：{len(state.messages)}。",
              "原始回归测试由编码节点生成后冻结，修复节点只改 solution.py。", "", "## 消息交接", "",
              "角色摘要记录公开交付说明；实际代码变更以版本差异为准，运行结果以控制器实测表为准。", "",
              "| 序号 | 发送方 → 接收方 | 类型 | Agent 公开摘要 |", "| --- | --- | --- | --- |"]
    for message in state.messages:
        summary = cell(message["payload"].get("summary", ""))
        lines.append(f"| {message['seq']} | {message['sender']} → {message['recipient']} | {message['kind']} | {summary} |")
    lines += ["", "## 逐轮验收对比", "", "| 审查轮次 | 确认问题 | 实际测试 | 测试数量 |", "| --- | --- | --- | --- |"]
    for row in evidence["review_history"]:
        lines.append(f"| {row['cycle']} | {row['issues']} | {row['tests']} | {row['tests_run'] if row['tests_run'] is not None else '未运行'} |")
    lines += ["", "## 代码版本与修复差异", ""]
    if not evidence["revisions"]:
        lines.append("该历史记录未保存逐轮源码快照，不能重建先前代码；以下仅报告已保存的审查事实。")
    for revision in evidence["revisions"]:
        lines += [f"### 版本 {revision['index']} · {revision['node']} · 修复轮次 {revision['cycle']}", "",
                  revision["summary"], "", f"交接消息：{revision['message_seq']}；代码 SHA-256：`{revision['code_sha256']}`。", ""]
        if state.initial_source and revision["index"] == 0 and revision["origin"] == "agent":
            lines += ["该版本保留用户原始模块；编码节点的实现建议不会替代独立审查和修复。", ""]
        if revision["index"]:
            lines += [f"变更：新增 {revision['added']} 行，删除 {revision['removed']} 行。", "", "```diff", revision["diff"].rstrip() or "代码未发生变化。", "```", ""]
        elif revision["origin"] == "legacy_checkpoint":
            lines.append("该快照来自旧检查点，仅保存当时的当前版本。")
    if evidence["tests_sha256"]:
        lines += ["", f"原始测试 SHA-256：`{evidence['tests_sha256']}`。各修复版本使用同一冻结测试。", ""]
    for review in state.reviews:
        lines += ["", f"## 第 {review['cycle']} 轮审查", "", review["summary"], "",
                  f"审查决定：{'通过' if review['approved'] else '需要修复'}；Python 语法：{'有效' if review['static']['syntax_ok'] else '错误'}。", ""]
        for issue in review["issues"]:
            lines += [f"### [{issue['severity']}] {issue['title']}", "", f"位置：`solution.py:{issue['line']}`。", "",
                      "```python", issue["evidence"], "```", "", issue["detail"], "", "修复建议：" + issue["fix"], "",
                      "回归建议：" + issue["test"], ""]
        tests = review["tests"]
        lines += ["### 实际测试状态", "", f"状态：**{tests['status']}**；执行测试：{tests['tests_run']}；退出码：{tests['exit_code']}。", "",
                  "```text", bounded_log(tests["output"]), "```", ""]
        if review["static"]["findings"]:
            lines += ["### 静态规则候选", "", "规则候选用于辅助模型判断，不代表所有候选都是实际漏洞。", ""]
            for finding in review["static"]["findings"]:
                lines.append(f"- `solution.py:{finding['line']}`：{finding['title']}。{finding['detail']}")
    if state.error:
        lines += ["", "## 错误与恢复", "", state.error, "", f"检查点停留在 `{state.phase}`，可修正配置后继续，已完成节点会保留。"]
    if state.status == "exhausted":
        reason = next((event["message"] for event in reversed(state.events) if event["kind"] == "limit"), "流程未获得最终通过结论。")
        lines += ["", "## 未解决问题", "", reason + "请查看最后一轮审查和测试输出。"]
    lines += ["", "## 交付文件", "", "- `solution.py`：最新模块。", "- `test_solution.py`：原始回归测试。",
              "- `state.json`：状态、消息和实际事件。", "- `report.md`：本报告。", "",
              "- `evidence.json`：逐轮源码、代码差异、测试对比与验收指标。", "",
              "没有开启测试执行时，报告明确显示 not_run；模型建议与静态检查不能替代运行验证。"]
    # Normalize presentation whitespace; exact snapshots and patches remain in evidence.json.
    return "\n".join(line.rstrip() for line in "\n".join(lines).split("\n")) + "\n"
