"""Read only explicitly selected Python source; never load its credentials."""

from pathlib import Path
import ast
import re
import sys

from .errors import AgentError

RESERVED_MODULES = {name.casefold() for name in getattr(sys,'stdlib_module_names',set())} | {name.casefold() for name in sys.builtin_module_names} | {'agentcoder'}


def module_name(value):
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", value)
            or value.casefold() in RESERVED_MODULES or value.casefold().startswith('test_inherited_')
            or value.casefold() in {"test_solution","agentcoder","con","prn","aux","nul",*(f"com{i}" for i in range(1,10)),*(f"lpt{i}" for i in range(1,10))}):
        raise AgentError("被测模块名必须是单个普通 Python 模块名，不能覆盖标准库或系统保留名。")
    return value


def source_kind(content):
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return {"kind":"source", "module":"solution"}
    has_tests = any(isinstance(node,ast.ClassDef) and any(
        isinstance(base,ast.Attribute) and base.attr == "TestCase" or isinstance(base,ast.Name) and base.id == "TestCase"
        for base in node.bases) for node in tree.body)
    implementations = any(isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and not node.name.startswith('test_') for node in tree.body)
    imports = {node.module for node in tree.body if isinstance(node,ast.ImportFrom) and node.module
               and node.module not in getattr(sys,'stdlib_module_names',set()) and node.level==0}
    if has_tests and not implementations and len(imports)==1:
        target = module_name(next(iter(imports)))
        return {"kind":"tests", "module":target}
    return {"kind":"source", "module":"solution"}


def normalize_test_input(state):
    if state.initial_source and not state.initial_tests:
        info = source_kind(state.initial_source)
        if info["kind"] == "tests":
            state.initial_tests, state.test_module = state.initial_source, info["module"]
            state.initial_source = ""


def read_source(value, root):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096 or "\x00" in value:
        raise AgentError("请填写有效的 Python 文件路径。")
    candidate = Path(value.strip().strip('"'))
    candidate = candidate if candidate.is_absolute() else root / candidate
    try:
        path = candidate.resolve(strict=True)
        private = {".git", ".agentcoder", ".codeagent", ".qa", ".venv", "node_modules"}
        if any(p.lower() in private or p.lower().startswith(".env") for p in candidate.parts + path.parts):
            raise AgentError("不能选择配置、凭据或内部文件。")
        if not path.is_file() or path.suffix.lower() != ".py":
            raise AgentError("本工作流处理单个 Python 模块，请选择 .py 文件。")
        with path.open("rb") as f:
            data = f.read(65537)
        if len(data) > 65536 or b"\x00" in data:
            raise AgentError("源文件必须是文本，且不能超过 64 KiB。")
        content = data.decode("utf-8-sig").replace("\r\n", "\n")
        return {"path": str(path), "content": content, **source_kind(content)}
    except (OSError, UnicodeError, RuntimeError) as exc:
        raise AgentError("无法读取所选 UTF-8 Python 文件。") from exc
