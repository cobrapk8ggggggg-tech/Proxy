import base64
import json
import httpx
import pytest
from proxy.providers import Providers, ProviderError


def acct(provider):
    return {
        "id": "test-" + provider,
        "provider": provider,
        "credential": (
            "__Secure-1PSID=test-cookie" if provider == "gemini" else "opaque-token"
        ),
    }


def frame(text, cid="remote"):
    return (
        json.dumps(
            [
                [
                    "wrb.fr",
                    None,
                    json.dumps(
                        [
                            None,
                            [cid, "r2"],
                            None,
                            {"26": "private-csrf"},
                            [["choice", [text]]],
                        ]
                    ),
                ]
            ]
        )
        + "\n"
    )


def event(text="Final answer", channel="final", status="finished_successfully"):
    return (
        "data: "
        + json.dumps(
            {
                "conversation_id": "new-remote",
                "message": {
                    "id": "node",
                    "author": {"role": "assistant"},
                    "channel": channel,
                    "status": status,
                    "content": {"parts": [text]},
                },
            }
        )
        + "\n\n"
    )


@pytest.mark.asyncio
async def test_chatgpt_auto_account_models_project_limit_and_project_chats():
    calls = []

    def handle(req):
        calls.append(req)
        if req.url.path.endswith("/accounts/check/v4-2023-04-27"):
            assert "chatgpt-account-id" not in req.headers
            return httpx.Response(200, json={"default_account_id": "detected"})
        assert req.headers["chatgpt-account-id"] == "detected"
        if req.url.path.endswith("/models"):
            return httpx.Response(
                200, json={"models": [{"slug": "auto", "title": "Auto"}]}
            )
        if req.url.path.endswith("/sidebar"):
            assert req.url.params["conversations_per_gizmo"] == "5"
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "gizmo": {
                                "gizmo": {"id": "project", "display": {"name": "Novel"}}
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            200, json={"items": [{"id": "sixth", "title": "Sixth chat"}]}
        )

    p = Providers(httpx.MockTransport(handle))
    result = await p.discover(acct("chatgpt"))
    assert (
        result["models"][0]["id"] == "auto" and result["projects"][0]["name"] == "Novel"
    )
    assert (await p.chats(acct("chatgpt"), "project"))["items"][0]["id"] == "sixth"
    assert any("/gizmos/project/conversations" in str(c.url) for c in calls)


@pytest.mark.asyncio
async def test_chatgpt_stream_ignores_reasoning_and_refreshes_parent_project():
    def handle(req):
        if "/accounts/check/" in req.url.path:
            return httpx.Response(200, json={"default_account_id": "a"})
        if req.method == "GET":
            return httpx.Response(
                200, json={"current_node": "latest-node", "gizmo_id": "project"}
            )
        body = json.loads(req.content)
        assert (
            body["parent_message_id"] == "latest-node"
            and body["conversation_id"] == "existing"
        )
        assert (
            body["conversation_mode"]["gizmo_id"] == "project"
            and body["messages"][0]["metadata"]["gizmo_id"] == "project"
        )
        assert body["thinking_effort"] == "high"
        return httpx.Response(
            200,
            text=event("private reasoning", "analysis")
            + event("جواب نهائي.")
            + "data: [DONE]\n\n",
        )

    p = Providers(httpx.MockTransport(handle))
    state = {"conversation_id": "existing"}
    assert (
        await p.send(
            acct("chatgpt"),
            "Full chapter",
            state,
            {"project_id": "project", "reasoning_effort": "high"},
        )
        == "جواب نهائي."
    )
    assert state["conversation_id"] == "new-remote"


@pytest.mark.asyncio
async def test_chatgpt_partial_completion_is_error_and_post_never_retries():
    posts = []

    def handle(req):
        if req.method == "GET":
            return httpx.Response(200, json={"default_account_id": "a"})
        posts.append(req)
        return httpx.Response(200, text=event("partial", status="in_progress"))

    p = Providers(httpx.MockTransport(handle))
    with pytest.raises(ProviderError):
        await p.send(acct("chatgpt"), "prompt", {}, {})
    assert len(posts) == 1


