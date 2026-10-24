"""Keep native account tools separate from OpenAI client-side execution."""

import json
import httpx
import pytest
from proxy.providers import Providers, ProviderError
from proxy.protocol import prompt_for, chat_chunks, as_response

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "shell_execute",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    }
]


def streamed(message):
    return (
        "data: "
        + json.dumps({"conversation_id": "remote", "message": message})
        + "\n\n"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "native",
    [
        {
            "author": {"role": "assistant"},
            "recipient": "container.exec",
            "channel": "analysis",
        },
        {
            "author": {"role": "assistant"},
            "recipient": "python",
            "channel": "commentary",
        },
        {"author": {"role": "tool", "name": "python"}, "recipient": "all"},
    ],
)
async def test_external_policy_rejects_native_execution_without_retry(native):
    posts = []

    def handle(req):
        if req.method == "GET":
            return httpx.Response(200, json={"default_account_id": "account"})
        posts.append(req)
        return httpx.Response(200, text=streamed(native))

    provider = Providers(httpx.MockTransport(handle))
    account = {"id": "test", "provider": "chatgpt", "credential": "test-token"}
    with pytest.raises(ProviderError, match="أداة داخلية"):
        await provider.send(
            account, "Request external shell", {}, {"_client_tools": True}
        )
    assert len(posts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settings", [{"_client_tools": True, "tool_environment": "hybrid"}, {}]
)
async def test_hybrid_and_plain_chat_allow_native_tools_without_exposing_reasoning(
    settings,
):
    def handle(req):
        if req.method == "GET":
            return httpx.Response(200, json={"default_account_id": "account"})
        return httpx.Response(
            200,
            text=(
                streamed(
                    {
                        "author": {"role": "assistant"},
                        "recipient": "all",
                        "channel": "analysis",
                        "content": {"parts": ["private reasoning"]},
                    }
                )
                + streamed(
                    {
                        "author": {"role": "assistant"},
                        "recipient": "python",
                        "channel": "analysis",
                    }
                )
                + streamed(
                    {
                        "author": {"role": "tool"},
                        "content": {"parts": ["internal output"]},
                    }
                )
                + streamed(
                    {
                        "author": {"role": "assistant"},
                        "recipient": "all",
                        "channel": "final",
                        "status": "finished_successfully",
                        "content": {"parts": ["Calculated"]},
                    }
                )
            ),
        )

    state = {}
    provider = Providers(httpx.MockTransport(handle))
    reply = await provider.send(
        {"id": "test", "provider": "chatgpt", "credential": "test-token"},
        "Calculate",
        state,
        settings,
    )
    assert reply == "Calculated" and state["native_tools_used"] is True


def test_execution_prompt_distinguishes_app_results_and_tool_choice_none():
    message = [
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "shell_execute",
            "content": "actual output",
        }
    ]
    strict = prompt_for(message, TOOLS, "auto", True)
    assert (
        "TWO SEPARATE ENVIRONMENTS" in strict and "do not invoke ANY native" in strict
    )
    assert "APPLICATION TOOL RESULT shell_execute (call_1)" in strict
    hybrid = prompt_for(message, TOOLS, "auto", True, "hybrid")
    assert (
        "Native ChatGPT tools are permitted ONLY" in hybrid
        and "do not invoke ANY native" not in hybrid
    )
    disabled = prompt_for(message, TOOLS, "none", True)
    assert (
        "do not request any application tools" in disabled
        and "do not invoke ANY native" in disabled
    )
    assert "EXECUTION ENVIRONMENT CONTRACT" not in prompt_for(
        [{"role": "user", "content": "Hello"}], [], "auto", True
    )


def test_environment_setting_is_validated_and_separates_conversations(env):
    c, app, p, account, h = env
    path = "/admin/api/accounts/" + account["id"]
    assert (
        c.patch(path, json={"settings": {"tool_environment": "unknown"}}).status_code
        == 400
    )
    body = {
        "model": "gpt",
        "messages": [{"role": "user", "content": "Hello"}],
        "tools": TOOLS,
    }
    headers = {**h, "X-Conversation-ID": "same-device-conversation"}
    first = c.post("/v1/chat/completions", headers=headers, json=body).json()
    assert p.sent[-1]["settings"]["_client_tools"] is True
    assert (
        c.patch(path, json={"settings": {"tool_environment": "hybrid"}}).status_code
        == 200
    )
    second = c.post("/v1/chat/completions", headers=headers, json=body).json()
    assert second["conversation_id"] != first["conversation_id"]
    assert not p.sent[-1]["state"].get("conversation_id")
    assert second["execution"]["policy"] == "hybrid"
    # Client request fields cannot override the policy chosen by the administrator.
    c.post(
        "/v1/chat/completions",
        headers=headers,
        json={**body, "tool_environment": "application"},
    )
    assert p.sent[-1]["settings"]["tool_environment"] == "hybrid"


