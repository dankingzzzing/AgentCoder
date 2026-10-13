"""Export one reproducible run, without local settings or neighboring files."""

import io
import json
from zipfile import ZIP_DEFLATED, ZipFile

from .evidence import build_evidence
from .report import make_report


def make_bundle(state):
    state.validate()
    files = {**state.artifacts, "report.md": make_report(state),
             "state.json": json.dumps(state.public(), ensure_ascii=False, indent=2),
             "evidence.json": json.dumps(build_evidence(state), ensure_ascii=False, indent=2),
             "workflow.json": json.dumps(state.graph, ensure_ascii=False, indent=2),
             "README.md": f"# AgentCoder 运行交付\n\n运行 `{state.id}`，状态 `{state.status}`，模式 `{state.mode}`。\n\n"
                          "`report.md` 给出验收结论；`evidence.json` 保存实际快照、差异和测试历史。\n\n"
                          "在 Python 3.10+ 中核对代码与测试后，可执行 `python -m unittest discover -s . -p test_solution.py -v`。\n"}
    if state.test_module != 'solution' and 'solution.py' in state.artifacts:
        files[state.test_module+'.py'] = state.artifacts['solution.py']
        files['README.md'] += f"\n`{state.test_module}.py` 与 `solution.py` 是同一实现，供原始回归测试按原模块名导入。\n"
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content.encode("utf-8"))
    return buffer.getvalue()
