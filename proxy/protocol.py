"""OpenAI wire formats and explicit text-to-tool bridge for account web backends."""

import json
import re
import time
import uuid
from jsonschema import validate, ValidationError, SchemaError
from .providers import ProviderError


def content_text(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "\n".join(
        (
            p.get("text", "")
            if p.get("type") in ("text", "input_text", "output_text")
            else "[attachment]"
        )
        for p in content
    )


def canonical(messages):
    return [
        {
            "role": m["role"],
            "content": content_text(m.get("content")),
            **(
                {
                    "tool_calls": [
                        {"id": c["id"], "function": c["function"]}
                        for c in m["tool_calls"]
                    ]
                }
                if m.get("tool_calls")
                else {}
            ),
            **({"tool_call_id": m["tool_call_id"]} if m.get("tool_call_id") else {}),
        }
        for m in messages
    ]


def prompt_for(messages, tools, choice, parallel, environment="application"):
    instructions = []
    dialogue = []
    names = {
        c["id"]: c["function"]["name"]
        for m in messages
        for c in m.get("tool_calls", [])
    }
    for m in messages:
        role = m["role"]
        text = content_text(m.get("content"))
        if role in ("system", "developer"):
            instructions.append(text)
            continue
        if m.get("tool_calls"):
            text = json.dumps(
                {"tool_calls": [c["function"] for c in m["tool_calls"]]},
                ensure_ascii=False,
            )
        if role == "tool":
            text = f"APPLICATION TOOL RESULT {names.get(m.get('tool_call_id'),m.get('name','tool'))} ({m.get('tool_call_id','')}):\n{text}"
        dialogue.append(f"{role.upper()}:\n{text}")
    boundary = ""
    if tools:
        boundary = (
            "EXECUTION ENVIRONMENT CONTRACT:\n"
            "The client application (for example OpenMinis) and ChatGPT's native sandbox are TWO SEPARATE ENVIRONMENTS. "
            "The supplied functions belong to the CLIENT APPLICATION. Its Linux shell, files, network, installed packages, browser and device are NOT your native environment. "
            "A tool name appearing in this text is an application function to REQUEST, not a native tool to execute. "
            "To act on application/device files or execute an application command, return the JSON tool_calls envelope below and STOP. "
            "Do not run the command internally first. Wait for the next APPLICATION TOOL RESULT before reporting application execution, success, failure or file contents. "
            "Do not say application tools are unavailable merely because you cannot invoke them natively: the client executes your JSON request. "
            "Never fabricate a tool result, tool permission failure or application filesystem state. "
        )
        boundary += (
            "Native ChatGPT tools are permitted ONLY for independent work in ChatGPT's own environment, such as calculations or files already available there. "
            "Keep native and application results distinct. Native files are not application files; transferring them requires an explicit application tool call carrying the actual data. "
            "When the user's requested environment is unclear, prefer the application. "
            if environment == "hybrid"
            else "For this turn, do not invoke ANY native ChatGPT tools (including python, container, terminal, browser, search or computer). Use only the application JSON tool-call protocol. "
        )
        if choice == "none":
            boundary += "The client set tool_choice=none: do not request any application tools in this turn. Respond in text without claiming new application execution."
        else:
            boundary += 'Application call format: {"tool_calls":[{"name":"tool_name","arguments":{}}]}. Your ENTIRE response must be that JSON when requesting a tool.'
        instructions.append(boundary)
    if tools and choice != "none":
        instructions.append(
            'Use only the tools listed below when needed. To request tools, your ENTIRE response must be a JSON object {"tool_calls":[{"name":"tool_name","arguments":{}}]}. Arguments must follow the full JSON Schema. Do not invent results. After tool results arrive, continue the task. Otherwise respond normally. '
            + (
                "Call at most one tool."
                if not parallel
                else "Multiple independent calls may be requested."
            )
            + "\nTOOLS: "
            + json.dumps(tools, ensure_ascii=False)
        )
        if choice == "required":
            instructions.append("You must request at least one tool in this turn.")
        if isinstance(choice, dict):
            instructions.append("Use this tool: " + choice["function"]["name"])
    return (
        ("SYSTEM:\n" + "\n\n".join(instructions) + "\n\n" if instructions else "")
        + "\n\n".join(dialogue)
        + ("\n\n" + boundary if boundary else "")
    )


def parse_tools(text, tools, choice="auto", parallel=True):
    if not tools or choice == "none":
        return None
    definitions = {t["function"]["name"]: t["function"] for t in tools}
    raw = text.strip()
    if raw.startswith("```") and raw.endswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)[:-3].strip()
    candidates = []
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("tool_calls"), list):
            candidates = data["tool_calls"]
    except ValueError:
        # Legacy ACTION syntax: raw_decode preserves nested objects and braces in strings.
        match = re.fullmatch(r"ACTION:\s*([\w.-]+)\|\s*(.*)", raw, re.S)
        if match:
            try:
                args, end = json.JSONDecoder().raw_decode(match[2])
                candidates = (
                    [{"name": match[1], "arguments": args}]
                    if not match[2][end:].strip()
                    else []
                )
            except ValueError:
                pass
    if not candidates:
        if choice == "required" or isinstance(choice, dict):
            raise ProviderError(
                "لم يلتزم المزوّد بطلب استدعاء الأداة؛ لم ننفذ أداة وهمية."
            )
        return None
    if not parallel and len(candidates) > 1:
        raise ProviderError("أعاد المزوّد عدة أدوات مع تعطيل الاستدعاء المتوازي.")
    result = []
    for candidate in candidates:
        function = candidate.get("function", candidate)
        name = function.get("name")
        args = function.get("arguments", {})
        if name not in definitions:
            raise ProviderError("طلب المزوّد أداة غير معلنة.")
        if isinstance(choice, dict) and name != choice["function"]["name"]:
            raise ProviderError("لم يلتزم المزوّد بالأداة المحددة.")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                raise ProviderError("أعاد المزوّد معاملات أداة غير صالحة.") from None
        if not isinstance(args, dict):
            raise ProviderError("معاملات الأداة يجب أن تكون كائن JSON.")
        try:
            validate(args, definitions[name].get("parameters", {"type": "object"}))
        except (ValidationError, SchemaError):
            raise ProviderError("معاملات الأداة لا تطابق المخطط المعلن.") from None
        result.append(
            {
                "id": "call_" + uuid.uuid4().hex,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            }
        )
    return result


