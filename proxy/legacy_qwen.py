"""
Universal AI Proxy  v10.0
=========================
التحسينات الجديدة عن v9:
  ① إصلاح Tool Call: استخراج الأداة حتى لو كانت مدفونة في نص عادي
      - parse_tool_call تبحث في كامل النص (re.search بدل re.match)
      - has_tool_call() مساعدة تكشف وجود أداة في أي مكان
      - clean_text() لا تُشغَّل إذا وُجدت أداة
  ② دعم الصور (Vision/Multimodal):
      - رفع الصور لـ Qwen OSS تلقائياً
      - قبول image_url في messages بصيغة OpenAI
      - /v1/models يُعلن vision: true لنموذج qwen
      - capabilities endpoint جديد
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

# ══════════════════════════════════════════════════════════
# Logging
# ══════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("ai_proxy")
# Legacy logs included signed storage URLs and proxy credentials. Structured events replace them.
log.disabled = True


# ══════════════════════════════════════════════════════════
# Proxy Manager (Qwen فقط)
# ══════════════════════════════════════════════════════════

PROXY_FILE = os.environ.get("PROXY_FILE", "proxies.txt")


class QwenProxyManager:
    def __init__(self):
        self._proxies: List[str] = []
        self._banned: set = set()
        self._idx = 0
        self._lock = asyncio.Lock()
        self._session_proxy: Dict[str, str] = {}
        self._load()

    def _load(self):
        if not os.path.exists(PROXY_FILE):
            log.warning(
                "Proxy file '%s' not found — Qwen will run without proxy", PROXY_FILE
            )
            return
        with open(PROXY_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if not line.startswith("http"):
                    line = "http://" + line
                self._proxies.append(line)
        log.info(
            "ProxyManager: loaded %d proxies from %s", len(self._proxies), PROXY_FILE
        )

    @property
    def enabled(self) -> bool:
        return len(self._proxies) > 0

    def _available(self) -> List[str]:
        return [p for p in self._proxies if p not in self._banned]

    async def get_for_session(self, conv_id: str) -> Optional[str]:
        async with self._lock:
            if not self._proxies:
                return None
            avail = self._available()
            if not avail:
                log.warning("ProxyManager: all proxies banned! resetting ban list")
                self._banned.clear()
                avail = self._proxies[:]

            if conv_id in self._session_proxy:
                p = self._session_proxy[conv_id]
                if p in avail:
                    return p
                log.info(
                    "ProxyManager: session %s proxy was banned, reassigning",
                    conv_id[:16],
                )

            p = avail[self._idx % len(avail)]
            self._idx += 1
            self._session_proxy[conv_id] = p
            return p

    async def mark_banned(self, proxy_url: str, conv_id: str) -> Optional[str]:
        async with self._lock:
            if proxy_url in self._proxies:
                self._banned.add(proxy_url)
                log.warning(
                    "ProxyManager: banned proxy %s (total banned: %d/%d)",
                    proxy_url,
                    len(self._banned),
                    len(self._proxies),
                )
            avail = self._available()
            if not avail:
                log.warning("ProxyManager: no available proxies! resetting")
                self._banned.clear()
                avail = self._proxies[:]
            if not avail:
                return None
            p = avail[self._idx % len(avail)]
            self._idx += 1
            self._session_proxy[conv_id] = p
            log.info(
                "ProxyManager: conv %s switched to proxy %s",
                conv_id[:16],
                p.split("@")[-1],
            )
            return p

    def get_httpx_proxies(self, proxy_url: str) -> Dict[str, str]:
        return {"http://": proxy_url, "https://": proxy_url}


proxy_manager = QwenProxyManager()


# ══════════════════════════════════════════════════════════
# Session Store
# ══════════════════════════════════════════════════════════
SESSION_TTL = 60 * 60 * 6

_sessions: Dict[str, Dict[str, Any]] = {}
_session_lock = asyncio.Lock()


def _session_key(token: str, conv_id: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest() + ":" + conv_id


async def _get_session(token: str, conv_id: str) -> Optional[Dict]:
    async with _session_lock:
        sess = _sessions.get(_session_key(token, conv_id))
        if sess:
            sess["last_used"] = time.time()
        return dict(sess) if sess else None


async def _set_session(token: str, conv_id: str, data: Dict) -> None:
    async with _session_lock:
        _sessions[_session_key(token, conv_id)] = {**data, "last_used": time.time()}


async def _update_session(token: str, conv_id: str, **kwargs) -> None:
    async with _session_lock:
        key = _session_key(token, conv_id)
        if key in _sessions:
            _sessions[key].update(kwargs)
            _sessions[key]["last_used"] = time.time()


async def _clear_session(token: str, conv_id: str) -> None:
    async with _session_lock:
        key = _session_key(token, conv_id)
        if key in _sessions:
            del _sessions[key]
            log.info("Session cleared: %s", conv_id[:16])


async def _evict_old_sessions() -> None:
    async with _session_lock:
        now = time.time()
        stale = [k for k, v in _sessions.items() if now - v["last_used"] > SESSION_TTL]
        for k in stale:
            del _sessions[k]
        if stale:
            log.info("Evicted %d stale sessions", len(stale))


# ══════════════════════════════════════════════════════════
# conv_id ثابت
# ══════════════════════════════════════════════════════════


def _compute_conv_id(
    messages: List[Dict],
    explicit_id: Optional[str] = None,
) -> str:
    if explicit_id:
        return explicit_id
    anchor = messages[:1] if messages else [{"role": "user", "content": "init"}]
    raw = json.dumps(anchor, ensure_ascii=False, sort_keys=True)
    return "conv_" + hashlib.md5(raw.encode()).hexdigest()


def _is_regenerate_request(body: Dict) -> bool:
    if body.get("regenerate") or body.get("resend"):
        return True
    if body.get("action") in ("regenerate", "resend", "retry"):
        return True
    extra = body.get("extra_body") or {}
    if extra.get("regenerate") or extra.get("action") in ("regenerate", "resend"):
        return True
    return False


# ══════════════════════════════════════════════════════════
# ① إصلاح Tool Call — المنطق المحسّن
# ══════════════════════════════════════════════════════════


def _has_tool_call(text: str) -> bool:
    """كشف سريع: هل يوجد استدعاء أداة في النص؟"""
    if re.search(r"ACTION:\s*\w+\|\s*\{", text):
        return True
    if re.search(r"<tool_call>", text, re.IGNORECASE):
        return True
    if re.search(r"<name>.*?</name>", text, re.IGNORECASE | re.DOTALL):
        return True
    return False


def parse_tool_call(text: str) -> Optional[Dict]:
    """
    يبحث عن استدعاء الأداة في أي مكان من النص (مدفون أو منفرد).
    ← التغيير الجوهري: re.search بدلاً من re.match أو (?m)^
    """
    # صيغة ACTION (في أي مكان من النص)
    m = re.search(r"ACTION:\s*(\w+)\|\s*(\{.*?\})", text, re.DOTALL)
    if m:
        return _make_tc(m.group(1), m.group(2))

    # صيغة <tool_call>
    m = re.search(
        r"<tool_call>\s*<name>(.*?)</name>\s*<arguments>(.*?)</arguments>\s*</tool_call>",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if m:
        return _make_tc(m.group(1), m.group(2))

    # صيغة <name> + <parameter>
    m_name = re.search(r"<name>(.*?)</name>", text, re.IGNORECASE)
    m_params = re.findall(
        r"<parameter[=:](\w+)>\s*(.*?)\s*</parameter>",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if m_name and m_params:
        params = {p: v for p, v in m_params}
        return _make_tc(m_name.group(1).strip(), json.dumps(params, ensure_ascii=False))

    return None


def _make_tc(name: str, args_raw: str) -> Dict:
    name = name.strip()
    try:
        args_obj = json.loads(args_raw)
    except json.JSONDecodeError:
        try:
            args_obj = json.loads(args_raw.replace("'", '"'))
        except Exception:
            args_obj = {"raw": args_raw.strip()}
    return {"name": name, "arguments": json.dumps(args_obj, ensure_ascii=False)}


def clean_text(text: str) -> str:
    """يُزيل أجزاء الأداة من النص — لا يُستدعى إذا وُجدت أداة."""
    text = re.sub(r"ACTION:\s*\S+\|.*?(?=\n|$)", "", text, flags=re.DOTALL)
    text = re.sub(
        r"<tool_call>.*?</tool_call>", "", text, flags=re.DOTALL | re.IGNORECASE
    )
    text = re.sub(r"</?function[^>]*>", "", text, flags=re.IGNORECASE)
    return text.strip()


# ══════════════════════════════════════════════════════════
# ② OSS Upload — رفع الصور إلى Qwen (مستلهم من Discord bot)
# ══════════════════════════════════════════════════════════

QWEN_BASE = "https://chat.qwen.ai/api/v2"
QWEN_MODEL_ID_REAL = "qwen3.8-max"

_UA_APP = (
    "Dalvik/2.1.0 (Linux; U; Android 15; RMX3834 Build/AP3A.240905.015.A2) "
    "AliApp(QWENCHAT/2.7.2) AppType/Release AplusBridgeLite"
)


def _oss_signature(
    secret_key: str,
    method: str,
    content_md5: str,
    content_type: str,
    date: str,
    canonical_headers: str,
    canonical_resource: str,
) -> str:
    string_to_sign = (
        f"{method}\n{content_md5}\n{content_type}\n{date}\n"
        f"{canonical_headers}{canonical_resource}"
    )
    h = hmac.new(secret_key.encode(), string_to_sign.encode(), hashlib.sha1)
    return base64.b64encode(h.digest()).decode()


async def _qwen_upload_image_oss(
    token: str,
    image_bytes: bytes,
    client: httpx.AsyncClient,
    filename: Optional[str] = None,
) -> Dict:
    """
    رفع صورة إلى Qwen OSS وإرجاع payload الملف جاهزاً للإرسال.
    يتبع نفس منطق Discord bot بالضبط.
    """
    if not filename:
        filename = f"{uuid.uuid4()}_IMG.jpg"

    file_size = str(len(image_bytes))

    # ── 1. الحصول على STS token
    sts_url = f"{QWEN_BASE}/files/getstsToken"
    sts_headers = {
        "User-Agent": _UA_APP,
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "x-device-id": "0",
        "source": "app",
        "x-request-id": str(uuid.uuid4()),
        "Cookie": f"x-ap=eu-central-1; token={token}",
    }
    payload = {"filename": filename, "filetype": "image", "filesize": file_size}
    r = await client.post(sts_url, json=payload, headers=sts_headers, timeout=60)
    res = r.json()

    if "data" not in res:
        raise HTTPException(status_code=502, detail="Qwen rejected image upload")

    sts = res["data"]
    access_key_id = sts["access_key_id"]
    access_key_secret = sts["access_key_secret"]
    security_token = sts["security_token"]
    file_path = sts["file_path"]
    file_id = sts["file_id"]
    bucket = sts["bucketname"]
    host = f"{bucket}.{sts['endpoint']}"
    canon_sec_header = f"x-oss-security-token:{security_token}\n"

    def _gmt() -> str:
        return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")

    def _oss_base_headers(
        method: str,
        content_md5: str,
        content_type: str,
        canon_resource: str,
        content_length: str = "0",
    ) -> Dict:
        gmt = _gmt()
        sig = _oss_signature(
            access_key_secret,
            method,
            content_md5,
            content_type,
            gmt,
            canon_sec_header,
            canon_resource,
        )
        return {
            "Authorization": f"OSS {access_key_id}:{sig}",
            "User-Agent": "aliyun-sdk-android/2.9.21",
            "Host": host,
            "x-oss-security-token": security_token,
            "Date": gmt,
            "Content-Type": content_type,
            "Content-Length": content_length,
        }

    # ── 2. Initiate multipart upload
    init_url = f"https://{host}/{file_path}?uploads"
    init_hdrs = _oss_base_headers(
        "POST", "", "image/jpeg", f"/{bucket}/{file_path}?uploads"
    )
    init_r = await client.post(init_url, headers=init_hdrs, timeout=60)
    root = ET.fromstring(init_r.text)
    upload_id = root.find("{*}UploadId").text

    # ── 3. رفع الجزء الوحيد
    content_md5 = base64.b64encode(hashlib.md5(image_bytes).digest()).decode()
    part_url = f"https://{host}/{file_path}?uploadId={upload_id}&partNumber=1"
    part_res = f"/{bucket}/{file_path}?partNumber=1&uploadId={upload_id}"
    part_hdrs = _oss_base_headers("PUT", content_md5, "image/jpeg", part_res, file_size)
    part_hdrs["Content-MD5"] = content_md5
    pr = await client.put(part_url, content=image_bytes, headers=part_hdrs, timeout=120)
    etag = pr.headers.get("ETag", "").replace('"', "")

    # ── 4. Complete multipart upload
    complete_body = (
        f"<CompleteMultipartUpload>"
        f"<Part><PartNumber>1</PartNumber><ETag>{etag}</ETag></Part>"
        f"</CompleteMultipartUpload>"
    )
    complete_url = f"https://{host}/{file_path}?uploadId={upload_id}"
    comp_res = f"/{bucket}/{file_path}?uploadId={upload_id}"
    comp_hdrs = _oss_base_headers(
        "POST", "", "image/jpeg", comp_res, str(len(complete_body))
    )
    await client.post(
        complete_url, content=complete_body.encode(), headers=comp_hdrs, timeout=60
    )

    signed_url = sts.get("file_url", f"https://{host}/{file_path}")
    log.info("OSS upload OK: file_id=%s url=%s", file_id, signed_url[:60])

    return {
        "type": "image",
        "file": {
            "data": {},
            "filename": filename,
            "id": file_id,
            "meta": {"name": filename},
        },
        "id": file_id,
        "filename": filename,
        "name": filename,
        "url": signed_url,
        "image_width": 1024,
        "image_height": 1024,
    }


# ══════════════════════════════════════════════════════════
# ② استخراج الصور من messages بصيغة OpenAI
# ══════════════════════════════════════════════════════════


def _extract_images_from_messages(
    messages: List[Dict],
) -> Tuple[List[Dict], List[Dict]]:
    """
    يفحص messages ويُخرج:
      - messages_clean: نفس الرسائل لكن content الصور أصبح نص فقط [IMAGE]
      - image_items: قائمة بالصور {data: bytes, filename: str, msg_index: int}
    """
    image_items: List[Dict] = []
    messages_clean = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if isinstance(content, list):
            text_parts = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type", "")
                if ptype == "text":
                    text_parts.append(part.get("text", ""))
                elif ptype == "image_url":
                    img_url_obj = part.get("image_url", {})
                    url = (
                        img_url_obj.get("url", "")
                        if isinstance(img_url_obj, dict)
                        else str(img_url_obj)
                    )
                    fname = f"img_{len(image_items)}_{uuid.uuid4().hex[:6]}.jpg"
                    if url.startswith("data:"):
                        # base64 inline
                        try:
                            header, b64 = url.split(",", 1)
                            raw = base64.b64decode(b64)
                            image_items.append(
                                {"data": raw, "filename": fname, "url_type": "base64"}
                            )
                            text_parts.append(f"[IMAGE:{fname}]")
                        except Exception as e:
                            log.warning("Failed to decode base64 image: %s", e)
                            text_parts.append("[IMAGE:decode_error]")
                    elif url.startswith("http"):
                        image_items.append(
                            {
                                "data": None,
                                "url": url,
                                "filename": fname,
                                "url_type": "remote",
                            }
                        )
                        text_parts.append(f"[IMAGE:{fname}]")
                    else:
                        text_parts.append("[IMAGE:unknown]")

            clean_msg = dict(msg)
            clean_msg["content"] = " ".join(text_parts)
            messages_clean.append(clean_msg)
        else:
            messages_clean.append(dict(msg))

    return messages_clean, image_items


async def _resolve_images(
    image_items: List[Dict],
    token: str,
    client: httpx.AsyncClient,
) -> List[Dict]:
    """
    يُحمّل الصور البعيدة إذا لزم ثم يرفعها لـ Qwen OSS.
    يُرجع قائمة file_payload جاهزة للـ Qwen messages.
    """
    uploaded = []
    for item in image_items:
        raw = item.get("data")
        if raw is None and item.get("url_type") == "remote":
            # تحميل الصورة من الرابط
            try:
                r = await client.get(
                    item["url"],
                    timeout=60,
                    follow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                r.raise_for_status()
                raw = r.content
            except Exception as e:
                log.warning("Failed to download image %s: %s", item.get("url", ""), e)
                continue
        if raw:
            try:
                fp = await _qwen_upload_image_oss(token, raw, client, item["filename"])
                uploaded.append(fp)
            except Exception as e:
                log.warning("Failed to upload image to OSS: %s", e)
    return uploaded


# ══════════════════════════════════════════════════════════
# Backend Architecture
# ══════════════════════════════════════════════════════════


class BaseBackend(ABC):
    @property
    @abstractmethod
    def model_id(self) -> str: ...

    @abstractmethod
    async def complete(
        self,
        token: str,
        messages: List[Dict],
        tools: List[Dict],
        thinking: bool,
        conv_id: str,
        extra: Dict,
    ) -> AsyncIterator[str]: ...


_BACKENDS: Dict[str, "BaseBackend"] = {}


def register_backend(backend: "BaseBackend") -> None:
    _BACKENDS[backend.model_id] = backend
    log.info("Registered backend: %s", backend.model_id)


def get_backend(model_id: str) -> Optional["BaseBackend"]:
    return _BACKENDS.get(model_id)


# ══════════════════════════════════════════════════════════
# Shared Tools
# ══════════════════════════════════════════════════════════

TOOL_SYSTEM_SUFFIX = """
══════════════════════════════════════════════
TOOL USE — STRICT FORMAT
══════════════════════════════════════════════
When you need to call a tool, your ENTIRE response MUST be ONLY this exact line:

