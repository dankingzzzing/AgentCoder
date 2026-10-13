# 多场景工作流验收

规则 Agent + 真实 unittest；衡量编排行为，不代表 LLM 缺陷检测能力。

预期行为通过：10/10。预算终止、未实测等场景按各自预期判断，不能作为代码验收通过率。

| 场景 | 运行结果 | 验收结论 | 修复 | 测试历史 | 冻结测试 | 预期行为 |
| --- | --- | --- | --- | --- | --- | --- |
| 契约缺陷 → 修复 | completed | verified | 1 | failed → passed | 一致 | 通过 |
| 金额精度 → 修复 | completed | verified | 1 | failed → passed | 一致 | 通过 |
| 分词排序 → 修复 | completed | verified | 1 | failed → passed | 一致 | 通过 |
| 正确代码 → 直接交付 | completed | verified | 0 | passed | 一致 | 通过 |
| 无效首修 → 第二次修复 | completed | verified | 2 | failed → failed → passed | 一致 | 通过 |
| 错误批准 → 实测否决 | completed | verified | 1 | failed → passed | 一致 | 通过 |
| 无效修复 → 预算终止 | exhausted | unfinished | 2 | failed → failed → failed | 一致 | 通过 |
| 审查中断 → 检查点恢复 | completed | verified | 1 | failed → passed | 一致 | 通过 |
| 未运行测试 → 不宣称验收 | completed | unverified | 0 | not_run | 一致 | 通过 |
| 新增交付验收规则节点 | completed | verified | 1 | failed → passed | 一致 | 通过 |
