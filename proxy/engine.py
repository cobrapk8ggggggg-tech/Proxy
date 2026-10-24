import asyncio
import base64
import hashlib
import json
import time
import uuid
from contextlib import asynccontextmanager
from .protocol import canonical, prompt_for, parse_tools, completion, content_text
from .providers import ProviderError
from .chatgpt_models import select_model
from jsonschema import Draft202012Validator, SchemaError


class Engine:
    def __init__(self, store, providers):
        self.store = store
        self.providers = providers
        self.locks = {}
        self.gate = asyncio.Semaphore(8)

    @asynccontextmanager
    async def lock(self, key):
        if key not in self.locks:
            if len(self.locks) > 1000:
                raise ProviderError("عدد المحادثات المتزامنة مرتفع؛ حاول لاحقًا.", 429)
            self.locks[key] = [asyncio.Lock(), 0]
        item = self.locks[key]
        item[1] += 1
        try:
            async with item[0]:
                yield
        finally:
            item[1] -= 1
            if not item[1]:
                self.locks.pop(key, None)

    def digest(self, messages):
        return hashlib.sha256(
            json.dumps(
                canonical(
                    [m for m in messages if m["role"] not in ("system", "developer")]
                ),
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def validate(self, body):
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages or len(messages) > 1000:
            raise ProviderError(
                "messages يجب أن تكون قائمة غير فارغة بحد أقصى 1000 رسالة.", 400
            )
        for message in messages:
            if not isinstance(message, dict) or message.get("role") not in (
                "user",
                "assistant",
                "system",
                "developer",
                "tool",
                "function",
            ):
                raise ProviderError("دور رسالة غير صالح.", 400)
            content = message.get("content")
            if content is not None and not isinstance(content, (str, list)):
                raise ProviderError("محتوى الرسالة غير صالح.", 400)
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict) or part.get("type") not in (
                        "text",
                        "image_url",
                        "file",
                        "input_text",
                        "output_text",
                    ):
                        raise ProviderError(
                            "نوع المحتوى غير مدعوم؛ استخدم نصًا أو صورة أو ملفًا.", 400
                        )
                    if part["type"] in (
                        "text",
                        "input_text",
                        "output_text",
                    ) and not isinstance(part.get("text"), str):
                        raise ProviderError("محتوى النص يجب أن يكون نصًا.", 400)
        for message in messages:
            calls = message.get("tool_calls", [])
            if not isinstance(calls, list) or len(calls) > 128:
                raise ProviderError("قائمة tool_calls غير صالحة.", 400)
            for call in calls:
                if (
                    not isinstance(call, dict)
                    or not isinstance(call.get("id"), str)
                    or not isinstance(call.get("function"), dict)
                    or not isinstance(call["function"].get("name"), str)
                    or not isinstance(call["function"].get("arguments"), str)
                ):
                    raise ProviderError(
                        "صيغة استدعاء أداة في تاريخ المحادثة غير صالحة.", 400
                    )
            if message["role"] == "tool" and not isinstance(
                message.get("tool_call_id"), str
            ):
                raise ProviderError("رسالة نتيجة الأداة تحتاج tool_call_id.", 400)
        tools = body.get("tools") or []
        if not isinstance(tools, list) or len(tools) > 128:
            raise ProviderError("قائمة الأدوات غير صالحة.", 400)
        for tool in tools:
            if (
                not isinstance(tool, dict)
                or tool.get("type", "function") != "function"
                or not isinstance(tool.get("function"), dict)
                or not isinstance(tool["function"].get("name"), str)
            ):
                raise ProviderError("أداة function غير صالحة.", 400)
        for tool in tools:
            schema = tool["function"].get("parameters", {"type": "object"})

            def check_refs(value):
                if isinstance(value, dict):
                    for key, child in value.items():
                        if key in ("$ref", "$dynamicRef") and (
                            not isinstance(child, str) or not child.startswith("#")
                        ):
                            raise ProviderError(
                                "مراجع مخطط الأدوات يجب أن تكون محلية فقط.", 400
                            )
                        check_refs(child)
                elif isinstance(value, list):
                    for child in value:
                        check_refs(child)

            check_refs(schema)
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError:
                raise ProviderError("مخطط أداة function غير صالح.", 400) from None
        choice = body.get("tool_choice", "auto")
        if (
            isinstance(choice, str) and choice not in ("auto", "none", "required")
        ) or not isinstance(choice, (str, dict)):
            raise ProviderError("tool_choice غير صالح.", 400)
        if isinstance(choice, dict) and not isinstance(choice.get("function"), dict):
            raise ProviderError("tool_choice function غير صالح.", 400)
        if isinstance(choice, dict) and not any(
            t["function"]["name"] == (choice.get("function") or {}).get("name")
            for t in tools
        ):
            raise ProviderError("الأداة المحددة غير موجودة.", 400)
        if choice == "required" and not tools:
            raise ProviderError("tool_choice required يحتاج أدوات.", 400)
        for key, low, high in [
            ("temperature", 0, 2),
            ("max_tokens", 1, 131072),
            ("max_completion_tokens", 1, 131072),
            ("max_output_tokens", 1, 131072),
        ]:
            if key in body and (
                isinstance(body[key], bool)
                or not isinstance(body[key], (float, int))
                or not low <= body[key] <= high
            ):
                raise ProviderError("معامل " + key + " خارج المدى.", 400)
        if body.get("n", 1) != 1:
            raise ProviderError("هذا المزوّد يدعم n=1 فقط.", 400)
        if (
            not isinstance(body.get("response_format", {}), dict)
            or body.get("response_format", {}).get("type", "text") != "text"
        ):
            raise ProviderError(
                "Structured output غير متاح في بروتوكولات الحسابات؛ استخدم النص أو أدوات function.",
                400,
            )
        if "stream_options" in body and not isinstance(body["stream_options"], dict):
            raise ProviderError("stream_options يجب أن يكون كائنًا.", 400)
        for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
            if key in body and not isinstance(body[key], int):
                raise ProviderError("معامل " + key + " يجب أن يكون عددًا صحيحًا.", 400)

    def resolve(self, model, client):
        route = next(
            (r for r in self.store.routes() if r["id"] == model and r["enabled"]), None
        )
        if not route:
            raise ProviderError(
                "النموذج غير موجود أو معطّل. اختر نموذجًا من /v1/models.", 404
            )
        if "*" not in client["models"] and model not in client["models"]:
            raise ProviderError("هذا المفتاح غير مخول لهذا النموذج.", 403)
        account = self.store.account(route["account_id"])
        if not account["enabled"]:
            raise ProviderError("حساب المزوّد معطّل.", 503)
        return route, account

    async def complete(self, body, client, explicit=None):
        self.validate(body)
        route, account = self.resolve(body.get("model", ""), client)
        settings = {**account["settings"], **route["config"]}
        if account["provider"] == "chatgpt":
            try:
                _, settings["chatgpt_mode"], _ = select_model(
                    account["catalog"], settings
                )
            except ValueError as exc:
                raise ProviderError(str(exc), 400) from None
        messages = body["messages"]
        tools = body.get("tools") or []
        # Derived from the request, never from account settings or model-generated text.
        settings["_client_tools"] = bool(tools)
        # Per-request preferences override defaults, while project/account routing stays under dashboard control.
        for key in (
            "temperature",
            "max_tokens",
            "reasoning_effort",
            "thinking_enabled",
            "search_enabled",
        ):
            if key in body:
                settings[key] = body[key]
        if isinstance(body.get("reasoning"), dict) and body["reasoning"].get("effort"):
            settings["reasoning_effort"] = body["reasoning"]["effort"]
        if "thinking" in body:
            thinking = body["thinking"]
            settings["thinking_enabled"] = (
                thinking.get("type") not in ("disabled", "none", "off")
                if isinstance(thinking, dict)
                else bool(thinking)
            )
        if "enable_thinking" in body:
            settings["thinking_enabled"] = bool(body["enable_thinking"])
        if account["provider"] in ("deepseek", "qwen") and body.get("reasoning_effort"):
            settings["thinking_enabled"] = body["reasoning_effort"] not in (
                "none",
                "off",
                "false",
            )
        if "max_completion_tokens" in body:
            settings["max_tokens"] = body["max_completion_tokens"]
        explicit = explicit or body.get("conversation_id") or body.get("session_id")
        if explicit is not None and (
            not isinstance(explicit, str) or len(explicit) > 200
        ):
            raise ProviderError("معرّف المحادثة غير صالح.", 400)
        scope = hashlib.sha256(
            json.dumps(
                [
                    hashlib.sha256(account["credential"].encode()).hexdigest(),
                    settings.get("project_id", ""),
                    settings.get("conversation_mode"),
                    settings.get("conversation_id", ""),
                    *(
                        [settings["chatgpt_mode"]]
                        + (
                            [settings.get("tool_environment", "application")]
                            if tools
                            else []
                        )
                        if account["provider"] == "chatgpt"
                        else []
                    ),
                ]
            ).encode()
        ).hexdigest()
        last_assistant = max(
            (i for i, m in enumerate(messages) if m["role"] == "assistant"), default=-1
        )
        prefix = (
            self.digest(messages[: last_assistant + 1]) if last_assistant >= 0 else ""
        )
        convs = []
        if settings.get("conversation_mode") != "per_request" and not body.get(
            "regenerate"
        ):
            if explicit:
                convs = self.store.rows(
                    "SELECT * FROM conversations WHERE client_id=? AND account_id=? AND scope=? AND explicit_id=? ORDER BY updated DESC LIMIT 1",
                    (client["id"], account["id"], scope, explicit),
                )
            elif prefix:
                convs = self.store.rows(
                    "SELECT * FROM conversations WHERE client_id=? AND account_id=? AND scope=? AND history_hash=? AND updated>? ORDER BY updated DESC LIMIT 2",
                    (
                        client["id"],
                        account["id"],
                        scope,
                        prefix,
                        time.time() - 30 * 86400,
                    ),
                )
        # Identical histories are ambiguous: replay into a fresh chat rather than merge two chats.
        conversation = convs[0] if len(convs) == 1 else None
        cid = conversation["id"] if conversation else "conv_" + uuid.uuid4().hex
        fixed = (
            settings.get("conversation_id")
            if settings.get("conversation_mode") == "fixed"
            else None
        )
        lock_key = account["id"] + ":" + (fixed or explicit or cid)
        started = time.monotonic()
        async with self.lock(lock_key), self.gate:
            if (
                explicit
                and not body.get("regenerate")
                and settings.get("conversation_mode") != "per_request"
            ):
                found = self.store.rows(
                    "SELECT * FROM conversations WHERE client_id=? AND account_id=? AND scope=? AND explicit_id=? ORDER BY updated DESC LIMIT 1",
                    (client["id"], account["id"], scope, explicit),
                )
                if found:
                    conversation = found[0]
                    cid = conversation["id"]
            if conversation:
                fresh = self.store.rows(
                    "SELECT * FROM conversations WHERE id=?", (cid,)
                )
                state = self.store.unseal(fresh[0]["state"]) if fresh else {}
            else:
                state = (
                    (
                        self.store.remote_state(account["id"], fixed)
                        or await self.providers.conversation(account, fixed)
                    )
                    if fixed
                    else {}
                )
            if fixed and account["provider"] in ("gemini", "deepseek"):
                cached = self.store.remote_state(account["id"], fixed)
                if cached:
                    state = cached
            pending = (
                messages[last_assistant + 1 :]
                if conversation and last_assistant >= 0
                else messages
            )
            # Repeat system/tool instructions; never replay the old user dialogue into a known remote chat.
            if conversation:
                pending = [
                    m for m in messages if m["role"] in ("system", "developer")
                ] + pending
            attachments = []
            for message in pending:
                for part in (
                    message.get("content", [])
                    if isinstance(message.get("content"), list)
                    else []
                ):
                    if part.get("type") == "image_url":
                        url = part.get("image_url", {})
                        url = url.get("url", "") if isinstance(url, dict) else url
                        if (
                            not isinstance(url, str)
                            or not url.startswith("data:image/")
                            or ";base64," not in url
                        ):
                            raise ProviderError(
                                "أرسل الصورة كـ data URL محلية؛ تنزيل روابط الصور الخارجية غير مفعّل.",
                                400,
                            )
                        try:
                            header, encoded = url.split(",", 1)
                            data = base64.b64decode(encoded, validate=True)
                        except (ValueError, TypeError):
                            raise ProviderError(
                                "بيانات الصورة غير صالحة.", 400
                            ) from None
                        attachments.append(
                            await self.providers.upload(
                                account,
                                "image." + header.split("/")[1].split(";")[0],
                                header[5:].split(";")[0],
                                data,
                            )
                        )
                    if part.get("type") == "file":
                        file_id = part.get("file", {}).get("file_id")
                        rows = self.store.rows(
                            "SELECT * FROM files WHERE id=? AND client_id=? AND account_id=? AND expires>?",
                            (file_id, client["id"], account["id"], time.time()),
                        )
                        if not rows:
                            raise ProviderError(
                                "الملف غير متاح لهذا الحساب أو انتهت صلاحيته.", 400
                            )
                        attachment = self.store.unseal(rows[0]["attachment"])
                        if attachment.pop("base64", False):
                            attachment["bytes"] = base64.b64decode(attachment["bytes"])
                        attachments.append(attachment)
            tool_names = {
                call["id"]: call["function"]["name"]
                for message in messages
                for call in message.get("tool_calls", [])
            }
            pending = [
                (
                    {
                        **message,
                        "name": tool_names.get(
                            message.get("tool_call_id"), message.get("name", "tool")
                        ),
                    }
                    if message["role"] == "tool"
                    else message
                )
                for message in pending
            ]
            prompt = prompt_for(
                pending,
                tools,
                body.get("tool_choice", "auto"),
                body.get("parallel_tool_calls", True),
                settings.get("tool_environment", "application"),
            )
            try:
                if (
                    account["provider"] == "gemini"
                    and settings.get("gemini_paragraph_mode")
                    and settings.get("task") == "translation"
                ):
                    if tools:
                        raise ProviderError(
                            "وضع ترجمة الفقرات لا يعمل مع أدوات الوكيل؛ استخدم مسار المحادثة العادي.",
                            400,
                        )
                    from .translation import translate

                    source = content_text(pending[-1].get("content"))
                    if pending[-1]["role"] != "user" or not source:
                        raise ProviderError(
                            "مسار الترجمة يحتاج نص الفصل في آخر رسالة مستخدم.", 400
                        )
                    text = await translate(
                        source,
                        lambda value: self.providers.send(
                            account,
                            prompt.replace(source, value, 1),
                            state,
                            settings,
                            attachments,
                        ),
                    )
                else:
                    text = await self.providers.send(
                        account, prompt, state, settings, attachments
                    )
                calls = parse_tools(
                    text,
                    tools,
                    body.get("tool_choice", "auto"),
                    body.get("parallel_tool_calls", True),
                )
                native_observed = (
                    bool(state.get("native_tools_used"))
                    if account["provider"] == "chatgpt"
                    else False
                )
                message = {
                    "role": "assistant",
                    "content": None if calls else text,
                    **({"tool_calls": calls} if calls else {}),
                }
                result = completion(body["model"], message, cid)
                if tools and account["provider"] == "chatgpt":
                    result["execution"] = {
                        "policy": settings.get("tool_environment", "application"),
                        "native_tools_observed": native_observed,
                        "application_tool_calls": len(calls or []),
                    }
                if account["provider"] in ("gemini", "deepseek"):
                    self.store.save_remote_state(account["id"], state)
                self.store.run(
                    "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET history_hash=excluded.history_hash,state=excluded.state,updated=excluded.updated",
                    (
                        cid,
                        account["id"],
                        client["id"],
                        scope,
                        explicit,
                        self.digest(messages + [message]),
                        self.store.seal(state),
                        time.time(),
                        time.time(),
                    ),
                )
            except Exception:
                self.store.event(
                    "completion",
                    account["id"],
                    body["model"],
                    502,
                    time.monotonic() - started,
                )
                raise
            self.store.event(
                "completion",
                account["id"],
                body["model"],
                200,
                time.monotonic() - started,
            )
            return result