def completion(model, message, cid):
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
            }
        ],
        "conversation_id": cid,
    }


def data_event(value, event=None):
    return (
        (f"event: {event}\n" if event else "")
        + "data: "
        + json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        + "\n\n"
    )


def chat_chunks(result, include_usage=False):
    base = {k: result[k] for k in ("id", "created", "model")}
    base["object"] = "chat.completion.chunk"

    def part(delta, finish=None):
        return data_event(
            {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        )

    yield part({"role": "assistant"})
    message = result["choices"][0]["message"]
    if message.get("tool_calls") and message.get("content"):
        yield part({"content": message["content"]})
    if message.get("tool_calls"):
        for i, call in enumerate(message["tool_calls"]):
            yield part(
                {
                    "tool_calls": [
                        {
                            "index": i,
                            "id": call["id"],
                            "type": "function",
                            "function": {
                                "name": call["function"]["name"],
                                "arguments": "",
                            },
                        }
                    ]
                }
            )
            args = call["function"]["arguments"]
            for j in range(0, len(args), 100):
                yield part(
                    {
                        "tool_calls": [
                            {"index": i, "function": {"arguments": args[j : j + 100]}}
                        ]
                    }
                )
    else:
        text = message.get("content") or ""
        for i in range(0, len(text), 96):
            yield part({"content": text[i : i + 96]})
    yield part({}, result["choices"][0]["finish_reason"])
    # Account interfaces do not expose reliable usage. Never invent billing token counts.
    if include_usage:
        yield data_event({**base, "choices": [], "usage": None})
    yield "data: [DONE]\n\n"


def response_messages(body):
    input_value = body.get("input", [])
    if isinstance(input_value, str):
        messages = [{"role": "user", "content": input_value}]
    elif isinstance(input_value, list):
        messages = []
        for item in input_value:
            if not isinstance(item, dict):
                raise ProviderError(
                    "عناصر input يجب أن تكون رسائل أو استدعاءات أدوات.", 400
                )
            kind = item.get("type", "message")
            if kind == "function_call":
                if any(
                    not isinstance(item.get(k), str)
                    for k in ("call_id", "name", "arguments")
                ):
                    raise ProviderError("استدعاء function_call غير صالح.", 400)
                messages.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": item["call_id"],
                                "type": "function",
                                "function": {
                                    "name": item["name"],
                                    "arguments": item["arguments"],
                                },
                            }
                        ],
                    }
                )
            elif kind == "function_call_output":
                if not isinstance(item.get("call_id"), str) or "output" not in item:
                    raise ProviderError("نتيجة function_call_output غير صالحة.", 400)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": item["call_id"],
                        "content": (
                            item["output"]
                            if isinstance(item["output"], str)
                            else json.dumps(item["output"])
                        ),
                    }
                )
            elif kind == "message":
                content = item.get("content", "")
                if isinstance(content, list):
                    if any(
                        not isinstance(p, dict)
                        or p.get("type")
                        not in (
                            "input_text",
                            "output_text",
                            "input_image",
                            "input_file",
                        )
                        for p in content
                    ):
                        raise ProviderError("نوع محتوى Responses غير مدعوم.", 400)
                    content = [
                        (
                            {"type": "text", "text": p.get("text", "")}
                            if p.get("type") in ("input_text", "output_text")
                            else (
                                {
                                    "type": "image_url",
                                    "image_url": {"url": p.get("image_url", "")},
                                }
                                if p.get("type") == "input_image"
                                else {
                                    "type": "file",
                                    "file": {"file_id": p.get("file_id")},
                                }
                            )
                        )
                        for p in content
                    ]
                messages.append({"role": item.get("role", "user"), "content": content})
            else:
                raise ProviderError(
                    "نوع عنصر Responses غير مدعوم: " + str(kind)[:40], 400
                )
    else:
        raise ProviderError("input يجب أن يكون نصًا أو قائمة رسائل.", 400)
    if body.get("instructions"):
        messages.insert(0, {"role": "system", "content": body["instructions"]})
    return messages