ACTION: tool_name|{"param1": "value1"}

Rules:
  • Output ONLY the ACTION line — no words before or after it
  • ONE tool call per response — never two at once
  • Use valid JSON (double quotes)
  • NEVER fabricate tool results — wait for [TOOL RESULT: ...] blocks
  • When finished (no more tools needed) → write your answer as plain text

Tool results arrive as:
  [TOOL RESULT: tool_name]
  ...result content...
  [/TOOL RESULT]
══════════════════════════════════════════════
"""


def tools_to_xml(tools: List[Dict]) -> str:
    if not tools:
        return ""
    lines = ["<available_tools>"]
    for tool in tools:
        func = tool.get("function") or tool
        name = func.get("name", "unknown")
        desc = func.get("description", "")
        params = func.get("parameters", {})
        required = params.get("required", [])
        properties = params.get("properties", {})
        lines.append("  <tool>")
        lines.append(f"    <name>{name}</name>")
        lines.append(f"    <description>{desc}</description>")
        if properties:
            lines.append("    <parameters>")
            for pname, pinfo in properties.items():
                ptype = pinfo.get("type", "string")
                pdesc = pinfo.get("description", "")
                req = " required" if pname in required else ""
                lines.append(
                    f'      <param name="{pname}" type="{ptype}"{req}>{pdesc}</param>'
                )
            lines.append("    </parameters>")
        lines.append("  </tool>")
    lines.append("</available_tools>")
    return "\n".join(lines)


def messages_to_prompt(messages: List[Dict], tools: List[Dict]) -> Tuple[str, str]:
    system_parts = []
    conv_parts = []

    for m in messages:
        if m.get("role") == "system":
            content = m.get("content") or ""
            if isinstance(content, list):
                content = " ".join(
                    c.get("text", "") for c in content if isinstance(c, dict)
                )
            system_parts.append(str(content))

    if tools:
        system_parts.append(f"\n{tools_to_xml(tools)}\n{TOOL_SYSTEM_SUFFIX}")

    for m in messages:
        role = m.get("role", "user")
        content = m.get("content") or ""

        if role == "system":
            continue
        elif role == "user":
            if isinstance(content, list):
                text_parts = []
                for c in content:
                    if isinstance(c, dict):
                        if c.get("type") == "text":
                            text_parts.append(c.get("text", ""))
                        elif c.get("type") == "image_url":
                            text_parts.append("[IMAGE]")
                content = " ".join(text_parts)
            conv_parts.append(f"User: {content}")
        elif role == "assistant":
            if isinstance(content, list):
                content = " ".join(
                    c.get("text", "")
                    for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                )
            tool_calls = m.get("tool_calls", [])
            if tool_calls:
                for tc in tool_calls:
                    func = tc.get("function", {})
                    tc_name = func.get("name", "")
                    tc_args = func.get("arguments", "{}")
                    conv_parts.append(f"ACTION: {tc_name}|{tc_args}")
            else:
                conv_parts.append(f"Assistant: {content}")
        elif role in ("tool", "function"):
            tool_name = m.get("name") or m.get("tool_call_id", "tool")
            if isinstance(content, list):
                content = str(content)
            conv_parts.append(f"[TOOL RESULT: {tool_name}]\n{content}\n[/TOOL RESULT]")

    return "\n\n".join(system_parts), "\n\n".join(conv_parts)


def build_full_prompt(messages: List[Dict], tools: List[Dict]) -> str:
    system_text, conv_text = messages_to_prompt(messages, tools)
    parts = []
    if system_text:
        parts.append(f"[SYSTEM]\n{system_text}\n[/SYSTEM]")
    if conv_text:
        parts.append(conv_text)
    parts.append("Assistant:")
    return "\n\n".join(parts)


def make_tc_response(tc: Dict, model: str) -> Dict:
    call_id = f"call_{uuid.uuid4().hex[:24]}"
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": tc["arguments"],
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def make_text_response(content: str, model: str) -> Dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def sse_chunk(
    content: str = "",
    model: str = "qwen",
    *,
    finish: bool = False,
    tc: Optional[Dict] = None,
    call_id: Optional[str] = None,
) -> str:
    cid = call_id or f"call_{uuid.uuid4().hex[:24]}"
    if tc and not finish:
        delta = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "index": 0,
                    "id": cid,
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                }
            ],
        }
        choice = {"index": 0, "delta": delta, "finish_reason": None}
    elif finish and tc:
        choice = {"index": 0, "delta": {}, "finish_reason": "tool_calls"}
    elif finish:
        choice = {"index": 0, "delta": {}, "finish_reason": "stop"}
    else:
        choice = {"index": 0, "delta": {"content": content}, "finish_reason": None}
    obj = {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [choice],
    }
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


# ══════════════════════════════════════════════════════════
# BACKEND 1: Qwen (مع دعم Proxy + Vision)
# ══════════════════════════════════════════════════════════

QWEN_PROXY_ID = "qwen"
REQUEST_TIMEOUT = 180
QWEN_BAN_CODES = {403, 429, 451}

_UA_CHAT = (
    "Dalvik/2.1.0 (Linux; U; Android 15; RMX3834 Build/AP3A.240905.015.A2) "
    "AliApp(QWENCHAT/2.7.2) AppType/Release AplusBridgeLite"
)
_UA_NEW = (
    "Dalvik/2.1.0 (Linux; U; Android 15; RMX3834 Build/AP3A.240905.015.A2),"
    "Dalvik/2.1.0 (Linux; U; Android 15; RMX3834 Build/AP3A.240905.015.A2) "
    "AliApp(QWENCHAT/2.7.2) AppType/Release AplusBridgeLite"
)


def _qwen_headers_chat(token: str, *, stream: bool = False) -> Dict[str, str]:
    return {
        "User-Agent": _UA_CHAT,
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "*/*,text/event-stream" if stream else "application/json",
        "Accept-Language": "en-US",
        "Accept-Encoding": "gzip, deflate",
        "Cache-Control": "no-store",
        "Connection": "Keep-Alive",
        "Host": "chat.qwen.ai",
        "X-Platform": "android",
        "x-device-id": "0",
        "source": "app",
        "Authorization": f"Bearer {token}",
        "x-request-id": str(uuid.uuid4()),
        "Cookie": f"x-ap=eu-central-1; token={token}",
    }


def _qwen_headers_new(token: str) -> Dict[str, str]:
    return {
        "User-Agent": _UA_NEW,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Accept-Language": "en-US",
        "Accept-Encoding": "gzip",
        "Connection": "Keep-Alive",
        "Host": "chat.qwen.ai",
        "X-Platform": "android",
        "x-device-id": "0",
        "source": "app",
        "Authorization": f"Bearer {token}",
        "x-request-id": str(uuid.uuid4()),
        "Cookie": f"x-ap=eu-central-1; token={token}",
    }


def _qwen_is_rate_limited(obj: Any) -> bool:
    if isinstance(obj, str):
        return "RateLimited" in obj
    if isinstance(obj, dict):
        if obj.get("code") == "RateLimited":
            return True
        data = obj.get("data")
        if isinstance(data, dict) and data.get("code") == "RateLimited":
            return True
        try:
            return "RateLimited" in json.dumps(obj)
        except Exception:
            return False
    return False


def _qwen_is_antibot(line: str) -> bool:
    return "_____tmd_____" in line or "punish" in line


def _make_qwen_client(proxy_url: Optional[str] = None) -> httpx.AsyncClient:
    if proxy_url:
        return httpx.AsyncClient(proxy=proxy_url)
    return httpx.AsyncClient()


async def _qwen_create_chat(token: str, client: httpx.AsyncClient) -> str:
    url = f"{QWEN_BASE}/chats/new"
    payload = {"chat_mode": "normal", "project_id": ""}
    resp = await client.post(
        url, json=payload, headers=_qwen_headers_new(token), timeout=60
    )
    if resp.status_code in QWEN_BAN_CODES:
        raise BannedProxyError(f"HTTP {resp.status_code}")
    resp.raise_for_status()
    data = resp.json()
    cid = (
        data.get("chat_id")
        or data.get("id")
        or (data.get("data") or {}).get("chat_id")
        or (data.get("data") or {}).get("id")
    )
    if not cid:
        raise HTTPException(status_code=502, detail="Qwen rejected chat creation")
    return cid


class BannedProxyError(Exception):
    pass


def _qwen_build_payload(
    chat_id,
    prompt,
    parent_id,
    *,
    chat_type="t2t",
    thinking=False,
    auto_search=False,
    files=None,
    size="1:1",
) -> Dict:
    ts = int(time.time())
    fid = str(uuid.uuid4())
    return {
        "stream": True,
        "incremental_output": True,
        "chatId": chat_id,
        "chat_id": chat_id,
        "chat_mode": "normal",
        "model": QWEN_MODEL_ID_REAL,
        "messages": [
            {
                "id": None,
                "fid": fid,
                "chat_type": chat_type,
                "content": prompt,
                "role": "user",
                "feature_config": {
                    "output_schema": "phase",
                    "thinking_enabled": thinking,
                    "thinking_format": "summary",
                    "auto_thinking": thinking,
                    "auto_search": auto_search,
                },
                "timestamp": ts,
                "sub_chat_type": chat_type,
                "models": [QWEN_MODEL_ID_REAL],
                "model": "",
                "files": files or [],
                "user_action": "chat",
                "extra": {"meta": {"subChatType": chat_type}},
                "parentId": parent_id,
                "parent_id": parent_id,
            }
        ],
        "timestamp": ts,
        "size": size,
        "share_id": "",
        "version": "2.1",
        "origin_branch_message_id": "",
        "parentId": parent_id or "",
        "parent_id": parent_id,
    }


async def _qwen_stream_collect(
    token: str,
    chat_id: str,
    payload: Dict,
    client: httpx.AsyncClient,
) -> Tuple[str, Optional[str], bool]:
    url = f"{QWEN_BASE}/chat/completions"
    full_txt = ""
    resp_id: Optional[str] = None
    was_banned = False
    finished = False

    try:
        async with client.stream(
            "POST",
            url,
            json=payload,
            headers=_qwen_headers_chat(token, stream=True),
            params={"chat_id": chat_id},
            timeout=REQUEST_TIMEOUT,
        ) as resp:
            if resp.status_code in QWEN_BAN_CODES:
                log.warning("Qwen: HTTP %d → proxy banned", resp.status_code)
                return "", None, True

            resp.raise_for_status()
            async for raw_line in resp.aiter_lines():
                if not raw_line:
                    continue
                if _qwen_is_antibot(raw_line):
                    raise HTTPException(502, "Qwen bot challenge")
                if _qwen_is_rate_limited(raw_line):
                    raise HTTPException(429, "Qwen rate limited")
                if not raw_line.startswith("data: "):
                    continue
                ds = raw_line[6:].strip()
                if ds == "[DONE]":
                    finished = True
                    break
                try:
                    obj = json.loads(ds)
                    if _qwen_is_rate_limited(obj):
                        raise HTTPException(429, "Qwen rate limited")
                    rid = obj.get("response_id") or obj.get("choices", [{}])[0].get(
                        "delta", {}
                    ).get("response_id")
                    if rid:
                        resp_id = rid
                    choices = obj.get("choices", [])
                    if not choices:
                        continue
                    if choices[0].get("finish_reason") in ("stop", "tool_calls"):
                        finished = True
                    delta = choices[0].get("delta", {})
                    if delta.get("phase", "") not in ("answer", ""):
                        continue
                    content = delta.get("content", "")
                    if content:
                        full_txt += content
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue

    except httpx.ProxyError as e:
        log.warning("Qwen: ProxyError → %s", e)
        return "", None, False

    if not full_txt.strip() or not finished:
        raise HTTPException(502, "Qwen returned an empty or incomplete response")
    return full_txt, resp_id, was_banned


class QwenBackend(BaseBackend):
    @property
    def model_id(self) -> str:
        return QWEN_PROXY_ID

    async def complete(
        self, token, messages, tools, thinking, conv_id, extra
    ) -> AsyncIterator[str]:
        # ── ② استخراج الصور من messages
        messages_clean, image_items = _extract_images_from_messages(messages)
        prompt = build_full_prompt(messages_clean, tools)
        if not prompt.strip():
            return
        await _evict_old_sessions()

        MAX_PROXY_RETRIES = 3
        uploaded_files: List[Dict] = []

        for attempt in range(MAX_PROXY_RETRIES + 1):
            proxy_url = (
                await proxy_manager.get_for_session(conv_id)
                if proxy_manager.enabled
                else None
            )

            sess = await _get_session(token, conv_id)
            if sess:
                qwen_chat_id = sess["qwen_chat_id"]
                parent_id = sess.get("parent_id")
            else:
                try:
                    async with _make_qwen_client(proxy_url) as tmp:
                        qwen_chat_id = await _qwen_create_chat(token, tmp)

                        # ── رفع الصور عند إنشاء المحادثة (مرة واحدة فقط)
                        if image_items and not uploaded_files:
                            try:
                                uploaded_files = await _resolve_images(
                                    image_items, token, tmp
                                )
                                log.info(
                                    "Qwen: uploaded %d image(s) to OSS",
                                    len(uploaded_files),
                                )
                            except Exception as e:
                                log.warning("Qwen: image upload failed: %s", e)

                except BannedProxyError:
                    if proxy_url and attempt < MAX_PROXY_RETRIES:
                        log.warning(
                            "Qwen: proxy banned during chat creation, switching"
                        )
                        proxy_url = await proxy_manager.mark_banned(proxy_url, conv_id)
                        continue
                    yield sse_chunk(
                        "[Qwen Error: all proxies banned]", model=self.model_id
                    )
                    yield sse_chunk(model=self.model_id, finish=True)
                    yield "data: [DONE]\n\n"
                    return
                parent_id = None
                await _set_session(
                    token,
                    conv_id,
                    {
                        "qwen_chat_id": qwen_chat_id,
                        "parent_id": parent_id,
                        "qwen_proxy": proxy_url,
                    },
                )

            # ── رفع الصور إذا كانت جلسة قديمة ولم ترفع بعد
            if image_items and not uploaded_files:
                async with _make_qwen_client(proxy_url) as tmp2:
                    try:
                        uploaded_files = await _resolve_images(image_items, token, tmp2)
                    except Exception as e:
                        log.warning(
                            "Qwen: image upload (existing session) failed: %s", e
                        )

            payload = _qwen_build_payload(
                qwen_chat_id,
                prompt,
                parent_id,
                thinking=thinking,
                files=uploaded_files if uploaded_files else None,
            )

            async with _make_qwen_client(proxy_url) as client:
                qwen_text, last_rid, was_banned = await _qwen_stream_collect(
                    token, qwen_chat_id, payload, client
                )

            if was_banned and proxy_url and attempt < MAX_PROXY_RETRIES:
                log.info(
                    "Qwen: switching proxy (attempt %d/%d)",
                    attempt + 1,
                    MAX_PROXY_RETRIES,
                )
                proxy_url = await proxy_manager.mark_banned(proxy_url, conv_id)
                await _clear_session(token, conv_id)
                continue

            await _update_session(token, conv_id, parent_id=last_rid)
            break

        # ── ① إصلاح Tool Call: نكشف الأداة أولاً قبل clean_text
        tc = parse_tool_call(qwen_text)
        if tc:
            call_id = f"call_{uuid.uuid4().hex[:24]}"
            yield sse_chunk(tc=tc, model=self.model_id, call_id=call_id)
            yield sse_chunk(tc=tc, model=self.model_id, call_id=call_id, finish=True)
            yield "data: [DONE]\n\n"
        else:
            # لا توجد أداة → نظّف النص العادي
            txt = clean_text(qwen_text) or "[Qwen: empty response]"
            for i in range(0, max(len(txt), 1), 40):
                yield sse_chunk(txt[i : i + 40], model=self.model_id)
            yield sse_chunk(model=self.model_id, finish=True)
            yield "data: [DONE]\n\n"


register_backend(QwenBackend())