@pytest.mark.asyncio
async def test_chatgpt_upload_does_not_forward_account_authorization_to_storage():
    def handle(req):
        if "/accounts/check/" in req.url.path:
            return httpx.Response(200, json={"default_account_id": "a"})
        if req.url.path.endswith("/files"):
            return httpx.Response(
                200,
                json={
                    "file_id": "file1",
                    "upload_url": "https://storage.example/upload",
                },
            )
        if req.url.host == "storage.example":
            assert (
                "authorization" not in req.headers
                and "chatgpt-account-id" not in req.headers
            )
            assert req.content == b"chapter"
        return httpx.Response(200, json={})

    p = Providers(httpx.MockTransport(handle))
    result = await p.upload(acct("chatgpt"), "chapter.txt", "text/plain", b"chapter")
    assert result["attachment"]["id"] == "file1"


@pytest.mark.asyncio
async def test_gemini_auto_models_chat_continuation_revisions_and_cookies():
    def handle(req):
        if req.method == "GET":
            return httpx.Response(
                200, text='"SNlM0e":"csrf","FdrFJe":"-12","cfb2h":"live-build"'
            )
        from urllib.parse import parse_qs

        form = parse_qs(req.content.decode())
        if req.url.path.endswith("/batchexecute"):
            rpc = req.url.params["rpcids"]
            if rpc == "otAQ7b":
                model = ["model-auto"] + [None] * 10 + ["Auto"]
                result = [None] * 14 + [None, [model], [], []]
            elif rpc == "MaZiqc":
                result = [None, None, [["cid", "Novel"]]]
            else:
                turn = [["cid", "rid"], None, None, [[["choice", ["old"]]]]]
                result = [[turn]]
            return httpx.Response(
                200,
                text=")]}'\n100\n" + json.dumps([["wrb.fr", rpc, json.dumps(result)]]),
            )
        assert req.url.params["bl"] == "live-build"
        assert json.loads(req.headers["x-goog-ext-525001261-jspb"])[4] == "model-auto"
        payload = json.loads(json.loads(form["f.req"][0])[1])
        assert payload[2][:3] == ["cid", "rid", "choice"]
        return httpx.Response(
            200,
            text=frame("A longer old snapshot", "cid") + frame("نسخة منقّحة.", "cid"),
            headers={"set-cookie": "SIDCC=rotated; Secure"},
        )

    p = Providers(httpx.MockTransport(handle))
    a = acct("gemini")
    catalog = await p.discover(a)
    assert (
        catalog["models"][0] == {"id": "model-auto", "name": "Auto"}
        and catalog["chats"][0]["title"] == "Novel"
    )
    state = await p.conversation(a, "cid")
    result = await p.send(a, "prompt", state, {"model": "model-auto"})
    assert result == "نسخة منقّحة." and state["response_id"] == "r2"
    assert "SIDCC=rotated" in p.gemini_sessions[a["id"]]["cookies"]
    assert "private-csrf" not in json.dumps(catalog)


@pytest.mark.asyncio
async def test_worker_catalog_and_session_flags_parent_zero():
    def handle(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "deepseek-chat"}]})
        body = json.loads(req.content)
        assert body["token"] == "opaque-token"
        if req.url.path.endswith("/sessions/list"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "sessions": [{"id": "s", "title": "Novel", "last_response_id": 0}],
                },
            )
        assert body["session_id"] == "s" and body["parent_message_id"] == 0
        assert (
            body["temperature"] == 0
            and body["thinking_enabled"]
            and not body["search_enabled"]
        )
        return httpx.Response(
            200,
            json={
                "response": "answer",
                "session_id": "s",
                "parent_message_id": 2,
                "thinking": "not output",
            },
        )

    p = Providers(httpx.MockTransport(handle))
    a = acct("deepseek")
    catalog = await p.discover(a)
    assert catalog["chats"][0]["parent_message_id"] == 0
    state = {"conversation_id": "s", "parent_message_id": 0}
    assert (
        await p.send(
            a,
            "prompt",
            state,
            {"temperature": 0, "thinking_enabled": True, "search_enabled": False},
        )
        == "answer"
    )
    assert state["parent_message_id"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"error": "private token error"},
        {"response": ""},
        {"response": "partial", "finish_reason": "length"},
    ],
)
async def test_worker_does_not_return_error_or_partial_as_text(response):
    p = Providers(httpx.MockTransport(lambda req: httpx.Response(200, json=response)))
    with pytest.raises(ProviderError):
        await p.send(acct("deepseek"), "prompt", {}, {})


