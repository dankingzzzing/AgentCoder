"""CLI entry point for demonstrations, real runs, recovery and the local Web app."""

import argparse
import json
import os
import sys
from pathlib import Path

from . import demo
from .config import Config, load_env
from .errors import AgentError
from .graph import validate_graph
from .inputs import read_source
from .providers import LLMProvider
from .state import RunStore, WorkflowState
from .workflow import WorkflowRunner


def configuration(root, file=None):
    return Config.from_values({**load_env(file), **os.environ}) if file else Config.from_env(root)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="AgentCoder：编码、审查与修复协作工作流")
    sub = parser.add_subparsers(dest="command", required=True)
    evaluation_parser = sub.add_parser("evaluate", help="运行十个离线工作流验收场景，不调用模型")
    evaluation_parser.add_argument("--root", type=Path, default=Path.cwd())
    evaluation_parser.add_argument("--output", type=Path, default=Path("artifacts/evaluation.json"))
    for command in ("demo", "run", "resume", "serve"):
        p = sub.add_parser(command)
        p.add_argument("--root", type=Path, default=Path.cwd())
        p.add_argument("--config", type=Path, help="指定本机 .env 文件，不写入代码仓库")
        if command == "serve":
            p.add_argument("--port", type=int, default=8767)
            continue
        if command == "resume":
            p.add_argument("id")
        else:
            p.add_argument("--max-cycles", type=int, default=3)
            p.add_argument("--graph", type=Path, help="使用自定义 JSON 流程定义")
            if command == "demo":
                p.add_argument("--example", choices=("contracts", "prices", "words"), default="contracts")
                p.add_argument("--scenario", choices=("buggy", "clean"), default="buggy")
                p.add_argument("--no-exec", action="store_true")
            else:
                p.add_argument("--task", required=True)
                p.add_argument("--source", help="本地原始模块，源文件不会被覆盖")
                p.add_argument("--allow-exec", action="store_true", help="允许运行可信的生成代码测试")
        p.add_argument("--json", action="store_true")
        p.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        root = args.root.resolve()
        if not root.is_dir():
            raise AgentError("项目目录不存在。")
        if args.command == "evaluate":
            from .evaluation import run_evaluation, evaluation_report
            result = run_evaluation(root)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            args.output.with_suffix(".md").write_text(evaluation_report(result), encoding="utf-8")
            print(evaluation_report(result))
            raise SystemExit(0 if result["all_passed"] else 1)
        if args.command == "serve":
            from .web import serve
            serve(root, port=args.port, config_file=args.config)
            return
        store = RunStore(root)
        config = configuration(root, args.config)
        if args.command == "resume":
            state = store.load(args.id)
        else:
            graph = json.loads(args.graph.read_text(encoding="utf-8")) if args.graph else None
            if graph:
                validate_graph(graph)
            options = {"max_cycles": args.max_cycles}
            if graph:
                options["graph"] = graph
            if args.command == "demo":
                from .fixtures import get_case
                state = WorkflowState.create(get_case(args.example).task, "demo", scenario=args.scenario,
                    example=args.example, allow_exec=not args.no_exec, **options)
            else:
                source = read_source(args.source, root)["content"] if args.source else ""
                state = WorkflowState.create(args.task, "llm", initial_source=source, allow_exec=args.allow_exec, **options)
        provider = LLMProvider(config) if state.mode == "llm" else None
        callback = None if args.json else lambda event: print(
            f"[{event['seq']:02}] {event['agent'] or 'workflow'} · {event['message']}", flush=True)
        result = WorkflowRunner(store, provider=provider, test_timeout=config.test_timeout, on_event=callback).run(state)
        value = result.public()
        value["directory"] = str(store.directory(result.id))
        text = json.dumps(value, ensure_ascii=False, indent=2) if args.json else (store.directory(result.id)/"report.md").read_text(encoding="utf-8")
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
        print(text)
        raise SystemExit(0 if result.status == "completed" else 2 if result.status == "exhausted" else 1)
    except (AgentError, OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1)