def as_response(result):
    rid = "resp_" + result["id"].removeprefix("chatcmpl-")
    message = result["choices"][0]["message"]
    output = []
    for call in message.get("tool_calls", []):
        output.append(
            {
                "id": "fc_" + uuid.uuid4().hex,
                "type": "function_call",
                "status": "completed",
                "call_id": call["id"],
                **call["function"],
            }
        )
    if message.get("content") is not None:
        output.append(
            {
                "id": "msg_" + uuid.uuid4().hex,
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": message["content"],
                        "annotations": [],
                    }
                ],
            }
        )
    return {
        "id": rid,
        "object": "response",
        "created_at": result["created"],
        "status": "completed",
        "model": result["model"],
        "output": output,
        "error": None,
        "incomplete_details": None,
        "parallel_tool_calls": True,
        "conversation_id": result["conversation_id"],
        **({"execution": result["execution"]} if "execution" in result else {}),
    }


def response_chunks(response):
    seq = 0

    def emit(kind, **fields):
        nonlocal seq
        item = {"type": kind, "sequence_number": seq, **fields}
        seq += 1
        return data_event(item, kind)

    yield emit(
        "response.created", response={**response, "status": "in_progress", "output": []}
    )
    yield emit(
        "response.in_progress",
        response={**response, "status": "in_progress", "output": []},
    )
    for i, item in enumerate(response["output"]):
        initial = {
            **item,
            "status": "in_progress",
            **(
                {"arguments": ""}
                if item["type"] == "function_call"
                else {"content": []}
            ),
        }
        yield emit("response.output_item.added", output_index=i, item=initial)
        if item["type"] == "function_call":
            yield emit(
                "response.function_call_arguments.delta",
                item_id=item["id"],
                output_index=i,
                delta=item["arguments"],
            )
            yield emit(
                "response.function_call_arguments.done",
                item_id=item["id"],
                output_index=i,
                arguments=item["arguments"],
            )
        else:
            part = item["content"][0]
            yield emit(
                "response.content_part.added",
                item_id=item["id"],
                output_index=i,
                content_index=0,
                part={**part, "text": ""},
            )
            for offset in range(0, len(part["text"]), 96):
                yield emit(
                    "response.output_text.delta",
                    item_id=item["id"],
                    output_index=i,
                    content_index=0,
                    delta=part["text"][offset : offset + 96],
                )
            yield emit(
                "response.output_text.done",
                item_id=item["id"],
                output_index=i,
                content_index=0,
                text=part["text"],
            )
            yield emit(
                "response.content_part.done",
                item_id=item["id"],
                output_index=i,
                content_index=0,
                part=part,
            )
        yield emit("response.output_item.done", output_index=i, item=item)
    yield emit("response.completed", response=response)
