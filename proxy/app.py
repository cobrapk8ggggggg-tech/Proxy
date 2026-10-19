import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse
from fastapi import FastAPI, Request, Depends, UploadFile, File, Form
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from .store import Store
from .providers import Providers, ProviderError
from .engine import Engine
from .chatgpt_models import select_model
from .protocol import (
    as_response,
    response_messages,
    chat_chunks,
    response_chunks,
    data_event,
)

STATIC = Path(__file__).resolve().parent.parent / "static"
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class ApiError(Exception):
    def __init__(self, status, message, code="invalid_request_error"):
        self.status = status
        self.message = message
        self.code = code


def create_app(directory=None, providers=None):
    store = Store(directory or os.getenv("DATA_DIR", "data"))
    upstream = providers or Providers()
    engine = Engine(store, upstream)
    app = FastAPI(
        title="Aurum Proxy", version="12.1.0", docs_url="/api-docs", redoc_url=None
    )
    app.state.store = store
    app.state.providers = upstream
    app.state.engine = engine
    login_attempts = {}
    rates = {}

    def origin(request):
        value = request.headers.get("origin")
        if value and urlparse(value).netloc != request.headers.get("host"):
            raise ApiError(403, "طلب إدارة من موقع مختلف مرفوض.", "forbidden")

    async def admin(request: Request):
        token = request.cookies.get("aurum_session", "")
        row = store.rows(
            "SELECT * FROM admin_sessions WHERE hash=? AND expires>?",
            (hashlib.sha256(token.encode()).hexdigest(), time.time()),
        )
        if not row:
            raise ApiError(401, "سجّل دخولك إلى لوحة الإدارة.", "authentication_error")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin(request)
        return {"id": "admin", "models": ["*"]}

    async def client(request: Request):
        token = request.headers.get("authorization", "")
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        else:
            raise ApiError(
                401, "Bearer proxy API key is required.", "authentication_error"
            )
        value = store.client(token)
        if not value:
            raise ApiError(
                401, "Invalid or revoked proxy API key.", "authentication_error"
            )
        now = time.monotonic()
        previous = [t for t in rates.get(value["id"], []) if t > now - 60]
        if len(previous) >= int(os.getenv("RATE_LIMIT_PER_MINUTE", "120")):
            raise ApiError(429, "Rate limit reached. Retry later.", "rate_limit_error")
        rates[value["id"]] = previous + [now]
        return value

    def account(aid):
        try:
            return store.account(aid)
        except KeyError:
            raise ApiError(404, "الحساب غير موجود.", "not_found") from None

    async def body(request):
        try:
            result = await request.json()
        except (ValueError, UnicodeError):
            raise ApiError(400, "Invalid JSON body.") from None
        if not isinstance(result, dict):
            raise ApiError(400, "JSON body must be an object.")
        return result

    def error(status, message, code):
        return JSONResponse(
            {"error": {"message": message, "type": code, "param": None, "code": code}},
            status_code=status,
        )

    @app.exception_handler(ApiError)
    async def api_error(request, exc):
        return error(exc.status, exc.message, exc.code)

    @app.exception_handler(ProviderError)
    async def provider_error(request, exc):
        return error(exc.status, exc.message, "upstream_error")

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return error(400, "Request fields are invalid.", "invalid_request_error")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        return error(
            exc.status_code,
            (
                "المسار غير موجود."
                if exc.status_code == 404
                else "رفض المزوّد أو الخادم الطلب."
            ),
            "not_found" if exc.status_code == 404 else "upstream_error",
        )

    @app.exception_handler(Exception)
    async def unexpected(request, exc):
        # Never return/log exception repr: HTTP clients may include credential-bearing headers/URLs.
        return error(
            500,
            "تعذر تنفيذ الطلب. راجع حالة المزوّد والمحاولة المسجلة.",
            "internal_error",
        )

    @app.middleware("http")
    async def headers(request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            length = request.headers.get("content-length", "0")
            if not length.isdigit() or int(length) > 24 * 1024 * 1024:
                return error(413, "Maximum request size is 24 MB.", "request_too_large")
            # Also bound chunked bodies, so absence of Content-Length cannot bypass the limit.
            data = b""
            async for chunk in request.stream():
                data += chunk
                if len(data) > 24 * 1024 * 1024:
                    return error(
                        413, "Maximum request size is 24 MB.", "request_too_large"
                    )
            request._body = data
        if request.method == "OPTIONS" and request.url.path.startswith("/v1/"):
            response = JSONResponse({})
            response.headers.update(
                {
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
                    "Access-Control-Allow-Headers": "Authorization,Content-Type,X-Conversation-ID,Idempotency-Key",
                    "Access-Control-Max-Age": "600",
                }
            )
            return response
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/v1/"):
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Access-Control-Expose-Headers"] = (
                "X-Conversation-ID,X-Request-ID"
            )
        else:
            response.headers["Cache-Control"] = "no-store"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
            )
        return response

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "aurum-proxy", "version": "12.1.0"}

    @app.get("/")
    @app.get("/admin")
    async def dashboard():
        return FileResponse(STATIC / "index.html")

    @app.get("/manifest.webmanifest")
    async def manifest():
        return FileResponse(
            STATIC / "manifest.webmanifest", media_type="application/manifest+json"
        )

    @app.get("/sw.js")
    async def service_worker():
        return FileResponse(
            STATIC / "sw.js",
            media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/"},
        )

    @app.get("/offline")
    async def offline():
        return FileResponse(STATIC / "offline.html")

    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")

    @app.post("/admin/api/login")
    async def login(request: Request):
        origin(request)
        data = await body(request)
        expected = os.getenv("ADMIN_PASSWORD", "")
        if len(expected) < 12:
            raise ApiError(
                503,
                "اضبط ADMIN_PASSWORD بطول 12 حرفًا على الأقل لتفعيل الإدارة.",
                "setup_required",
            )
        ip = request.client.host if request.client else "unknown"
        now = time.time()
        attempts = [t for t in login_attempts.get(ip, []) if t > now - 600]
        if len(attempts) >= 10:
            raise ApiError(429, "محاولات كثيرة. انتظر عشر دقائق.", "rate_limit_error")
        if not hmac.compare_digest(str(data.get("password", "")), expected):
            login_attempts[ip] = attempts + [now]
            raise ApiError(401, "كلمة المرور غير صحيحة.", "authentication_error")
        login_attempts.pop(ip, None)
        token = secrets.token_urlsafe(48)
        store.run("DELETE FROM admin_sessions WHERE expires<?", (now,))
        store.run(
            "INSERT INTO admin_sessions VALUES(?,?)",
            (hashlib.sha256(token.encode()).hexdigest(), now + 12 * 3600),
        )
        response = JSONResponse({"ok": True})
        response.set_cookie(
            "aurum_session",
            token,
            httponly=True,
            samesite="strict",
            secure=os.getenv("COOKIE_SECURE", "").lower() == "true"
            or request.url.scheme == "https",
            max_age=12 * 3600,
            path="/admin",
        )
        return response

    @app.post("/admin/api/logout")
    async def logout(request: Request, who=Depends(admin)):
        store.run(
            "DELETE FROM admin_sessions WHERE hash=?",
            (
                hashlib.sha256(
                    request.cookies.get("aurum_session", "").encode()
                ).hexdigest(),
            ),
        )
        response = JSONResponse({"ok": True})
        response.delete_cookie("aurum_session", path="/admin")
        return response

    @app.get("/admin/api/state")
    async def state(who=Depends(admin)):
        now = time.time()
        store.run("DELETE FROM files WHERE expires<?", (now,))
        store.run("DELETE FROM responses WHERE expires<?", (now,))
        store.run("DELETE FROM idempotency WHERE expires<?", (now,))
        store.run("DELETE FROM conversations WHERE updated<?", (now - 30 * 86400,))
        events = store.rows("SELECT * FROM events ORDER BY created DESC LIMIT 120")
        stats = store.rows(
            "SELECT COUNT(*) AS requests,SUM(CASE WHEN status<400 THEN 1 ELSE 0 END) AS successes,AVG(duration) AS latency FROM events WHERE kind='completion' AND created>?",
            (now - 86400,),
        )[0]
        daily = store.rows(
            "SELECT strftime('%Y-%m-%d',created,'unixepoch') AS day,COUNT(*) AS count FROM events WHERE kind='completion' AND created>? GROUP BY day ORDER BY day",
            (now - 7 * 86400,),
        )
        return {
            "accounts": store.accounts(),
            "routes": store.routes(),
            "keys": store.keys(),
            "conversations": store.rows(
                "SELECT id,account_id,client_id,created,updated FROM conversations ORDER BY updated DESC LIMIT 100"
            ),
            "events": events,
            "stats": stats,
            "daily": daily,
            "version": "12.1.0",
        }

    async def register(data, source, enabled):
        provider = data.get("provider")
        credential = str(data.get("credential", "")).strip()
        if provider not in ("chatgpt", "gemini", "deepseek", "qwen"):
            raise ApiError(400, "اختر مزوّدًا صالحًا.")
        if provider != "gemini":
            credential = re.sub(r"^Bearer\s+", "", credential, flags=re.I)
        if len(credential) < 6 or len(credential) > 131072:
            raise ApiError(400, "الصق توكن الحساب أو الكوكيز أولًا.")
        value = store.add_account(
            provider, credential, str(data.get("label", ""))[:80], source, enabled
        )
        store.event("account_registered", value["id"])
        return store.public_account(
            store.rows("SELECT * FROM accounts WHERE id=?", (value["id"],))[0]
        )

    @app.post("/admin/api/accounts")
    async def add_account(request: Request, who=Depends(admin)):
        return await register(await body(request), "dashboard", True)

    @app.post("/v1/accounts/register")
    async def register_client_account(request: Request, who=Depends(client)):
        # Explicit registration only: never capture arbitrary bearer headers as upstream credentials.
        return await register(await body(request), "client:" + who["id"], False)

    @app.post("/admin/api/accounts/{aid}/connect")
    async def connect_account(aid: str, who=Depends(admin)):
        value = account(aid)
        try:
            catalog = await upstream.discover(value)
        except Exception:
            store.update_account(aid, status="error")
            store.event("account_connect", aid, status=502)
            raise
        store.update_account(aid, catalog=catalog, status="connected")
        store.event("account_connect", aid)
        settings = value["settings"]
        if catalog.get("models") and not any(
            m["id"] == settings.get("model") for m in catalog["models"]
        ):
            settings["model"] = (
                catalog.get("default_model") or catalog["models"][0]["id"]
            )
            store.update_account(aid, settings=settings)
        existing = [r for r in store.routes() if r["account_id"] == aid]
        if not existing and catalog.get("models"):
            base = {
                "chatgpt": "gpt",
                "gemini": "gemini",
                "deepseek": "deepseek-telegram",
                "qwen": "qwen",
            }[value["provider"]]
            if any(r["id"] == base for r in store.routes()):
                base += "-" + aid[:6]
            store.set_route(base, aid, {})
            if value["provider"] == "deepseek":
                for alias in ("deepseek", "deepseek-default"):
                    if not any(r["id"] == alias for r in store.routes()):
                        store.set_route(
                            alias,
                            aid,
                            (
                                {"thinking_enabled": False}
                                if alias == "deepseek-default"
                                else {}
                            ),
                        )
            if value["provider"] == "qwen" and not any(
                r["id"] == "qwen-vision" for r in store.routes()
            ):
                store.set_route("qwen-vision", aid, {})
            for model in catalog["models"]:
                slug = re.sub(r"[^A-Za-z0-9_.-]", "-", model["id"])[:80]
                store.set_route(
                    base + "/" + slug,
                    aid,
                    {
                        "model": model["id"],
                        **(
                            {"chatgpt_mode": model.get("mode", "chat")}
                            if value["provider"] == "chatgpt"
                            else {}
                        ),
                    },
                )
        if value["provider"] == "chatgpt":
            prefix = next(
                (
                    r["id"]
                    for r in store.routes()
                    if r["account_id"] == aid and "/" not in r["id"] and not r["config"]
                ),
                "gpt-" + aid[:6],
            )
            auto_alias = prefix + "/auto"
            if not any(r["id"] == auto_alias for r in store.routes()):
                store.set_route(
                    auto_alias, aid, {"model": "auto", "chatgpt_mode": "chat"}
                )
            for route in store.routes():
                entry = next(
                    (
                        m
                        for m in catalog.get("models", [])
                        if m["id"] == route["config"].get("model")
                    ),
                    None,
                )
                if (
                    route["account_id"] == aid
                    and entry
                    and "chatgpt_mode" not in route["config"]
                    and route["id"].endswith(
                        "/" + re.sub(r"[^A-Za-z0-9_.-]", "-", entry["id"])[:80]
                    )
                ):
                    store.set_route(
                        route["id"],
                        aid,
                        {**route["config"], "chatgpt_mode": entry.get("mode", "chat")},
                        bool(route["enabled"]),
                    )
        return catalog

    @app.patch("/admin/api/accounts/{aid}")
    async def edit_account(aid: str, request: Request, who=Depends(admin)):
        value = account(aid)
        data = await body(request)
        patch = data.get("settings", {})
        if not isinstance(patch, dict):
            raise ApiError(400, "إعدادات الحساب غير صالحة.")
        permitted = {
            "project_id",
            "conversation_id",
            "conversation_mode",
            "model",
            "reasoning_effort",
            "thinking_enabled",
            "search_enabled",
            "gemini_paragraph_mode",
            "chatgpt_mode",
            "tool_environment",
            "temperature",
            "max_tokens",
        }
        settings = {
            **value["settings"],
            **{k: v for k, v in patch.items() if k in permitted},
        }
        validate_settings(value, settings)
        store.update_account(
            aid,
            settings=settings,
            label=str(data["label"])[:80] if "label" in data else None,
            enabled=bool(data["enabled"]) if "enabled" in data else None,
        )
        return {"ok": True}

    def validate_settings(value, settings):
        if settings.get("conversation_mode", "automatic") not in (
            "automatic",
            "fixed",
            "per_request",
        ):
            raise ApiError(400, "اختر سلوك محادثة صالحًا.")
        if settings.get("conversation_mode") == "fixed" and not settings.get(
            "conversation_id"
        ):
            raise ApiError(400, "اختر محادثة ثابتة أولًا.")
        if value["provider"] != "chatgpt" and settings.get("project_id"):
            raise ApiError(400, "المشاريع متاحة لحساب ChatGPT فقط.")
        if value["provider"] == "chatgpt":
            if settings.get("tool_environment", "application") not in (
                "application",
                "hybrid",
            ):
                raise ApiError(400, "اختر بيئة أدوات صالحة.")
            try:
                select_model(value["catalog"], settings)
            except ValueError as exc:
                raise ApiError(400, str(exc)) from None
        for key, low, high in [("temperature", 0, 2), ("max_tokens", 256, 131072)]:
            v = settings.get(key, 0.7 if key == "temperature" else 16384)
            if (
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not low <= v <= high
            ):
                raise ApiError(400, "قيمة " + key + " خارج المدى.")
        for key in ("thinking_enabled", "search_enabled", "gemini_paragraph_mode"):
            if key in settings and not isinstance(settings[key], bool):
                raise ApiError(400, "قيمة مفتاح الإعداد غير صالحة.")
        if (
            settings.get("model")
            and value["catalog"].get("models")
            and not any(
                m["id"] == settings["model"] for m in value["catalog"]["models"]
            )
        ):
            raise ApiError(400, "اختر نموذجًا من الحساب.")

    @app.post("/admin/api/accounts/{aid}/credentials")
    async def rotate_credentials(aid: str, request: Request, who=Depends(admin)):
        value = account(aid)
        data = await body(request)
        credential = str(data.get("credential", "")).strip()
        if value["provider"] != "gemini":
            credential = re.sub(r"^Bearer\s+", "", credential, flags=re.I)
        if not 6 <= len(credential) <= 131072:
            raise ApiError(400, "الصق بيانات دخول صالحة.")
        fingerprint = hashlib.sha256(
            (value["provider"] + "\0" + credential).encode()
        ).hexdigest()
        duplicate = store.rows(
            "SELECT id FROM accounts WHERE fingerprint=? AND id!=?", (fingerprint, aid)
        )
        if duplicate:
            raise ApiError(409, "بيانات الدخول مسجلة بالفعل في حساب آخر.", "conflict")
        store.run(
            "UPDATE accounts SET secret=?,fingerprint=?,catalog=?,status=? WHERE id=?",
            (store.seal(credential), fingerprint, "{}", "pending", aid),
        )
        settings = {
            **value["settings"],
            "project_id": "",
            "conversation_id": "",
            "conversation_mode": "automatic",
            "chatgpt_mode": "chat",
            "model": "",
        }
        store.update_account(aid, settings=settings)
        for route in store.routes():
            if route["account_id"] == aid:
                store.set_route(
                    route["id"],
                    aid,
                    {
                        **route["config"],
                        "project_id": "",
                        "conversation_id": "",
                        "conversation_mode": "automatic",
                        "chatgpt_mode": "chat",
                        "model": "",
                    },
                    bool(route["enabled"]),
                )
        for table in ("conversations", "files"):
            store.run(f"DELETE FROM {table} WHERE account_id=?", (aid,))
        store.run("DELETE FROM idempotency")
        upstream.gemini_sessions.pop(aid, None)
        store.event("credential_updated", aid)
        return {"ok": True}

    @app.delete("/admin/api/accounts/{aid}")
    async def delete_account(aid: str, who=Depends(admin)):
        account(aid)
        for table in ("routes", "conversations", "files"):
            store.run(f"DELETE FROM {table} WHERE account_id=?", (aid,))
        store.run("DELETE FROM accounts WHERE id=?", (aid,))
        upstream.gemini_sessions.pop(aid, None)
        store.event("account_deleted", aid)
        return {"ok": True}

    @app.get("/admin/api/accounts/{aid}/projects")
    async def projects(aid: str, cursor: str | None = None, who=Depends(admin)):
        return await upstream.projects(account(aid), cursor)

    @app.get("/admin/api/accounts/{aid}/chats")
    async def chats(
        aid: str,
        project: str = "",
        cursor: str | None = None,
        offset: int = 0,
        limit: int = 50,
        who=Depends(admin),
    ):
        return await upstream.chats(
            account(aid), project, cursor, max(0, offset), min(200, max(1, limit))
        )

    @app.post("/admin/api/accounts/{aid}/new-chat")
    async def new_chat(aid: str, request: Request, who=Depends(admin)):
        value = account(aid)
        data = await body(request)
        settings = {
            **value["settings"],
            **{
                k: v
                for k, v in data.items()
                if k in ("project_id", "model", "chatgpt_mode", "reasoning_effort")
            },
        }
        validate_settings(value, settings)
        state = {}
        await upstream.send(
            value,
            "هذه محادثة جديدة لمساعدي الشخصي. أكّد استعدادك باختصار ثم انتظر رسالتي.",
            state,
            settings,
        )
        store.event("chat_created", aid)
        return {
            "id": state.get("conversation_id"),
            "title": "محادثة جديدة",
            "project_id": settings.get("project_id", ""),
        }

    @app.post("/admin/api/routes")
    async def save_route(request: Request, who=Depends(admin)):
        data = await body(request)
        value = account(data.get("account_id", ""))
        name = str(data.get("id", "")).strip()
        if not name:
            name = value["provider"] + "-" + uuid.uuid4().hex[:6]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,119}", name):
            raise ApiError(
                400,
                "اسم النموذج الظاهر يجب أن يكون اسمًا قصيرًا بأحرف إنجليزية أو أرقام.",
            )
        config = data.get("config", {})
        if not isinstance(config, dict):
            raise ApiError(400, "إعدادات النموذج غير صالحة.")
        allowed = {
            "model",
            "chatgpt_mode",
            "tool_environment",
            "project_id",
            "conversation_id",
            "conversation_mode",
            "reasoning_effort",
            "thinking_enabled",
            "search_enabled",
            "task",
            "temperature",
            "max_tokens",
        }
        config = {k: v for k, v in config.items() if k in allowed}
        validate_settings(value, {**value["settings"], **config})
        if config.get("task", "chat") not in (
            "chat",
            "translation",
            "glossary",
            "review",
        ):
            raise ApiError(400, "نوع المسار غير صالح.")
        store.set_route(name, value["id"], config, bool(data.get("enabled", True)))
        store.event("route_saved", value["id"], name)
        return {"ok": True, "id": name}

    @app.delete("/admin/api/routes/{model:path}")
    async def delete_route(model: str, who=Depends(admin)):
        store.run("DELETE FROM routes WHERE id=?", (model,))
        return {"ok": True}

    @app.post("/admin/api/keys")
    async def create_key(request: Request, who=Depends(admin)):
        data = await body(request)
        models = data.get("models", ["*"])
        if (
            not isinstance(models, list)
            or not models
            or any(not isinstance(x, str) for x in models)
        ):
            raise ApiError(400, "اختر النماذج المسموح بها.")
        return store.add_key(str(data.get("name", "OpenMinis"))[:80], models)

    @app.delete("/admin/api/keys/{kid}")
    async def revoke_key(kid: str, who=Depends(admin)):
        store.run("UPDATE clients SET enabled=0 WHERE id=?", (kid,))
        store.event("key_revoked")
        return {"ok": True}

    @app.post("/admin/api/conversations/{cid}/reset")
    async def reset_conversation(cid: str, who=Depends(admin)):
        store.run("DELETE FROM conversations WHERE id=?", (cid,))
        return {"ok": True}

    @app.get("/v1/models")
    async def models(who=Depends(client)):
        result = []
        for route in store.routes():
            if not route["enabled"] or (
                "*" not in who["models"] and route["id"] not in who["models"]
            ):
                continue
            value = account(route["account_id"])
            if not value["enabled"]:
                continue
            result.append(
                {
                    "id": route["id"],
                    "object": "model",
                    "created": int(value["created"]),
                    "owned_by": value["provider"],
                    "name": next(
                        (
                            m.get("name", m["id"])
                            for m in value["catalog"].get("models", [])
                            if m["id"]
                            == route["config"].get(
                                "model", value["settings"].get("model")
                            )
                        ),
                        route["id"],
                    ),
                    "chatgpt_mode": route["config"].get("chatgpt_mode")
                    or value["settings"].get("chatgpt_mode")
                    or next(
                        (
                            m.get("mode", "chat")
                            for m in value["catalog"].get("models", [])
                            if m["id"]
                            == route["config"].get(
                                "model", value["settings"].get("model")
                            )
                        ),
                        "chat",
                    ),
                    "supports_tools": True,
                    "supports_vision": value["provider"]
                    in ("chatgpt", "gemini", "qwen"),
                    "capabilities": {
                        "tools": "emulated",
                        "vision": value["provider"] in ("chatgpt", "gemini", "qwen"),
                        "parallel_tool_calls": True,
                        "responses_api": True,
                    },
                }
            )
        return {"object": "list", "data": result}

    @app.get("/v1/models/{model:path}")
    async def model_info(model: str, who=Depends(client)):
        found = next((m for m in (await models(who))["data"] if m["id"] == model), None)
        if not found:
            raise ApiError(404, "Model not found.", "model_not_found")
        return found

    async def invoke(data, who, request, response_api=False):
        if response_api:
            messages = response_messages(data)
            if not isinstance(data.get("tools", []), list) or any(
                not isinstance(t, dict) for t in data.get("tools", [])
            ):
                raise ApiError(400, "قائمة الأدوات غير صالحة.")
            if data.get("previous_response_id"):
                found = store.rows(
                    "SELECT * FROM responses WHERE id=? AND client_id=? AND expires>?",
                    (data["previous_response_id"], who["id"], time.time()),
                )
                if not found:
                    raise ApiError(
                        404, "Previous response not found or not stored.", "not_found"
                    )
                messages = store.unseal(found[0]["payload"])["messages"] + messages
            data = {
                **data,
                "messages": messages,
                "tools": [
                    (
                        {
                            "type": "function",
                            "function": {k: v for k, v in t.items() if k != "type"},
                        }
                        if t.get("type") == "function" and "function" not in t
                        else t
                    )
                    for t in data.get("tools", [])
                ],
            }
            if (
                isinstance(data.get("tool_choice"), dict)
                and "name" in data["tool_choice"]
            ):
                data["tool_choice"] = {
                    "type": "function",
                    "function": {"name": data["tool_choice"]["name"]},
                }
            if "max_output_tokens" in data:
                data["max_tokens"] = data["max_output_tokens"]
        engine.validate(data)
        _, request_account = engine.resolve(data.get("model", ""), who)
        explicit = request.headers.get("x-conversation-id") or data.get(
            "conversation_id"
        )
        idempotency = request.headers.get("idempotency-key", "")
        if len(idempotency) > 200:
            raise ApiError(400, "Idempotency-Key is too long.")

        async def generate():
            cachekey = hashlib.sha256(
                (
                    who["id"]
                    + ":"
                    + hashlib.sha256(request_account["credential"].encode()).hexdigest()
                    + ":"
                    + idempotency
                ).encode()
            ).hexdigest()
            digest = hashlib.sha256(
                json.dumps([data, explicit], sort_keys=True).encode()
            ).hexdigest()
            async with engine.lock(
                "request:" + cachekey if idempotency else uuid.uuid4().hex
            ):
                if idempotency:
                    cached = store.rows(
                        "SELECT * FROM idempotency WHERE id=? AND expires>?",
                        (cachekey, time.time()),
                    )
                    if cached:
                        if cached[0]["body_hash"] != digest:
                            raise ApiError(
                                409,
                                "Idempotency-Key was reused for another request.",
                                "conflict",
                            )
                        return store.unseal(cached[0]["payload"])
                result = await engine.complete(data, who, explicit)
                if response_api:
                    raw = result
                    result = as_response(raw)
                    if data.get("store", True):
                        store.run(
                            "INSERT INTO responses VALUES(?,?,?,?)",
                            (
                                result["id"],
                                who["id"],
                                store.seal(
                                    {
                                        "messages": data["messages"]
                                        + [raw["choices"][0]["message"]],
                                        "response": result,
                                    }
                                ),
                                time.time() + 86400,
                            ),
                        )
                if idempotency:
                    store.run(
                        "INSERT OR REPLACE INTO idempotency VALUES(?,?,?,?)",
                        (cachekey, digest, store.seal(result), time.time() + 3600),
                    )
                return result

        if data.get("stream", False):

            async def events():
                task = asyncio.create_task(generate())
                try:
                    while not task.done():
                        done, _ = await asyncio.wait({task}, timeout=10)
                        if not done:
                            yield ": keepalive\n\n"
                    result = await task
                    for chunk in (
                        response_chunks(result)
                        if response_api
                        else chat_chunks(
                            result,
                            data.get("stream_options", {}).get("include_usage", False),
                        )
                    ):
                        yield chunk
                except (ProviderError, ApiError) as exc:
                    err = {
                        "error": {
                            "message": exc.message,
                            "type": "upstream_error",
                            "code": getattr(exc, "code", "upstream_error"),
                        }
                    }
                    if response_api:
                        yield data_event({"type": "error", **err}, "error")
                    else:
                        yield data_event(err)
                        yield "data: [DONE]\n\n"
                except asyncio.CancelledError:
                    task.cancel()
                    raise
                except Exception:
                    yield data_event(
                        {
                            "error": {
                                "message": "تعذر إكمال التوليد؛ لم نعد إرسال الرسالة.",
                                "type": "internal_error",
                                "code": "internal_error",
                            }
                        }
                    )
                    if not response_api:
                        yield "data: [DONE]\n\n"

            return StreamingResponse(
                events(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "X-Request-ID": uuid.uuid4().hex,
                },
            )
        result = await generate()
        return JSONResponse(
            result,
            headers={
                "X-Conversation-ID": result["conversation_id"],
                "X-Request-ID": uuid.uuid4().hex,
            },
        )

    @app.post("/v1/chat/completions")
    async def completions(request: Request, who=Depends(client)):
        return await invoke(await body(request), who, request)

    @app.post("/v1/responses")
    async def responses(request: Request, who=Depends(client)):
        return await invoke(await body(request), who, request, True)

    @app.post("/admin/api/playground")
    async def playground(request: Request, who=Depends(admin)):
        return await invoke(await body(request), who, request)

    @app.post("/v1/images/generations")
    async def image_generation(request: Request, who=Depends(client)):
        data = await body(request)
        route, value = engine.resolve(data.get("model", "qwen"), who)
        if value["provider"] != "qwen":
            raise ApiError(
                400,
                "Image generation is available only through the supplied Qwen image protocol.",
                "unsupported_feature",
            )
        if data.get("n", 1) != 1 or data.get("response_format", "url") != "url":
            raise ApiError(
                400, "Qwen image generation supports n=1 and response_format=url only."
            )
        prompt = data.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ApiError(400, "A prompt is required.")
        return {
            "created": int(time.time()),
            "data": [
                {
                    "url": await upstream.image(
                        value, prompt, str(data.get("size", "1:1"))
                    )
                }
            ],
        }

    @app.post("/v1/files")
    async def upload_file(
        request: Request,
        file: UploadFile = File(...),
        purpose: str = Form("assistants"),
        who=Depends(client),
    ):
        model = request.query_params.get("model")
        if not model:
            raise ApiError(
                400,
                "Specify ?model=your-proxy-model to select the destination account.",
            )
        route, value = engine.resolve(model, who)
        data = await file.read(10 * 1024 * 1024 + 1)
        attachment = await upstream.upload(
            value,
            file.filename or "file",
            file.content_type or "application/octet-stream",
            data,
        )
        if value["provider"] == "qwen":
            attachment["bytes"] = base64.b64encode(attachment["bytes"]).decode()
            attachment["base64"] = True
        fid = "file-" + uuid.uuid4().hex
        store.run(
            "INSERT INTO files VALUES(?,?,?,?,?,?)",
            (
                fid,
                who["id"],
                value["id"],
                file.filename,
                store.seal(attachment),
                time.time() + 86400,
            ),
        )
        return {
            "id": fid,
            "object": "file",
            "bytes": len(data),
            "created_at": int(time.time()),
            "filename": file.filename,
            "purpose": purpose,
            "status": "processed",
        }

    @app.delete("/v1/files/{fid}")
    async def delete_file(fid: str, who=Depends(client)):
        store.run("DELETE FROM files WHERE id=? AND client_id=?", (fid, who["id"]))
        return {"id": fid, "object": "file", "deleted": True}

    return app


app = create_app()
