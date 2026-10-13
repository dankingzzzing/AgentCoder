"""Local-only Web UI, protected model settings and live workflow frames."""

from __future__ import annotations

import json
import copy
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import demo
from .cli import configuration
from .errors import AgentError, ProviderError
from .delivery import make_bundle
from .evidence import build_evidence
from .fixtures import catalog, get_case
from .graph import GraphStore, BUILTIN_KINDS, default_graph, audited_graph, validate_graph
from .file_picker import pick_local_path
from .inputs import read_source
from .providers import LLMProvider
from .report import make_report
from .settings_store import SettingsStore
from .state import RunStore, WorkflowState
from .workflow import WorkflowRunner


STATIC = Path(__file__).parent / "static"


def create_server(root: Path, *, port=8767, config_file=None):
    root = root.resolve()
    bootstrap_error = ""
    try:
        config = configuration(root, config_file)
    except AgentError as exc:
        from .config import Config
        config = Config()
        bootstrap_error = str(exc)
    settings = SettingsStore(root)
    try:
        saved = settings.load()
    except AgentError as exc:
        saved = None
        bootstrap_error = str(exc)
    configured, remembered, mode = bool(saved), bool(saved), "demo"
    if saved:
        config, is_demo = saved
        mode = "demo" if is_demo else "llm"
    token = secrets.token_urlsafe(32)
    store = RunStore(root)
    graph_store = GraphStore(root)
    graph_error = ""
    try:
        selected_graph = graph_store.load()
    except AgentError as exc:
        selected_graph, graph_error = default_graph(), str(exc)
    active = {}
    lock, picker_lock = threading.Lock(), threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def valid_host(self):
            return self.headers.get("Host") in {f"localhost:{self.server.server_port}", f"127.0.0.1:{self.server.server_port}"}

        def send(self, status, value, content_type="application/json; charset=utf-8", attachment=None):
            body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if attachment:
                self.send_header("Content-Disposition", f'attachment; filename="{attachment}"')
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.valid_host():
                return self.send(403, {"error": "无效的本机 Host。"})
            if self.path == "/api/config":
                return self.send(200, {"csrf_token": token, "configured": configured, "remembered": remembered,
                    "mode": mode, "api_key_configured": bool(config.api_key), "model": config.model,
                    "base_url": config.base_url, "api_style": config.api_style, "demo_task": demo.TASK,
                    "demo_cases": catalog(),
                    "bootstrap_error": bootstrap_error})
            if self.path == "/api/graph":
                with lock:
                    graph = copy.deepcopy(selected_graph)
                return self.send(200, {"graph": graph, "error": graph_error,
                    "presets": {"standard": default_graph(), "audited": audited_graph()}, "bindings": BUILTIN_KINDS})
            if self.path == "/api/history":
                rows = store.history()
                with lock:
                    for row in rows:
                        row["active"] = row["id"] in active
                return self.send(200, {"runs": rows})
            if self.path.startswith("/api/runs/"):
                try:
                    run_id = self.path.removeprefix("/api/runs/")
                    bundle = run_id.endswith("/bundle")
                    if bundle:
                        run_id = run_id.removesuffix("/bundle")
                    state = store.load(run_id)
                    with lock:
                        running = run_id in active
                    if bundle:
                        if running:
                            return self.send(409, {"error": "该运行正在执行，请等待保存交付结果。"})
                        return self.send(200, make_bundle(state), "application/zip", f"agentcoder-{state.id[:8]}.zip")
                    return self.send(200, {"state": state.public(), "report": make_report(state),
                        "evidence": build_evidence(state), "active": running})
                except AgentError as exc:
                    return self.send(404, {"error": str(exc)})
            routes = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/studio.js": ("studio.js", "text/javascript; charset=utf-8"),
                      "/markdown.js": ("markdown.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
            if self.path not in routes:
                return self.send(404, {"error": "页面不存在。"})
            name, mime = routes[self.path]
            return self.send(200, (STATIC/name).read_bytes(), mime)

        def apply_settings(self, payload):
            nonlocal config, configured, mode, remembered
            if set(payload) - {"api_key", "model", "base_url", "api_style", "demo", "remember"}:
                raise AgentError("模型设置字段无效。")
            is_demo, remember = payload.get("demo", False), payload.get("remember", True)
            if type(is_demo) is not bool or type(remember) is not bool:
                raise AgentError("模型模式参数无效。")
            candidate = config.with_web_settings({k: v for k, v in payload.items() if k in {"api_key", "model", "base_url", "api_style"}})
            if not is_demo:
                LLMProvider(candidate).check_connection()
            if remember:
                settings.save(candidate, is_demo)
            else:
                settings.clear()
            with lock:
                config, configured, remembered = candidate, True, remember
                mode = "demo" if is_demo else "llm"
            return {"configured": True, "remembered": remember, "mode": mode,
                    "api_key_configured": bool(config.api_key), "model": config.model,
                    "base_url": config.base_url, "api_style": config.api_style}

        def execute(self, payload, resume=False):
            if not configured:
                raise AgentError("请先配置模型或进入离线演示。")
            if resume:
                if set(payload) != {"id"}:
                    raise AgentError("恢复请求必须只包含运行 ID。")
                state = store.load(payload["id"])
                if state.status in {"completed", "exhausted"}:
                    raise AgentError("该流程已结束，请创建新运行。")
            else:
                if set(payload) - {"task", "initial_source", "max_cycles", "allow_exec", "scenario", "example", "graph"}:
                    raise AgentError("工作流请求字段无效。")
                task = payload.get("task", demo.TASK)
                case = get_case(payload.get("example", "contracts"))
                if mode == "demo":
                    task = case.task
                with lock:
                    chosen_graph = copy.deepcopy(payload.get("graph", selected_graph))
                validate_graph(chosen_graph, bindings=BUILTIN_KINDS)
                state = WorkflowState.create(task, mode, initial_source=payload.get("initial_source", ""),
                    allow_exec=payload.get("allow_exec", False), max_cycles=payload.get("max_cycles", 3),
                    scenario=payload.get("scenario", "buggy"), example=case.id, graph=chosen_graph)
            provider = LLMProvider(config) if state.mode == "llm" else None
            cancelled = threading.Event()
            with lock:
                if state.id in active or len(active) >= 4:
                    return self.send(409, {"error": "该工作流正在执行，或已达到并发上限。"})
                active[state.id] = cancelled
            try:
                store.save(state)
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True

                def frame(kind, data):
                    self.wfile.write((json.dumps({"type": kind, "data": data}, ensure_ascii=False)+"\n").encode())
                    self.wfile.flush()
                frame("run", state.public())
                runner = WorkflowRunner(store, provider=provider, test_timeout=config.test_timeout,
                                        on_event=lambda event: frame("event", event), cancelled=cancelled)
                result = runner.run(state)
                frame("result", {"state": result.public(), "report": make_report(result), "evidence": build_evidence(result)})
            except (BrokenPipeError, ConnectionResetError):
                return
            finally:
                with lock:
                    active.pop(state.id, None)

        def do_POST(self):
            nonlocal selected_graph, graph_error
            if not self.valid_host() or not secrets.compare_digest(self.headers.get("X-Agent-Token", "").encode(), token.encode()):
                return self.send(403, {"error": "请求验证失败，请刷新页面。"})
            if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                return self.send(415, {"error": "需要 application/json 请求。"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 196608:
                    # Drain a bounded body so Windows does not reset a client still sending it.
                    if length > 0:
                        self.connection.settimeout(1)
                        try:
                            self.rfile.read(min(length, 262144))
                        except OSError:
                            pass
                    return self.send(413, {"error": "请求为空或超过大小上限。"})
                value = json.loads(self.rfile.read(length))
                if not isinstance(value, dict):
                    raise AgentError("请求必须是 JSON 对象。")
                if self.path == "/api/settings":
                    return self.send(200, self.apply_settings(value))
                if self.path in {"/api/graph", "/api/graph/validate"}:
                    if set(value) != {"graph"}:
                        raise AgentError("编排请求只能包含 graph。")
                    validate_graph(value["graph"], bindings=BUILTIN_KINDS)
                    if self.path == "/api/graph":
                        with lock:
                            graph_store.save(value["graph"])
                            selected_graph, graph_error = copy.deepcopy(value["graph"]), ""
                    return self.send(200, {"graph": value["graph"], "valid": True,
                        "summary": "路由有效，编码必须审查、修复必须复审，观察节点没有无出口循环。"})
                if self.path == "/api/run":
                    return self.execute(value)
                if self.path == "/api/resume":
                    return self.execute(value, resume=True)
                if self.path == "/api/cancel":
                    with lock:
                        event = active.get(value.get("id"))
                    if event:
                        event.set()
                    return self.send(200, {"requested": bool(event)})
                if self.path in {"/api/pick", "/api/source"}:
                    if not configured:
                        raise AgentError("请先完成模型设置。")
                    if self.path == "/api/pick":
                        if not picker_lock.acquire(blocking=False):
                            return self.send(409, {"error": "文件选择窗口已打开。"})
                        try:
                            path = pick_local_path("file", root)
                        finally:
                            picker_lock.release()
                        if path is None:
                            return self.send(200, {"cancelled": True})
                    else:
                        path = value.get("path")
                    return self.send(200, {**read_source(path, root), "cancelled": False})
                return self.send(404, {"error": "接口不存在。"})
            except (AgentError, ValueError, TypeError, UnicodeError) as exc:
                return self.send(400, {"error": str(exc), "needs_settings": isinstance(exc, ProviderError) and exc.http_status in {401, 403}})
            except OSError:
                return self.send(500, {"error": "本机文件访问失败。"})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def serve(root, *, port=8767, config_file=None):
    server = create_server(root, port=port, config_file=config_file)
    print(f"AgentCoder：http://127.0.0.1:{server.server_port}（Ctrl+C 停止）", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
