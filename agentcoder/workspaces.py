"""Explicit local directory selection and exclusive new-file creation."""

import hashlib
import json
import os
import re
import tempfile
import threading
from pathlib import Path

from .errors import AgentError
from .inputs import read_source, RESERVED_MODULES
from .state import RunStore


PRIVATE = {".git", ".agentcoder", ".codeagent", ".qa", ".venv", "node_modules"}


class Workspaces:
    def __init__(self, root):
        self.root = root.resolve()
        self.path = self.root / ".agentcoder" / "workspaces.json"
        try:
            self.path.resolve().relative_to(self.root)
        except (ValueError, OSError, RuntimeError) as exc:
            raise AgentError("目录配置路径超出了应用范围。") from exc
        self.lock = threading.RLock()
        self.paths = [self.root]
        self.current = self.identifier(self.root)
        self.error = ""
        try:
            if self.path.exists():
                if self.path.stat().st_size > 65536:
                    raise ValueError("oversized registry")
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if data.get("version") != 1 or not isinstance(data.get("paths"), list) or len(data["paths"]) > 32:
                    raise ValueError("invalid registry")
                for value in data["paths"]:
                    path = self.directory(value)
                    if path not in self.paths:
                        self.paths.append(path)
                self.get(data["current"])
                self.current = data["current"]
        except (OSError, ValueError, KeyError, TypeError, AgentError):
            self.error = "已保存目录不可用，已回到应用目录；可以重新选择本地目录。"

    @staticmethod
    def identifier(path):
        value = str(path).casefold() if os.name == "nt" else str(path)
        return hashlib.sha256(value.encode()).hexdigest()[:32]

    def directory(self, value):
        if not isinstance(value, str) or not value.strip() or len(value) > 4096 or "\x00" in value:
            raise AgentError("请填写有效的本地目录路径。")
        candidate = Path(value.strip().strip('"'))
        candidate = candidate if candidate.is_absolute() else self.root / candidate
        try:
            path = candidate.resolve(strict=True)
            if path != self.root and any(p.casefold() in PRIVATE or p.casefold().startswith(".env") for p in candidate.parts + path.parts):
                raise AgentError("不能选择凭据、内部状态或缓存目录。")
            if not path.is_dir():
                raise AgentError("工作目录必须是已经存在的文件夹。")
            return path
        except (OSError, RuntimeError) as exc:
            raise AgentError("本地目录不存在或无法访问。") from exc

    def get(self, workspace_id=None):
        with self.lock:
            workspace_id = self.current if workspace_id is None else workspace_id
            if not isinstance(workspace_id, str):
                raise AgentError("工作目录 ID 无效。")
            for path in self.paths:
                if self.identifier(path) == workspace_id:
                    if path.resolve() != path or not path.is_dir():
                        raise AgentError("工作目录已移动或不可用，请重新选择。")
                    return path
        raise AgentError("该工作目录尚未选择。")

    def select(self, value):
        path = self.directory(value)
        with self.lock:
            paths = self.paths[:]
            if path not in self.paths:
                if len(self.paths) >= 32:
                    raise AgentError("已达到 32 个保存目录的上限。")
                paths.append(path)
            try:
                self.path.resolve().relative_to(self.root)
            except (ValueError, OSError, RuntimeError) as exc:
                raise AgentError("目录配置路径超出了应用范围。") from exc
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False) as stream:
                    temporary = Path(stream.name)
                    json.dump({"version":1, "current":self.identifier(path), "paths":[str(p) for p in paths]}, stream, ensure_ascii=False)
                os.replace(temporary, self.path)
                self.paths, self.current, self.error = paths, self.identifier(path), ""
            finally:
                if temporary and temporary.exists():
                    temporary.unlink()
        return self.public()

    def public(self):
        try:
            path = self.get()
        except AgentError:
            self.current = self.identifier(self.root)
            self.error = "所选目录不可用，已回到应用目录；请重新选择。"
            path = self.get()
        files = []
        scanned = 0
        try:
            for item in path.iterdir():
                scanned += 1
                if scanned > 1000 or len(files) >= 50:
                    break
                if item.suffix.lower() == ".py" and not item.name.startswith('.') and item.is_file() and not item.is_symlink():
                    files.append({"name":item.name, "path":str(item)})
        except OSError:
            pass
        with self.lock:
            known = [{"id":self.identifier(p), "path":str(p), "name":p.name or str(p)} for p in self.paths]
        return {"id":self.current, "path":str(path), "files":sorted(files,key=lambda f:f["name"].casefold()),
                "known":known, "error":self.error, "output_hint":str(path / ".agentcoder" / "runs")}

    def create_file(self, workspace_id, name, content):
        path = self.get(workspace_id)
        if (not isinstance(name, str) or not 1 <= len(name) <= 120 or name != name.strip()
                or not re.fullmatch(r'[^\x00-\x1f<>:"/\\|?*]+\.py', name, re.I)
                or name.startswith('.') or name[:-3].casefold() in RESERVED_MODULES
                or name.split('.')[0].upper() in {"CON","PRN","AUX","NUL",*(f"COM{i}" for i in range(1,10)),*(f"LPT{i}" for i in range(1,10))}):
            raise AgentError("文件名必须是本目录中的 .py 文件名，不能包含路径或系统保留名。")
        if not isinstance(content, str) or "\x00" in content or len(content.encode()) > 65536:
            raise AgentError("新文件必须是最多 64 KiB 的 UTF-8 文本。")
        target = path / name
        try:
            target.resolve().relative_to(path)
        except (ValueError, OSError, RuntimeError) as exc:
            raise AgentError("新文件路径超出了已选择的目录。") from exc
        try:
            with target.open("xb") as stream:
                stream.write(content.encode("utf-8"))
        except FileExistsError as exc:
            raise AgentError("同名文件已经存在，请使用新名字；原文件没有被覆盖。") from exc
        except OSError as exc:
            raise AgentError("无法在所选目录创建文件，请检查目录权限。") from exc
        return read_source(str(target), self.root)

    def locate_run(self, run_id):
        with self.lock:
            paths = self.paths[:]
        for path in paths:
            store = RunStore(path)
            if (store.directory(run_id)/"state.json").is_file():
                return store
        raise AgentError("在已选择目录中找不到该运行记录。")

    def history(self):
        with self.lock:
            paths = self.paths[:]
        rows = []
        for path in paths:
            try:
                entries = RunStore(path).history()
                for entry in entries:
                    entry["workdir"] = str(path)
                rows.extend(entries)
            except (OSError, AgentError):
                continue
        return sorted(rows,key=lambda row:row['updated_at'],reverse=True)[:50]
