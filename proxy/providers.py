"""Account protocols ported from the supplied clients and tested C integration.
No provider POST is retried automatically. TLS verification stays enabled.
"""

import asyncio
import base64
import json
import re
import uuid
from urllib.parse import quote, urljoin, urlparse
import httpx
from .chatgpt_models import model_catalog, select_model

WORKER = "https://deepseek-pow.contact-3mh.workers.dev"


class ProviderError(Exception):
    def __init__(self, message, status=502):
        self.message = message
        self.status = status
        super().__init__(message)


def nested(value, *path, default=None):
    try:
        for part in path:
            value = value[part]
        return value if value is not None else default
    except (IndexError, KeyError, TypeError):
        return default


class Providers:
    def __init__(self, transport=None):
        self.transport = transport
        self.gemini_sessions = {}

    def client(self, **kw):
        return httpx.AsyncClient(
            timeout=httpx.Timeout(180, connect=30), transport=self.transport, **kw
        )

    async def checked(self, client, method, url, **kw):
        try:
            response = await client.request(method, url, **kw)
            if response.status_code >= 400:
                raise ProviderError(
                    f"رفض المزوّد الطلب (HTTP {response.status_code}). تحقق من بيانات الحساب وصلاحياته.",
                    429 if response.status_code == 429 else 502,
                )
            return response
        except httpx.HTTPError as e:
            raise ProviderError(
                "انقطع الاتصال بالمزوّد؛ لم نعد إرسال الرسالة تلقائيًا."
            ) from None

    async def gpt_headers(self, account, client):
        token = account["credential"].removeprefix("Bearer ").strip()
        aid = None
        try:
            claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "==="))
            aid = claims.get("https://api.openai.com/auth", {}).get(
                "chatgpt_account_id"
            )
        except (ValueError, IndexError, UnicodeError):
            pass
        headers = {
            "Authorization": "Bearer " + token,
            "User-Agent": "ChatGPT/1.2026.195 (Android 16; build 2619512)",
            "oai-package-name": "com.openai.chatgpt",
            "oai-client-type": "android",
            "oai-device-id": str(uuid.uuid4()),
            "Content-Type": "application/json",
        }
        if not aid:
            data = (
                await self.checked(
                    client,
                    "GET",
                    "https://android.chat.openai.com/backend-api/accounts/check/v4-2023-04-27",
                    headers=headers,
                )
            ).json()
            aid = (
                data.get("default_account_id")
                or nested(data, "account_ordering", 0)
                or next(iter(data.get("accounts", {})), None)
            )
        if not aid:
            raise ProviderError("تعذر اكتشاف حساب ChatGPT من توكن الجلسة.")
        headers["ChatGPT-Account-ID"] = aid
        return headers

    async def gpt_get(self, account, path, params=None):
        async with self.client() as client:
            headers = await self.gpt_headers(account, client)
            return (
                await self.checked(
                    client,
                    "GET",
                    "https://android.chat.openai.com/backend-api/" + path,
                    headers=headers,
                    params=params,
                )
            ).json()

    async def discover(self, account):
        provider = account["provider"]
        if provider == "chatgpt":
            models, projects, chats = await asyncio.gather(
                self.gpt_get(account, "models"),
                self.projects(account),
                self.chats(account),
                return_exceptions=True,
            )
            if isinstance(models, Exception):
                raise models
            entries, auto_model = model_catalog(models)
            if not entries:
                raise ProviderError("لم يعرض الحساب قائمة نماذج ChatGPT صالحة.")
            return {
                "models": entries,
                "default_model": "auto",
                "auto_model": auto_model,
                "chatgpt_modes": [
                    {"id": mode, "name": label}
                    for mode, label in (
                        ("chat", "دردشة عادية"),
                        ("work", "وضع العمل"),
                        ("research", "بحث معمّق"),
                    )
                    if any(m["mode"] == mode for m in entries)
                ],
                "projects": (
                    projects.get("items", []) if isinstance(projects, dict) else []
                ),
                "chats": chats.get("items", []) if isinstance(chats, dict) else [],
                "warnings": [
                    (
                        e.message
                        if isinstance(e, ProviderError)
                        else "تعذر جلب قائمة من الحساب"
                    )
                    for e in (projects, chats)
                    if isinstance(e, Exception)
                ],
                "capabilities": {
                    "projects": True,
                    "attachments": True,
                    "tools": "emulated",
                },
            }
        if provider == "gemini":
            session = await self.gemini_session(account, refresh=True)
            chats = await self.chats(account)
            return {
                "models": [
                    {k: v for k, v in m.items() if k != "header"}
                    for m in session["models"]
                ],
                "projects": [],
                "chats": chats["items"],
                "capabilities": {
                    "projects": False,
                    "attachments": True,
                    "tools": "emulated",
                },
                "warnings": ["تعرض قائمة Gemini أحدث المحادثات حتى 200 محادثة."],
            }
        if provider == "deepseek":
            async with self.client() as client:
                data = (
                    await self.checked(
                        client,
                        "POST",
                        WORKER + "/api/chat/sessions/list",
                        json={"token": account["credential"]},
                    )
                ).json()
                if data.get("success") is False or data.get("error"):
                    raise ProviderError("رفض DeepSeek توكن الحساب.")
                models = (
                    await self.checked(
                        client,
                        "GET",
                        WORKER + "/v1/models",
                        headers={"Authorization": "Bearer " + account["credential"]},
                    )
                ).json()
                return {
                    "models": [
                        {"id": m["id"], "name": m.get("name", m["id"])}
                        for m in models.get("data", [])
                    ],
                    "projects": [],
                    "chats": [
                        {
                            "id": c.get("id", c.get("session_id")),
                            "title": c.get("title", "DeepSeek"),
                            "parent_message_id": c.get(
                                "last_response_id", c.get("last_message_id")
                            ),
                        }
                        for c in data.get("sessions", [])
                    ],
                    "capabilities": {
                        "projects": False,
                        "attachments": False,
                        "tools": "emulated",
                    },
                }
        # Qwen validates its credential by fetching its actual model catalog.
        async with self.client() as client:
            data = (
                await self.checked(
                    client,
                    "GET",
                    "https://chat.qwen.ai/api/models",
                    headers={"Authorization": "Bearer " + account["credential"]},
                )
            ).json()
            entries = data.get("data", []) if isinstance(data, dict) else data
            if isinstance(entries, dict):
                entries = entries.get("models", [])
            models = [
                {
                    "id": m.get("id", m.get("model_id")),
                    "name": m.get("name", m.get("id", "Qwen")),
                }
                for m in entries
                if isinstance(m, dict) and (m.get("id") or m.get("model_id"))
            ]
            if not models:
                raise ProviderError("لم يعرض حساب Qwen قائمة نماذج صالحة.")
            return {
                "models": models,
                "projects": [],
                "chats": [],
                "capabilities": {
                    "projects": False,
                    "attachments": True,
                    "tools": "emulated",
                },
                "warnings": ["قوائم محادثات Qwen غير متاحة في العميل المرفق."],
            }

    async def projects(self, account, cursor=None):
        if account["provider"] != "chatgpt":
            return {"items": []}
        data = await self.gpt_get(
            account,
            "gizmos/snorlax/sidebar",
            {
                "owned_only": "true",
                "conversations_per_gizmo": 5,
                "limit": 50,
                **({"cursor": cursor} if cursor else {}),
            },
        )
        return {
            "items": [
                {
                    "id": nested(x, "gizmo", "gizmo", "id"),
                    "name": nested(
                        x, "gizmo", "gizmo", "display", "name", default="Project"
                    ),
                }
                for x in data.get("items", [])
                if nested(x, "gizmo", "gizmo", "id")
            ],
            "cursor": data.get("next_cursor", data.get("cursor")),
        }

    async def chats(self, account, project="", cursor=None, offset=0, limit=50):
        provider = account["provider"]
        if provider == "chatgpt":
            if project:
                return await self.gpt_get(
                    account,
                    "gizmos/" + quote(project, safe="") + "/conversations",
                    {"limit": 50, **({"cursor": cursor} if cursor else {})},
                )
            return await self.gpt_get(
                account,
                "conversations",
                {
                    "offset": offset,
                    "limit": 50,
                    "order": "updated",
                    "is_archived": "false",
                },
            )
        if provider == "gemini":
            session = await self.gemini_session(account)
            items = {}
            for pin in [1, 0]:
                for data in await self.rpc(
                    account,
                    session,
                    "MaZiqc",
                    [min(200, max(1, limit)), None, [pin, None, 1]],
                ):
                    for chat in nested(data, 2, default=[]):
                        items[chat[0]] = {"id": chat[0], "title": chat[1] or "Gemini"}
            return {
                "items": list(items.values()),
                "limited": True,
                "limit": min(200, limit),
            }
        if provider == "deepseek":
            return {"items": (await self.discover(account))["chats"]}
        return {"items": []}

    async def conversation(self, account, cid):
        if account["provider"] == "chatgpt":
            data = await self.gpt_get(account, "conversation/" + quote(cid, safe=""))
            return {
                "conversation_id": cid,
                "parent_message_id": data.get("current_node"),
                "project_id": data.get("gizmo_id")
                or nested(data, "conversation_mode", "gizmo_id"),
            }
        if account["provider"] == "gemini":
            session = await self.gemini_session(account)
            data = (
                await self.rpc(
                    account, session, "hNvQHb", [cid, 1, None, 1, [1], [4], None, 1]
                )
            )[0]
            turn = nested(data, 0, 0)
            rid = nested(turn, 0, 1)
            choice = nested(turn, 3, 0, 0, 0)
            if not rid or not choice:
                raise ProviderError("تعذر جلب معلومات متابعة محادثة Gemini.")
            return {"conversation_id": cid, "response_id": rid, "choice_id": choice}
        for chat in (await self.chats(account))["items"]:
            if chat["id"] == cid:
                return {
                    "conversation_id": cid,
                    "parent_message_id": chat.get("parent_message_id"),
                }
        raise ProviderError("المحادثة غير متاحة لهذا الحساب.")

    def gemini_headers(self, credential):
        return {
            "Cookie": credential,
            "User-Agent": "Mozilla/5.0 Chrome/130.0.0.0 Safari/537.36",
            "Origin": "https://gemini.google.com",
            "Referer": "https://gemini.google.com/",
            "x-same-domain": "1",
        }

    async def gemini_session(self, account, refresh=False):
        import time

        cached = self.gemini_sessions.get(account["id"])
        if cached and cached["expires"] > time.time() and not refresh:
            return cached
        async with self.client() as client:
            for index in [0, 1, 2]:
                headers = self.gemini_headers(account["credential"])
                url = f"https://gemini.google.com/u/{index}/app"
                for _ in range(5):
                    response = await self.checked_redirect(client, url, headers)
                    if response.is_redirect:
                        url = urljoin(url, response.headers["location"])
                        if not urlparse(url).hostname.endswith(".google.com"):
                            raise ProviderError("Google أعاد توجيهًا غير متوقع.")
                    else:
                        break
                page = response.text
                csrf = re.search(r'"SNlM0e"\s*:\s*"([^"]+)"', page)
                if not csrf:
                    continue
                sid = re.search(r'"FdrFJe"\s*:\s*"([\d-]+)"', page)
                build = re.search(r'"cfb2h"\s*:\s*"([^"]+)"', page)
                cookies = dict(
                    c.strip().split("=", 1)
                    for c in account["credential"].split(";")
                    if "=" in c
                )
                cookies.update({c.name: c.value for c in client.cookies.jar})
                session = {
                    "index": index,
                    "csrf": csrf[1],
                    "sid": sid[1] if sid else "",
                    "build": build[1] if build else "",
                    "cookies": "; ".join(k + "=" + v for k, v in cookies.items()),
                    "expires": time.time() + 600,
                }
                status = (await self.rpc(account, session, "otAQ7b", []))[0]
                if nested(status, 14) not in (None, 1000) or not isinstance(
                    nested(status, 15), list
                ):
                    continue
                tiers = nested(status, 16, default=[])
                flags = nested(status, 17, default=[])
                capacity = (
                    1
                    if 21 in tiers
                    else (
                        2
                        if 22 in tiers
                        else (
                            4
                            if 115 in flags
                            else (
                                3
                                if 16 in tiers or 106 in flags
                                else 2 if 8 in tiers or 19 in flags else 1
                            )
                        )
                    )
                )
                models = []
                for m in status[15]:
                    if not isinstance(nested(m, 0), str):
                        continue
                    ordinal = (
                        nested(m, 17)
                        if isinstance(nested(m, 17), int)
                        else nested(m, 9) if isinstance(nested(m, 9), int) else 1
                    )
                    header = (
                        [
                            1,
                            None,
                            None,
                            None,
                            m[0],
                            None,
                            None,
                            0,
                            [4, 5, 6, 8],
                            None,
                            None,
                        ]
                        + (
                            [None, capacity]
                            if 21 in tiers or 22 in tiers
                            else [capacity]
                        )
                        + [None, None, ordinal]
                    )
                    models.append(
                        {
                            "id": m[0],
                            "name": nested(m, 11)
                            or nested(m, 19)
                            or nested(m, 1)
                            or "Gemini",
                            "header": json.dumps(header),
                        }
                    )
                session["models"] = models
                if len(self.gemini_sessions) >= 128:
                    self.gemini_sessions.pop(next(iter(self.gemini_sessions)))
                self.gemini_sessions[account["id"]] = session
                return session
        raise ProviderError(
            "كوكيز Gemini غير صالحة أو يحتاج الحساب إلى إجراء في Google."
        )

    async def checked_redirect(self, client, url, headers):
        return await self.checked(
            client, "GET", url, headers=headers, follow_redirects=False
        )

    def rotate_cookies(self, session, response):
        from http.cookies import SimpleCookie

        cookies = dict(
            c.strip().split("=", 1) for c in session["cookies"].split(";") if "=" in c
        )
        for raw in response.headers.get_list("set-cookie"):
            parsed = SimpleCookie()
            try:
                parsed.load(raw)
            except Exception:
                continue
            for name, value in parsed.items():
                cookies[name] = value.value
        session["cookies"] = "; ".join(k + "=" + v for k, v in cookies.items())

    async def rpc(self, account, session, rpcid, payload):
        headers = self.gemini_headers(session["cookies"])
        headers["x-goog-ext-525001261-jspb"] = json.dumps(
            [
                1,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                [4, 5, 6, 8],
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                str(uuid.uuid4()),
            ]
        )
        async with self.client() as client:
            response = await self.checked(
                client,
                "POST",
                f'https://gemini.google.com/u/{session["index"]}/_/BardChatUi/data/batchexecute',
                headers=headers,
                params={
                    "rpcids": rpcid,
                    "hl": "en",
                    "rt": "c",
                    "source-path": f'/u/{session["index"]}/app',
                    "bl": session["build"],
                    "f.sid": session["sid"],
                    "_reqid": str(uuid.uuid4().int % 900000 + 10000),
                },
                data={
                    "at": session["csrf"],
                    "f.req": json.dumps(
                        [[[rpcid, json.dumps(payload), None, "generic"]]]
                    ),
                },
            )
        self.rotate_cookies(session, response)
        result = []
        for line in response.text.splitlines():
            if not line.strip().startswith("["):
                continue
            try:
                for frame in json.loads(line):
                    if (
                        isinstance(frame, list)
                        and frame[0] == "wrb.fr"
                        and frame[1] == rpcid
                        and isinstance(frame[2], str)
                    ):
                        result.append(json.loads(frame[2]))
            except (ValueError, IndexError, TypeError):
                pass
        if not result:
            raise ProviderError("صيغة اكتشاف Gemini تغيرت أو رفض الحساب الطلب.")
        return result

    async def send(self, account, prompt, state, settings, attachments=None):
        provider = account["provider"]
        attachments = attachments or []
        if provider == "chatgpt":
            state.pop("native_tools_used", None)
            state.pop("tool_route_corrected", None)
            try:
                selected_model, selected_mode, model_info = select_model(
                    account.get("catalog", {}), settings
                )
            except ValueError as exc:
                raise ProviderError(str(exc), 400) from None
            async with self.client() as client:
                headers = await self.gpt_headers(account, client)
                cid = state.get("conversation_id")
                parent = str(uuid.uuid4())
                if cid:
                    info = (
                        await self.checked(
                            client,
                            "GET",
                            "https://android.chat.openai.com/backend-api/conversation/"
                            + quote(cid, safe=""),
                            headers=headers,
                        )
                    ).json()
                    parent = info.get("current_node")
                    project = info.get("gizmo_id") or nested(
                        info, "conversation_mode", "gizmo_id"
                    )
                    if (
                        settings.get("project_id")
                        and project
                        and settings["project_id"] != project
                    ):
                        raise ProviderError("المحادثة تنتمي إلى مشروع آخر.")
                    if not parent:
                        raise ProviderError("لم يعرض ChatGPT عقدة متابعة المحادثة.")
                parts = [a["part"] for a in attachments if a.get("part")] + [prompt]
                body = {
                    "action": "next",
                    "messages": [
                        {
                            "id": str(uuid.uuid4()),
                            "author": {"role": "user"},
                            "content": {
                                "content_type": (
                                    "multimodal_text" if len(parts) > 1 else "text"
                                ),
                                "parts": parts,
                            },
                            "metadata": {
                                "attachments": [
                                    a["attachment"]
                                    for a in attachments
                                    if a.get("attachment")
                                ]
                            },
                            "recipient": "all",
                        }
                    ],
                    "parent_message_id": parent,
                    "model": selected_model,
                    "history_and_training_disabled": False,
                    "force_use_sse": True,
                    "stream": True,
                }
                if cid:
                    body["conversation_id"] = cid
                if settings.get("project_id"):
                    body.update(
                        conversation_mode={
                            "kind": "gizmo_interaction",
                            "gizmo_id": settings["project_id"],
                        },
                        conversation_origin="tpp",
                    )
                    body["messages"][0]["metadata"]["gizmo_id"] = settings["project_id"]
                if settings.get("reasoning_effort") not in (None, "", "auto"):
                    effort = settings["reasoning_effort"]
                    allowed = model_info.get("reasoning_efforts")
                    if allowed is not None and effort not in allowed:
                        effort = {
                            "low": "standard",
                            "medium": "standard",
                            "high": "extended",
                            "xhigh": "extended",
                        }.get(effort, effort)
                    if allowed is not None and model_info and effort not in allowed:
                        raise ProviderError(
                            "مستوى التفكير غير مدعوم للنموذج المختار.", 400
                        )
                    body["thinking_effort"] = effort
                text = ""
                finished = False
                native_violation = False
                try:
                    async with client.stream(
                        "POST",
                        "https://android.chat.openai.com/backend-api/conversation",
                        json=body,
                        headers=headers,
                    ) as response:
                        if response.status_code >= 400:
                            raise ProviderError(
                                f"رفض ChatGPT الطلب (HTTP {response.status_code}).",
                                429 if response.status_code == 429 else 502,
                            )
                        async for line in response.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            raw = line[5:].strip()
                            if raw == "[DONE]":
                                continue
                            event = json.loads(raw)
                            if event.get("error"):
                                raise ProviderError("أبلغ ChatGPT عن فشل التوليد.")
                            state["conversation_id"] = event.get(
                                "conversation_id", state.get("conversation_id")
                            )
                            m = event.get("message", {})
                            role = nested(m, "author", "role")
                            recipient = m.get("recipient")
                            native_call = role == "tool" or (
                                role == "assistant"
                                and recipient not in (None, "", "all")
                            )
                            if native_call:
                                if (
                                    settings.get("_client_tools")
                                    and settings.get("tool_environment", "application")
                                    != "hybrid"
                                ):
                                    native_violation = True
                                    if settings.get("_tool_repair_attempted"):
                                        raise ProviderError(
                                            "لم يُرجع ChatGPT استدعاء أداة التطبيق بعد محاولة تصحيح واحدة؛ لم ننفّذ أداة وهمية."
                                        )
                                # Presence only: don't expose internal tool output or reasoning.
                                state["native_tools_used"] = True
                            if (
                                role != "assistant"
                                or m.get("channel", "final") != "final"
                            ):
                                continue
                            parts = nested(m, "content", "parts", default=[])
                            if any(isinstance(p, str) for p in parts):
                                text = "".join(p for p in parts if isinstance(p, str))
                                state["parent_message_id"] = m.get("id")
                            finished = (
                                finished
                                or m.get("status") == "finished_successfully"
                                or m.get("end_turn") is True
                            )
                except httpx.HTTPError:
                    raise ProviderError(
                        "انقطع تدفق ChatGPT؛ لم نحفظ إجابة ناقصة."
                    ) from None
                if native_violation:
                    if not text.strip() or not finished:
                        raise ProviderError(
                            "رصدنا طلب أداة داخلية ولم يكتمل الرد؛ لم نُعد إرسال العملية."
                        )
                    # A distinct corrective turn, never a retry of an interrupted POST.
                    corrected = await self.send(
                        account,
                        "The preceding response used the wrong execution environment. The CLIENT APPLICATION has not executed that operation. Do not invoke a native tool again. Use the application JSON tool-call protocol from the original request below, respecting tool_choice=none if specified. Request the appropriate application function and STOP for its real result. Do not claim native output came from the application.\n\nORIGINAL APPLICATION REQUEST:\n"
                        + prompt,
                        state,
                        {**settings, "_tool_repair_attempted": True},
                        attachments,
                    )
                    state["native_tools_used"] = True
                    state["tool_route_corrected"] = True
                    return corrected
                if not text.strip() or not finished:
                    raise ProviderError("لم يكتمل رد ChatGPT.")
                state["chatgpt_mode"] = selected_mode
                return text
        if provider == "gemini":
            session = await self.gemini_session(account)
            selected = next(
                (m for m in session["models"] if m["id"] == settings.get("model")), None
            )
            if settings.get("model") and not selected:
                raise ProviderError("النموذج المحدد غير متاح لحساب Gemini.")
            headers = self.gemini_headers(session["cookies"])
            headers["x-goog-ext-525001261-jspb"] = (
                selected["header"]
                if selected
                else json.dumps(
                    [
                        1,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        [4, 5, 6, 8],
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                    ]
                )
            )
            headers["x-goog-ext-73010989-jspb"] = "[0]"
            context = [
                state.get("conversation_id", ""),
                state.get("response_id", ""),
                state.get("choice_id", ""),
                None,
                None,
                None,
                None,
                None,
                None,
                state.get("at_token", ""),
            ]
            metadata = state.get("gemini_metadata")
            if isinstance(metadata, list):
                context = list(metadata)
                while len(context) < 10:
                    context.append(None)
                context[:3] = [
                    state.get("conversation_id", ""),
                    state.get("response_id", ""),
                    state.get("choice_id", ""),
                ]
                if state.get("at_token"):
                    context[9] = state["at_token"]
            data = [
                [
                    prompt,
                    0,
                    None,
                    [[[a["uri"], 1], a["name"]] for a in attachments],
                    None,
                    None,
                    0,
                ],
                ["ar"],
                context,
                None,
                None,
                None,
                [],
                0,
                [],
                [],
                1,
                0,
            ]
            async with self.client() as client:
                response = await self.checked(
                    client,
                    "POST",
                    f'https://gemini.google.com/u/{session["index"]}/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate',
                    headers=headers,
                    params={
                        "bl": session["build"],
                        "hl": "ar",
                        "rt": "c",
                        "f.sid": session["sid"],
                        "_reqid": str(uuid.uuid4().int % 9000000 + 1000000),
                    },
                    data={
                        "at": session["csrf"],
                        "f.req": json.dumps([None, json.dumps(data)]),
                    },
                )
            self.rotate_cookies(session, response)
            text = ""
            for line in response.text.splitlines():
                if not line.strip().startswith("["):
                    continue
                try:
                    frames = json.loads(line)
                except ValueError:
                    raise ProviderError("تدفق Gemini غير مكتمل.") from None
                for frame in frames:
                    if not isinstance(frame, list):
                        continue
                    if frame[0] == "er":
                        raise ProviderError("أبلغ Gemini عن خطأ في التوليد.")
                    if not isinstance(nested(frame, 2), str):
                        continue
                    try:
                        payload = json.loads(frame[2])
                    except ValueError:
                        continue
                    if isinstance(nested(payload, 1), list):
                        old = state.get("gemini_metadata", [])
                        metadata = list(old)
                        for index, value in enumerate(payload[1]):
                            if index >= len(metadata):
                                metadata.extend([None] * (index + 1 - len(metadata)))
                            if value is not None:
                                metadata[index] = value
                        state["gemini_metadata"] = metadata
                    if isinstance(nested(payload, 25), str) and payload[25]:
                        state["at_token"] = payload[25]
                    if nested(payload, 1, 0):
                        if (
                            state.get("conversation_id")
                            and state["conversation_id"] != payload[1][0]
                        ):
                            raise ProviderError(
                                "أعاد Gemini محادثة أخرى بدل متابعة المحادثة المختارة؛ لم نحفظ الربط البديل."
                            )
                        state["conversation_id"] = payload[1][0]
                    if nested(payload, 1, 1):
                        state["response_id"] = payload[1][1]
                    if nested(payload, 4, 0, 0):
                        state["choice_id"] = payload[4][0][0]
                    if (
                        isinstance(nested(payload, 4, 0, 1, 0), str)
                        and payload[4][0][1][0]
                    ):
                        text = payload[4][0][1][0]
                    if nested(payload, 3, "26") and not isinstance(
                        nested(payload, 25), str
                    ):
                        state["at_token"] = payload[3]["26"]
            if not text.strip():
                raise ProviderError("أعاد Gemini إجابة فارغة.")
            return text
        if provider == "deepseek":
            if attachments:
                raise ProviderError("خدمة DeepSeek Telegram لا تدعم هذه المرفقات.", 400)
            payload = {
                "token": account["credential"],
                "prompt": prompt,
                "model": settings.get("model") or "deepseek-chat",
                "thinking_enabled": settings.get("thinking_enabled", False),
                "search_enabled": settings.get("search_enabled", False),
                "temperature": settings.get("temperature", 0.7),
                "max_tokens": settings.get("max_tokens", 16384),
            }
            if state.get("conversation_id"):
                payload["session_id"] = state["conversation_id"]
            if state.get("parent_message_id") is not None:
                payload["parent_message_id"] = state["parent_message_id"]
            async with self.client() as client:
                data = (
                    await self.checked(
                        client, "POST", WORKER + "/api/chat", json=payload
                    )
                ).json()
            if (
                data.get("error")
                or data.get("success") is False
                or not isinstance(data.get("response"), str)
                or not data["response"].strip()
            ):
                raise ProviderError("رفض DeepSeek الطلب أو أعاد إجابة فارغة.")
            if data.get("finish_reason") == "length":
                raise ProviderError("وصل رد DeepSeek إلى حد التوكنات ولم يكتمل.")
            if (
                state.get("conversation_id")
                and data.get("session_id")
                and state["conversation_id"] != data["session_id"]
            ):
                raise ProviderError(
                    "أنشأت خدمة DeepSeek جلسة أخرى بدل المتابعة؛ لم نحفظها كمحادثتك الأصلية."
                )
            state["conversation_id"] = data.get(
                "session_id", state.get("conversation_id")
            )
            state["parent_message_id"] = data.get(
                "parent_message_id", state.get("parent_message_id")
            )
            return data["response"]
        from . import legacy_qwen as qwen

        proxy_url = (
            await qwen.proxy_manager.get_for_session(
                account["id"] + ":" + state.get("conversation_id", "new")
            )
            if qwen.proxy_manager.enabled
            else None
        )
        async with self.client(**({"proxy": proxy_url} if proxy_url else {})) as client:
            if not state.get("conversation_id"):
                state["conversation_id"] = await qwen._qwen_create_chat(
                    account["credential"], client
                )
            files = []
            for attachment in attachments:
                files.append(
                    await qwen._qwen_upload_image_oss(
                        account["credential"],
                        attachment["bytes"],
                        client,
                        attachment["name"],
                    )
                )
            payload = qwen._qwen_build_payload(
                state["conversation_id"],
                prompt,
                state.get("parent_message_id"),
                thinking=settings.get("thinking_enabled", False),
                auto_search=settings.get("search_enabled", False),
                files=files,
            )
            if settings.get("model"):
                payload["model"] = settings["model"]
                payload["messages"][0]["models"] = [settings["model"]]
            try:
                text, parent, banned = await qwen._qwen_stream_collect(
                    account["credential"], state["conversation_id"], payload, client
                )
            except Exception:
                raise ProviderError(
                    "تعذر توليد رد Qwen؛ راجع صلاحية الحساب أو حدود الطلبات."
                ) from None
            if banned:
                raise ProviderError("رفض Qwen الاتصال أو حدد معدل الطلبات.", 429)
            state["parent_message_id"] = parent
            return text

    async def image(self, account, prompt, size):
        from . import legacy_qwen as qwen

        proxy_url = (
            await qwen.proxy_manager.get_for_session(account["id"] + ":image")
            if qwen.proxy_manager.enabled
            else None
        )
        async with self.client(**({"proxy": proxy_url} if proxy_url else {})) as client:
            cid = await qwen._qwen_create_chat(account["credential"], client)
            payload = qwen._qwen_build_payload(
                cid, prompt, None, chat_type="t2i", size=size.replace("x", ":")
            )
            response = await self.checked(
                client,
                "POST",
                qwen.QWEN_BASE + "/chat/completions",
                headers=qwen._qwen_headers_chat(account["credential"], stream=True),
                params={"chat_id": cid},
                json=payload,
            )
            result = ""
            for line in response.text.splitlines():
                if qwen._qwen_is_antibot(line) or qwen._qwen_is_rate_limited(line):
                    raise ProviderError(
                        "رفض Qwen توليد الصورة أو حدد معدل الطلبات.", 429
                    )
                if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                    continue
                try:
                    item = json.loads(line[5:])
                except ValueError:
                    continue
                result += nested(item, "choices", 0, "delta", "content", default="")
            match = re.search(r'https://[^\s<>"\)]+', result)
            if not match:
                raise ProviderError("لم يعد Qwen رابط صورة صالحًا.")
            return match[0]

    async def upload(self, account, name, mime, data):
        if len(data) > 10 * 1024 * 1024:
            raise ProviderError("حجم المرفق يتجاوز 10 MB.", 413)
        if account["provider"] == "chatgpt":
            async with self.client() as client:
                headers = await self.gpt_headers(account, client)
                init = (
                    await self.checked(
                        client,
                        "POST",
                        "https://android.chat.openai.com/backend-api/files",
                        headers=headers,
                        json={
                            "file_name": name,
                            "file_size": len(data),
                            "mime_type": mime,
                            "use_case": "multimodal",
                        },
                    )
                ).json()
                url = init.get("upload_url", "")
                if urlparse(url).scheme != "https":
                    raise ProviderError("عنوان رفع ChatGPT غير صالح.")
                # No Authorization/account headers are sent to blob storage.
                await self.checked(
                    client,
                    "PUT",
                    url,
                    content=data,
                    headers={"x-ms-blob-type": "BlockBlob", "Content-Type": mime},
                )
                await self.checked(
                    client,
                    "POST",
                    "https://android.chat.openai.com/backend-api/files/"
                    + quote(init["file_id"], safe="")
                    + "/uploaded",
                    headers=headers,
                    json={},
                )
                result = {
                    "attachment": {
                        "id": init["file_id"],
                        "name": name,
                        "mime_type": mime,
                        "size": len(data),
                    }
                }
                if mime.startswith("image/"):
                    result["part"] = {
                        "content_type": "image_asset_pointer",
                        "asset_pointer": "file-service://" + init["file_id"],
                        "size_bytes": len(data),
                        "width": 1024,
                        "height": 1024,
                    }
                return result
        if account["provider"] == "gemini":
            session = await self.gemini_session(account)
            headers = self.gemini_headers(session["cookies"])
            headers.update(
                {
                    "X-Goog-Upload-Protocol": "resumable",
                    "X-Goog-Upload-Command": "start",
                    "X-Goog-Upload-Header-Content-Length": str(len(data)),
                    "X-Tenant-Id": "bard-storage",
                    "Push-ID": "feeds/mcudyrk2a4khkz",
                    "X-Client-Pctx": "CgcSBWjK7pYx",
                    "Authorization": "Basic c2F2ZXM6cyNMdGhlNmxzd2F2b0RsN3J1d1U=",
                }
            )
            async with self.client() as client:
                init = await self.checked(
                    client,
                    "POST",
                    "https://push.clients6.google.com/upload/",
                    headers=headers,
                    content="File name: " + name,
                )
                url = init.headers.get(
                    "x-goog-upload-url",
                    init.headers.get("x-goog-upload-control-url", ""),
                )
                if urlparse(url).scheme != "https" or not (
                    urlparse(url).hostname or ""
                ).endswith(".google.com"):
                    raise ProviderError("عنوان رفع Gemini غير متوقع.")
                response = await self.checked(
                    client,
                    "POST",
                    url,
                    headers={
                        **headers,
                        "X-Goog-Upload-Command": "upload, finalize",
                        "X-Goog-Upload-Offset": "0",
                        "Content-Type": mime,
                    },
                    content=data,
                )
                if not response.text.strip():
                    raise ProviderError("لم يعد Gemini معرف الملف.")
                return {"uri": response.text.strip(), "name": name, "mime": mime}
        if account["provider"] == "qwen":
            return {"bytes": data, "name": name, "mime": mime}
        raise ProviderError("هذا المزوّد لا يدعم رفع الملفات.", 400)
