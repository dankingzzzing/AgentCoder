"""Read only explicitly selected Python source; never load its credentials."""

from pathlib import Path

from .errors import AgentError


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
        return {"path": str(path), "content": data.decode("utf-8-sig").replace("\r\n", "\n")}
    except (OSError, UnicodeError, RuntimeError) as exc:
        raise AgentError("无法读取所选 UTF-8 Python 文件。") from exc
