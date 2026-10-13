"""Declarative node definitions; agent implementations are registered separately."""

import re
import json
import os
import tempfile
from pathlib import Path

from .errors import AgentError


def default_graph():
    return {"name": "编码 → 审查 → 修复", "entry": "coding", "nodes": [
        {"id": "coding", "agent": "coding", "kind": "code", "next": "review"},
        {"id": "review", "agent": "review", "kind": "review", "next": "done", "revise": "repair"},
        {"id": "repair", "agent": "repair", "kind": "repair", "next": "review"},
    ]}


def audited_graph():
    graph = default_graph()
    graph["name"] = "编码 → 审查 → 修复 → 交付验收"
    graph["nodes"][1]["next"] = "delivery"
    graph["nodes"].append({"id": "delivery", "agent": "audit", "kind": "observer", "next": "done"})
    return graph


BUILTIN_KINDS = {"coding": "code", "review": "review", "repair": "repair", "audit": "observer"}


def validate_graph(graph, *, bindings=None):
    if not isinstance(graph, dict) or set(graph) != {"name", "entry", "nodes"} or not isinstance(graph["nodes"], list):
        raise AgentError("流程定义格式无效。")
    if not isinstance(graph["name"], str) or not 1 <= len(graph["name"].strip()) <= 100 or not 2 <= len(graph["nodes"]) <= 10:
        raise AgentError("流程需要名称（1–100 字）和 2 至 10 个节点。")
    mapping = {}
    for node in graph["nodes"]:
        if (not isinstance(node, dict) or set(node) - {"id", "agent", "kind", "next", "revise"}
                or any(not isinstance(node.get(k), str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", node[k])
                       for k in ("id", "agent"))
                or node["id"] in mapping or node["id"] == "done"
                or node.get("kind") not in {"code", "review", "repair", "observer"}
                or (node.get("kind") != "review" and "revise" in node)):
            raise AgentError("流程节点格式无效或重复。")
        if bindings is not None and bindings.get(node["agent"]) != node["kind"]:
            raise AgentError(f"节点 {node['id']} 的 Agent 未注册或角色不匹配。")
        mapping[node["id"]] = node
    if not isinstance(graph["entry"], str) or graph["entry"] not in mapping or mapping[graph["entry"]]["kind"] != "code":
        raise AgentError("入口必须是编码节点。")
    kinds = {kind: [n["id"] for n in mapping.values() if n["kind"] == kind] for kind in ("code", "review", "repair")}
    if len(kinds["code"]) != 1 or len(kinds["review"]) != 1 or len(kinds["repair"]) > 1:
        raise AgentError("本 MVP 需要一个编码、一个审查和至多一个修复节点，可增设观察节点。")
    for node in mapping.values():
        for route in (node.get("next"), node.get("revise") if node["kind"] == "review" else "done"):
            if not isinstance(route, str) or (route != "done" and route not in mapping):
                raise AgentError(f"节点 {node['id']} 的路由引用了不存在的节点。")
    reached = {graph["entry"]}

    def target(start):
        path = set()
        while start != "done" and mapping[start]["kind"] == "observer":
            if start in path:
                raise AgentError("观察节点形成无出口循环。")
            path.add(start)
            reached.add(start)
            start = mapping[start]["next"]
        reached.add(start)
        return start

    review = kinds["review"][0]
    if target(mapping[graph["entry"]]["next"]) != review:
        raise AgentError("编码交付必须进入审查，不能绕过审查或重复编码。")
    if target(mapping[review]["next"]) != "done":
        raise AgentError("审查通过的路由必须交付，可先经过观察节点。")
    revise = target(mapping[review]["revise"])
    repair = kinds["repair"][0] if kinds["repair"] else None
    if revise not in {repair, "done"}:
        raise AgentError("审查未通过必须进入修复，或结束并保留未解决问题。")
    if repair and (revise != repair or target(mapping[repair]["next"]) != review):
        raise AgentError("修复节点必须可达，且修复交付必须复审。")
    if reached - {"done"} != set(mapping):
        raise AgentError("存在不可达节点，请移除或修正路由。")
    return mapping


class GraphStore:
    def __init__(self, root: Path):
        self.path = root / ".agentcoder" / "workflow.json"

    def load(self):
        if not self.path.exists():
            return default_graph()
        try:
            if self.path.stat().st_size > 16384:
                raise ValueError("oversized graph")
            graph = json.loads(self.path.read_text(encoding="utf-8"))
            validate_graph(graph, bindings=BUILTIN_KINDS)
            return graph
        except (OSError, ValueError, TypeError, AgentError) as exc:
            raise AgentError("已保存流程无效，请在编排界面重新保存。") from exc

    def save(self, graph):
        validate_graph(graph, bindings=BUILTIN_KINDS)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(graph, stream, ensure_ascii=False, indent=2)
            os.replace(temporary, self.path)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
