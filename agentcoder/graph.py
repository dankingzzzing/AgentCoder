"""Declarative node definitions; agent implementations are registered separately."""

import re

from .errors import AgentError


def default_graph():
    return {"name": "编码 → 审查 → 修复", "entry": "coding", "nodes": [
        {"id": "coding", "agent": "coding", "kind": "code", "next": "review"},
        {"id": "review", "agent": "review", "kind": "review", "next": "done", "revise": "repair"},
        {"id": "repair", "agent": "repair", "kind": "repair", "next": "review"},
    ]}


def validate_graph(graph):
    if not isinstance(graph, dict) or set(graph) != {"name", "entry", "nodes"} or not isinstance(graph["nodes"], list):
        raise AgentError("流程定义格式无效。")
    if not isinstance(graph["name"], str) or not 2 <= len(graph["nodes"]) <= 10:
        raise AgentError("流程必须包含 2 至 10 个节点。")
    mapping = {}
    for node in graph["nodes"]:
        if (not isinstance(node, dict) or set(node) - {"id", "agent", "kind", "next", "revise"}
                or any(not isinstance(node.get(k), str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", node[k])
                       for k in ("id", "agent"))
                or node["id"] in mapping or node["id"] == "done"
                or node.get("kind") not in {"code", "review", "repair", "observer"}):
            raise AgentError("流程节点格式无效或重复。")
        mapping[node["id"]] = node
    if not isinstance(graph["entry"], str) or graph["entry"] not in mapping or mapping[graph["entry"]]["kind"] != "code":
        raise AgentError("入口必须是编码节点。")
    for node in mapping.values():
        for route in (node.get("next"), node.get("revise") if node["kind"] == "review" else "done"):
            if not isinstance(route, str) or (route != "done" and route not in mapping):
                raise AgentError("流程路由引用了不存在的节点。")
    return mapping