@pytest.mark.asyncio
async def test_qwen_account_transport_models_parent_and_no_thinking_leak():
    def handle(req):
        if req.method == "GET":
            return httpx.Response(
                200, json={"data": [{"id": "qwen-account-model", "name": "Qwen"}]}
            )
        if req.url.path.endswith("/chats/new"):
            return httpx.Response(200, json={"id": "qwen-session"})
        body = json.loads(req.content)
        assert body["model"] == "qwen-account-model"
        return httpx.Response(
            200,
            text="data: "
            + json.dumps(
                {
                    "response_id": "parent-2",
                    "choices": [{"delta": {"phase": "thinking", "content": "secret"}}],
                }
            )
            + "\n\ndata: "
            + json.dumps(
                {"choices": [{"delta": {"phase": "answer", "content": "Answer"}}]}
            )
            + "\n\ndata: [DONE]\n\n",
        )

    p = Providers(httpx.MockTransport(handle))
    a = acct("qwen")
    catalog = await p.discover(a)
    assert catalog["models"][0]["id"] == "qwen-account-model"
    state = {}
    assert await p.send(a, "prompt", state, {"model": "qwen-account-model"}) == "Answer"
    assert (
        state["conversation_id"] == "qwen-session"
        and state["parent_message_id"] == "parent-2"
    )


@pytest.mark.asyncio
async def test_provider_http_errors_never_expose_headers_or_raw_response():
    p = Providers(
        httpx.MockTransport(
            lambda req: httpx.Response(403, text="sensitive upstream detail")
        )
    )
    with pytest.raises(ProviderError) as error:
        await p.discover(acct("deepseek"))
    assert "sensitive" not in str(error.value) and "opaque-token" not in str(
        error.value
    )


@pytest.mark.asyncio
async def test_gemini_preserves_extended_context_for_three_turns():
    from urllib.parse import parse_qs

    calls = []

    def handle(req):
        data = json.loads(json.loads(parse_qs(req.content.decode())["f.req"][0])[1])
        calls.append(data[2])
        if len(calls) > 1:
            assert data[2][9] == "context-" + str(len(calls) - 1)
            assert data[2][3] == "extended-state"
        payload = [None] * 26
        payload[1] = [
            "remote-chat",
            "response-" + str(len(calls)),
            None,
            "extended-state",
        ]
        payload[4] = [["choice", ["reply"]]]
        payload[25] = "context-" + str(len(calls))
        return httpx.Response(
            200, text=json.dumps([["wrb.fr", None, json.dumps(payload)]])
        )

    p = Providers(httpx.MockTransport(handle))

    async def session(a):
        return {
            "models": [],
            "cookies": "test",
            "index": 0,
            "build": "build",
            "sid": "sid",
            "csrf": "csrf",
        }

    p.gemini_session = session
    state = {}
    for _ in range(3):
        assert await p.send(acct("gemini"), "hello", state, {}) == "reply"
    assert len(calls) == 3 and state["conversation_id"] == "remote-chat"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["gemini", "deepseek"])
async def test_provider_cannot_silently_replace_existing_session(provider):
    def handle(req):
        if provider == "deepseek":
            return httpx.Response(
                200,
                json={
                    "session_id": "unexpected-new-session",
                    "parent_message_id": 2,
                    "response": "reply",
                },
            )
        return httpx.Response(200, text=frame("reply", "unexpected-new-session"))

    p = Providers(httpx.MockTransport(handle))

    async def session(a):
        return {
            "models": [],
            "cookies": "test",
            "index": 0,
            "build": "build",
            "sid": "sid",
            "csrf": "csrf",
        }

    p.gemini_session = session
    state = {"conversation_id": "original-session"}
    with pytest.raises(ProviderError, match="بدل"):
        await p.send(acct(provider), "Continue", state, {})
    assert state["conversation_id"] == "original-session"
