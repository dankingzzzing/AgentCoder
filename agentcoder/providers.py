"""Native HTTP LLM adapters."""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field, replace
from typing import Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import Config
from .errors import ProviderError


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str

    def wire(self) -> dict:
        return {"id": self.id, "type": "function",
                "function": {"name": self.name, "arguments": self.arguments}}


@dataclass
class Reply:
    content: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    provider_items: list[dict] | None = None
    provider_fields: dict = field(default_factory=dict)

    def message(self) -> dict:
        message = {"role": "assistant", "content": self.content or None, **self.provider_fields}
        if self.calls:
            message["tool_calls"] = [call.wire() for call in self.calls]
        if self.provider_items is not None:
            message["_provider_items"] = self.provider_items
        return message


class Provider(Protocol):
    identity: str
    label: str

    def complete(self, messages: list[dict], tools: list[dict]) -> Reply: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LLMProvider:
    """No SDK or framework dependency; accepts two explicit API protocols."""

    def __init__(self, config: Config, *, sleeper=time.sleep):
        config.require_llm()
        self.config = replace(config, api_style=config.resolved_api_style)
        self.identity = f"llm:{self.config.api_style}:{config.base_url}:{config.model}"
        self.label = f"LLM · {config.model}"
        self.sleeper = sleeper
        self.opener = build_opener(_NoRedirect())

    def _post(self, endpoint: str, payload: dict) -> dict:
        request = Request(f"{self.config.base_url}/{endpoint}",
                          data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          headers={"Content-Type": "application/json",
                                   "Authorization": f"Bearer {self.config.api_key}"}, method="POST")
        for attempt in range(self.config.max_retries + 1):
            try:
                with self.opener.open(request, timeout=self.config.timeout) as response:
                    raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ProviderError("模型响应超过 2 MiB 限制。")
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ProviderError("模型服务返回了无效的 JSON 对象。")
                return value
            except HTTPError as exc:
                retryable = exc.code in {408, 429, 500, 502, 503, 504}
                if not retryable or attempt == self.config.max_retries:
                    hints = {401: "请检查 API key。", 403: "请检查模型和接口权限。",
                             404: "请检查 LLM_BASE_URL、模型名和 LLM_API_STYLE。",
                             400: "请检查模型是否支持所选 API 和 function calling。"}
                    detail = self._error_detail(exc)
                    hint = hints.get(exc.code, "模型服务暂不可用，重试次数已用完。")
                    raise ProviderError(f"LLM HTTP {exc.code}。{hint}{detail}", http_status=exc.code) from exc
                try:
                    delay = max(0, min(float(exc.headers.get("Retry-After", "0")), 5))
                except (ValueError, TypeError):
                    delay = 0
                exc.close()
                self.sleeper(max(delay, min(0.5 * 2 ** attempt, 5)) + random.uniform(0, 0.1))
            except (URLError, TimeoutError, OSError) as exc:
                if attempt == self.config.max_retries:
                    raise ProviderError("无法连接模型服务或请求超时，重试次数已用完。") from exc
                self.sleeper(min(0.5 * 2 ** attempt, 5) + random.uniform(0, 0.1))
            except (ValueError, UnicodeError) as exc:
                raise ProviderError("模型服务返回的内容不是有效 UTF-8 JSON。") from exc
        raise ProviderError("LLM 请求失败。")

    def _error_detail(self, error: HTTPError) -> str:
        try:
            payload = json.loads(error.read(4096))
            value = payload.get("error", payload)
            detail = value.get("message", value.get("msg", "")) if isinstance(value, dict) else value
            if not isinstance(detail, str):
                return ""
            detail = detail.replace(self.config.api_key, "[已隐藏密钥]")
            detail = re.sub(r"(?i)bearer\s+\S+", "Bearer [已隐藏密钥]", detail)
            return " 服务提示：" + " ".join(detail.split())[:400] if detail else ""
        except (OSError, ValueError, AttributeError):
            return ""
        finally:
            error.close()

    def check_connection(self) -> dict:
        """Check the actual tool call and tool-result round trip, without local file access."""
        tool = {"type": "function", "function": {"name": "connection_check",
                "description": "Check the connection. Call this once with value OK.", "strict": True,
                "parameters": {"type": "object", "properties": {"value": {"type": "string"}},
                               "required": ["value"], "additionalProperties": False}}}
        messages = [{"role": "system", "content": "This is a connection test. You must call connection_check once with value OK, then reply OK after its result. Do not use other tools."},
                    {"role": "user", "content": "Call connection_check now."}]
        reply = self.stream_complete(messages, [tool], lambda text: None, tool_choice="connection_check")
        if len(reply.calls) != 1 or reply.calls[0].name != "connection_check":
            raise ProviderError("模型未返回验证工具调用。请检查该模型是否支持工具调用，或切换 API 协议。")
        call = reply.calls[0]
        try:
            if json.loads(call.arguments) != {"value": "OK"}:
                raise ValueError("unexpected arguments")
        except ValueError as exc:
            raise ProviderError("模型返回的验证工具参数无效。") from exc
        messages.extend([reply.message(), {"role": "tool", "tool_call_id": call.id, "content": "OK"}])
        final = self.stream_complete(messages, [tool], lambda text: None, tool_choice="none")
        if not final.content or final.calls:
            raise ProviderError("模型没有完成验证工具结果的处理，请检查模型或 API 协议。")
        return {"connected": True, "tool_calling": True, "api_style": self.config.api_style,
                "model": self.config.model}

    def complete(self, messages: list[dict], tools: list[dict]) -> Reply:
        if self.config.api_style == "responses":
            return self._responses(messages, tools)
        return self._chat(messages, tools)

    def stream_complete(self, messages: list[dict], tools: list[dict], on_text: Callable[[str], None],
                        *, on_activity: Callable[[], None] | None = None, tool_choice: str | None = None) -> Reply:
        """Stream public answer text; keep provider reasoning private to protocol state."""
        try:
            return self._stream_complete(messages, tools, on_text, on_activity, tool_choice)
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            raise ProviderError("模型流式响应格式无效。") from exc

    def _stream_complete(self, messages, tools, on_text, on_activity, tool_choice):
        if self.config.api_style == "responses":
            payload = self._responses_payload(messages, tools)
            if tool_choice:
                payload["tool_choice"] = (tool_choice if tool_choice in {"none", "auto", "required"}
                                          else {"type": "function", "name": tool_choice})
            payload["stream"] = True
            final = None
            for event in self._stream_events("responses", payload):
                if on_activity:
                    on_activity()
                if event.get("type") == "response.output_text.delta":
                    delta = event.get("delta", "")
                    if not isinstance(delta, str):
                        raise ValueError("invalid text delta")
                    on_text(delta)
                elif event.get("type") == "response.completed":
                    final = event["response"]
                elif event.get("type") in {"error", "response.failed", "response.incomplete"}:
                    raise ProviderError("模型流式响应失败或未完整生成。")
            if final is None:
                raise ProviderError("模型流中缺少完成事件。")
            return self._parse_responses(final)
        payload = self._chat_payload(messages, tools)
        if tool_choice:
            payload["tool_choice"] = (tool_choice if tool_choice in {"none", "auto", "required"}
                                      else {"type": "function", "function": {"name": tool_choice}})
        payload["stream"] = True
        text, reasoning, calls = [], [], {}
        finished = False
        for event in self._stream_events("chat/completions", payload):
            if on_activity:
                on_activity()
            if event.get("error"):
                raise ProviderError("模型流式响应返回了服务错误。")
            for choice in event.get("choices", []):
                finish = choice.get("finish_reason")
                if finish in {"length", "content_filter"}:
                    raise ProviderError("模型流式输出被截断或过滤。")
                finished = finished or finish is not None
                delta = choice.get("delta", {})
                if isinstance(delta.get("content"), str):
                    text.append(delta["content"])
                    on_text(delta["content"])
                if isinstance(delta.get("reasoning_content"), str):
                    reasoning.append(delta["reasoning_content"])
                for fragment in delta.get("tool_calls") or []:
                    index = fragment["index"]
                    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < 12:
                        raise ValueError("invalid tool index")
                    call = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    call["id"] += fragment.get("id") or ""
                    function = fragment.get("function", {})
                    call["name"] += function.get("name") or ""
                    call["arguments"] += function.get("arguments") or ""
        if not finished:
            raise ProviderError("模型流提前断开，未获得完整报告。")
        assembled = [ToolCall(**calls[i]) for i in sorted(calls)]
        self._validate_calls(assembled)
        return Reply("".join(text), assembled, provider_fields={"reasoning_content": "".join(reasoning)} if reasoning else {})

    def _stream_events(self, endpoint: str, payload: dict):
        request = Request(f"{self.config.base_url}/{endpoint}", data=json.dumps(payload, ensure_ascii=False).encode(),
                          headers={"Content-Type": "application/json", "Accept": "text/event-stream",
                                   "Authorization": f"Bearer {self.config.api_key}"}, method="POST")
        response = None
        for attempt in range(self.config.max_retries + 1):
            try:
                response = self.opener.open(request, timeout=self.config.timeout)
                break
            except HTTPError as exc:
                retry = exc.code in {408, 429, 500, 502, 503, 504} and attempt < self.config.max_retries
                if not retry:
                    detail = self._error_detail(exc)
                    raise ProviderError(f"LLM HTTP {exc.code}。请检查密钥、模型及协议。{detail}", http_status=exc.code) from exc
                exc.close()
                self.sleeper(min(.5 * 2 ** attempt, 5))
            except (URLError, TimeoutError, OSError) as exc:
                if attempt == self.config.max_retries:
                    raise ProviderError("无法连接模型服务或请求超时。") from exc
                self.sleeper(min(.5 * 2 ** attempt, 5))
        total, data = 0, []
        try:
            with response:
                for raw in response:
                    total += len(raw)
                    if total > 4 * 1024 * 1024 or len(raw) > 1024 * 1024:
                        raise ProviderError("模型流超过输出大小上限。")
                    line = raw.decode("utf-8").rstrip("\r\n")
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip())
                    elif not line and data:
                        value = "\n".join(data)
                        data = []
                        if value == "[DONE]":
                            break
                        parsed = json.loads(value)
                        if not isinstance(parsed, dict):
                            raise ValueError("invalid stream event")
                        yield parsed
        except (OSError, ValueError, UnicodeError) as exc:
            raise ProviderError("模型流传输中断或响应格式无效。") from exc

    @staticmethod
    def _validate_calls(calls: list[ToolCall]) -> None:
        if len(calls) > 12 or len({call.id for call in calls}) != len(calls):
            raise ProviderError("模型返回了过多或重复 ID 的工具调用。")
        for call in calls:
            if (not isinstance(call.id, str) or not call.id or not isinstance(call.name, str)
                    or not call.name or not isinstance(call.arguments, str)
                    or len(call.arguments) > 20000):
                raise ProviderError("模型返回了无效的工具调用格式。")

    def _chat_payload(self, messages: list[dict], tools: list[dict]) -> dict:
        cleaned = [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]
        payload = {"model": self.config.model, "messages": cleaned}
        if tools:
            payload.update(tools=tools, tool_choice="auto")
        return payload

    def _chat(self, messages: list[dict], tools: list[dict]) -> Reply:
        value = self._post("chat/completions", self._chat_payload(messages, tools))
        try:
            choice = value["choices"][0]
            if choice.get("finish_reason") in {"length", "content_filter"}:
                raise ProviderError("模型输出被截断或过滤，无法完成本轮审查。")
            message = choice["message"]
            content = message.get("content") or message.get("refusal") or ""
            if not isinstance(content, str):
                raise ValueError("invalid content")
            raw_calls = message.get("tool_calls") or []
            if not isinstance(raw_calls, list) or any(c.get("type") != "function" for c in raw_calls):
                raise ValueError("invalid calls")
            calls = [ToolCall(c["id"], c["function"]["name"], c["function"]["arguments"]) for c in raw_calls]
            self._validate_calls(calls)
            fields = {}
            if isinstance(message.get("reasoning_content"), str):
                fields["reasoning_content"] = message["reasoning_content"]
            return Reply(content=content, calls=calls, provider_fields=fields)
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            raise ProviderError("Chat Completions 响应格式无效。") from exc

    def _responses_payload(self, messages: list[dict], tools: list[dict]) -> dict:
        inputs = []
        for message in messages:
            if message["role"] == "system":
                continue
            if message["role"] == "tool":
                inputs.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                               "output": message["content"]})
            elif message.get("_provider_items"):
                inputs.extend(message["_provider_items"])
            elif message["role"] == "assistant" and message.get("tool_calls"):
                raise ProviderError("会话包含另一种 API 的工具调用，请使用新的会话名。")
            else:
                inputs.append({"role": message["role"], "content": message.get("content") or ""})
        native_tools = [{"type": "function", **tool["function"]} for tool in tools]
        payload = {"model": self.config.model, "instructions": messages[0]["content"],
                   "input": inputs, "tools": native_tools, "tool_choice": "auto", "store": False,
                   "include": ["reasoning.encrypted_content"]}
        return payload

    def _responses(self, messages: list[dict], tools: list[dict]) -> Reply:
        return self._parse_responses(self._post("responses", self._responses_payload(messages, tools)))

    def _parse_responses(self, value: dict) -> Reply:
        if value.get("status") in {"failed", "incomplete", "cancelled"} or value.get("error"):
            raise ProviderError("Responses API 未完整生成结果。请检查模型、输入长度或服务状态。")
        try:
            items = value["output"]
            if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
                raise ValueError("invalid output")
            calls = [ToolCall(i["call_id"], i["name"], i["arguments"])
                     for i in items if i.get("type") == "function_call"]
            content = []
            for item in items:
                if item.get("type") == "message":
                    for part in item.get("content", []):
                        if part.get("type") == "output_text":
                            content.append(part["text"])
                        elif part.get("type") == "refusal":
                            content.append(part["refusal"])
            self._validate_calls(calls)
            return Reply(content="\n".join(content), calls=calls, provider_items=items)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ProviderError("Responses API 响应格式无效。") from exc
