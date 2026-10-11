"""Reports derive execution facts from durable state, not model claims."""

def make_report(state):
    modes = {"demo": "离线教学演示（规则 Agent，未调用 LLM）", "llm": "真实模型多 Agent 协作"}
    lines = ["# AgentCoder 工作流报告", "", f"运行：`{state.id}`；模式：{modes[state.mode]}；状态：**{state.status}**。", "",
             "## 用户需求", "", state.task, "", "## Agent 协作与状态", "",
             "| 节点 | 执行状态 |", "| --- | --- |"]
    lines += [f"| {name} | {status} |" for name, status in state.nodes.items()]
    lines += ["", f"修复轮次：{state.cycle}/{state.max_cycles}；消息数量：{len(state.messages)}。",
              "原始回归测试由编码节点生成后冻结，修复节点只改 solution.py。", "", "## 消息交接", "",
              "| 序号 | 发送方 → 接收方 | 类型 | 摘要 |", "| --- | --- | --- | --- |"]
    for message in state.messages:
        summary = str(message["payload"].get("summary", "")).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {message['seq']} | {message['sender']} → {message['recipient']} | {message['kind']} | {summary} |")
    for review in state.reviews:
        lines += ["", f"## 第 {review['cycle']} 轮审查", "", review["summary"], "",
                  f"审查决定：{'通过' if review['approved'] else '需要修复'}；Python 语法：{'有效' if review['static']['syntax_ok'] else '错误'}。", ""]
        for issue in review["issues"]:
            lines += [f"### [{issue['severity']}] {issue['title']}", "", f"位置：`solution.py:{issue['line']}`。", "",
                      "```python", issue["evidence"], "```", "", issue["detail"], "", "修复建议：" + issue["fix"], "",
                      "回归建议：" + issue["test"], ""]
        tests = review["tests"]
        lines += ["### 实际测试状态", "", f"状态：**{tests['status']}**；执行测试：{tests['tests_run']}；退出码：{tests['exit_code']}。", "",
                  "```text", tests["output"].strip(), "```", ""]
        if review["static"]["findings"]:
            lines += ["### 静态规则候选", "", "规则候选用于辅助模型判断，不代表所有候选都是实际漏洞。", ""]
            for finding in review["static"]["findings"]:
                lines.append(f"- `solution.py:{finding['line']}`：{finding['title']}。{finding['detail']}")
    if state.error:
        lines += ["", "## 错误与恢复", "", state.error, "", f"检查点停留在 `{state.phase}`，可修正配置后继续，已完成节点会保留。"]
    if state.status == "exhausted":
        lines += ["", "## 未解决问题", "", "达到循环预算，流程没有获得最终通过结论；请查看最后一轮审查和测试输出。"]
    lines += ["", "## 交付文件", "", "- `solution.py`：最新模块。", "- `test_solution.py`：原始回归测试。",
              "- `state.json`：状态、消息和实际事件。", "- `report.md`：本报告。", "",
              "没有开启测试执行时，报告明确显示 not_run；模型建议与静态检查不能替代运行验证。"]
    return "\n".join(lines) + "\n"