def test_native_provenance_is_metadata_only_and_tool_call_has_no_added_text(env):
    c, app, p, account, h = env
    c.patch(
        "/admin/api/accounts/" + account["id"],
        json={"settings": {"tool_environment": "hybrid"}},
    )

    async def reply(a, prompt, state, settings, attachments=None):
        state["native_tools_used"] = True
        return "Calculated internally"

    p.send = reply
    body = {
        "model": "gpt",
        "messages": [{"role": "user", "content": "Calculate"}],
        "tools": TOOLS,
    }
    result = c.post("/v1/chat/completions", headers=h, json=body).json()
    assert result["choices"][0]["message"]["content"] == "Calculated internally"
    assert result["execution"]["native_tools_observed"] is True

    async def call(a, prompt, state, settings, attachments=None):
        state["native_tools_used"] = True
        return json.dumps(
            {"tool_calls": [{"name": "shell_execute", "arguments": {"command": "pwd"}}]}
        )

    p.send = call
    result = c.post("/v1/chat/completions", headers=h, json=body).json()
    assert result["choices"][0]["message"]["content"] is None
    assert result["choices"][0]["finish_reason"] == "tool_calls"
    assert result["execution"]["application_tool_calls"] == 1
    chunks = [
        json.loads(s.split("data: ", 1)[1])
        for s in chat_chunks(result)
        if "[DONE]" not in s
    ]
    assert not any(chunk["choices"][0]["delta"].get("content") for chunk in chunks)
    assert any(chunk["choices"][0]["delta"].get("tool_calls") for chunk in chunks)
    response = as_response(result)
    assert response["execution"]["native_tools_observed"] is True
    assert {item["type"] for item in response["output"]} == {"function_call"}


@pytest.mark.asyncio
async def test_external_json_is_not_native_execution_and_old_trace_is_cleared():
    reply = json.dumps(
        {"tool_calls": [{"name": "shell_execute", "arguments": {"command": "pwd"}}]}
    )

    def handle(req):
        if req.method == "GET":
            return httpx.Response(200, json={"default_account_id": "account"})
        return httpx.Response(
            200,
            text=streamed(
                {
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "channel": "final",
                    "status": "finished_successfully",
                    "content": {"parts": [reply]},
                }
            ),
        )

    state = {"native_tools_used": True}
    result = await Providers(httpx.MockTransport(handle)).send(
        {"id": "test", "provider": "chatgpt", "credential": "test-token"},
        "Request app tool",
        state,
        {"_client_tools": True},
    )
    assert result == reply and not state.get("native_tools_used")


@pytest.mark.asyncio
@pytest.mark.parametrize("second_native", [False, True])
async def test_application_mode_corrects_once_then_fails_closed(second_native):
    posts = []
    native = streamed(
        {"author": {"role": "assistant"}, "recipient": "python", "channel": "analysis"}
    )

    def final(text):
        return streamed(
            {
                "author": {"role": "assistant"},
                "channel": "final",
                "status": "finished_successfully",
                "content": {"parts": [text]},
            }
        )

    def handle(req):
        if req.method == "GET":
            return httpx.Response(
                200, json={"current_node": "latest", "default_account_id": "a"}
            )
        posts.append(json.loads(req.content))
        if len(posts) == 1 or second_native:
            return httpx.Response(200, text=native + final("Internal output"))
        return httpx.Response(
            200,
            text=final(
                '{"tool_calls":[{"name":"shell_execute","arguments":{"command":"pwd"}}]}'
            ),
        )

    p = Providers(httpx.MockTransport(handle))
    state = {}
    a = {"id": "test", "provider": "chatgpt", "credential": "test-token"}
    if second_native:
        with pytest.raises(ProviderError, match="تصحيح واحدة"):
            await p.send(a, "Original request", state, {"_client_tools": True})
    else:
        text = await p.send(a, "Original request", state, {"_client_tools": True})
        assert json.loads(text)["tool_calls"][0]["name"] == "shell_execute"
        assert state["tool_route_corrected"] and state["native_tools_used"]
    assert len(posts) == 2
    assert posts[1]["conversation_id"] == "remote"
    assert (
        "wrong execution environment" in posts[1]["messages"][0]["content"]["parts"][0]
    )
