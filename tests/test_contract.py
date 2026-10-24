import json
import pytest
from proxy.providers import ProviderError


def completion(c, h, messages=None, **extra):
    return c.post(
        "/v1/chat/completions",
        headers=h,
        json={
            "model": "gpt",
            "messages": (
                messages
                if messages is not None
                else [{"role": "user", "content": "Hello"}]
            ),
            **extra,
        },
    )


def test_auth_and_catalog(env):
    c, a, p, account, h = env
    assert c.get("/v1/models").status_code == 401
    data = c.get("/v1/models", headers=h).json()["data"]
    assert {m["id"] for m in data} >= {"gpt", "gpt/auto", "gpt/test-pro"}
    assert c.get("/v1/models/unknown", headers=h).status_code == 404
    assert completion(c, h, model="unknown").status_code == 404


def test_full_chapter_no_paragraphs_and_stable_stream_id(env):
    c, a, p, account, h = env
    source = "A novel chapter. " * 700
    result = completion(
        c,
        h,
        [{"role": "user", "content": source}],
        stream=True,
        stream_options={"include_usage": True},
    )
    data = [
        json.loads(line[6:])
        for line in result.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert len({d["id"] for d in data}) == 1
    assert data[0]["choices"][0]["delta"]["role"] == "assistant"
    assert (
        data[-2]["choices"][0]["finish_reason"] == "stop" and data[-1]["usage"] is None
    )
    assert source in p.sent[0]["prompt"] and "[[END_" not in p.sent[0]["prompt"]
    assert result.text.endswith("data: [DONE]\n\n")


def test_independent_new_chats_resume_by_full_history_and_project(env):
    c, a, p, account, h = env
    assert (
        c.patch(
            "/admin/api/accounts/" + account["id"],
            json={"settings": {"project_id": "novel-project"}},
        ).status_code
        == 200
    )
    start = [
        {"role": "system", "content": "Same system prompt"},
        {"role": "user", "content": "Hello"},
    ]
    first = completion(c, h, start).json()
    second = completion(c, h, start).json()
    assert first["conversation_id"] != second["conversation_id"]
    third = completion(
        c,
        h,
        start
        + [first["choices"][0]["message"], {"role": "user", "content": "Continue"}],
    ).json()
    assert third["conversation_id"] not in (
        first["conversation_id"],
        second["conversation_id"],
    )
    unique = [
        {"role": "system", "content": "Same system prompt"},
        {"role": "user", "content": "A unique question"},
    ]
    result = completion(c, h, unique).json()
    following = completion(
        c,
        h,
        unique + [result["choices"][0]["message"], {"role": "user", "content": "Next"}],
    ).json()
    assert following["conversation_id"] == result["conversation_id"]
    assert (
        "A unique question" not in p.sent[-1]["prompt"]
        and "Next" in p.sent[-1]["prompt"]
    )
    assert all(x["settings"]["project_id"] == "novel-project" for x in p.sent)


def test_explicit_ids_and_idempotency(env):
    c, a, p, account, h = env
    one = completion(
        c, {**h, "X-Conversation-ID": "ios-chat-a", "Idempotency-Key": "req-a"}
    ).json()
    two = completion(
        c, {**h, "X-Conversation-ID": "ios-chat-a", "Idempotency-Key": "req-a"}
    ).json()
    assert one == two and len(p.sent) == 1
    three = completion(
        c,
        {**h, "X-Conversation-ID": "ios-chat-a"},
        [{"role": "user", "content": "New message"}],
    ).json()
    assert three["conversation_id"] == one["conversation_id"]
    assert (
        completion(
            c,
            {**h, "X-Conversation-ID": "ios-chat-a", "Idempotency-Key": "req-a"},
            [{"role": "user", "content": "Different"}],
        ).status_code
        == 409
    )


def test_key_scope_revocation_and_account_disable(env):
    c, a, p, account, h = env
    token = c.post(
        "/admin/api/keys", json={"name": "restricted", "models": ["gpt/auto"]}
    ).json()["key"]
    restricted = {"Authorization": "Bearer " + token}
    assert completion(c, restricted).status_code == 403
    assert [
        x["id"] for x in c.get("/v1/models", headers=restricted).json()["data"]
    ] == ["gpt/auto"]
    kid = a.state.store.client(token)["id"]
    assert c.delete("/admin/api/keys/" + kid).status_code == 200
    assert c.get("/v1/models", headers=restricted).status_code == 401
    assert (
        c.patch(
            "/admin/api/accounts/" + account["id"], json={"enabled": False}
        ).status_code
        == 200
    )
    assert completion(c, h).status_code == 503


def test_tools_nested_json_and_tool_result_followup(env):
    c, a, p, account, h = env
    original = p.send

    async def tool_reply(*args, **kwargs):
        await original(*args, **kwargs)
        return 'ACTION: lookup|{"query":{"name":"a } brace","nested":{"ids":[1,2]}}}'

    p.send = tool_reply
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "object"}},
                    "required": ["query"],
                },
            },
        }
    ]
    first = completion(c, h, tools=tools).json()
    message = first["choices"][0]["message"]
    call = message["tool_calls"][0]
    assert first["choices"][0]["finish_reason"] == "tool_calls"
    assert json.loads(call["function"]["arguments"])["query"]["nested"]["ids"] == [1, 2]
    p.send = original
    result = completion(
        c,
        h,
        [
            {"role": "user", "content": "Hello"},
            message,
            {"role": "tool", "tool_call_id": call["id"], "content": "Found"},
        ],
        tools=tools,
    ).json()
    assert result["conversation_id"] == first["conversation_id"]
    assert (
        "APPLICATION TOOL RESULT lookup" in p.sent[-1]["prompt"]
        and "Found" in p.sent[-1]["prompt"]
    )


def test_tool_choice_none_preserves_action_text_and_schema_rejected(env):
    c, a, p, account, h = env

    async def send(*args, **kwargs):
        return 'ACTION: lookup|{"x":1}'

    p.send = send
    tools = [
        {
            "type": "function",
            "function": {"name": "lookup", "parameters": {"type": "object"}},
        }
    ]
    result = completion(c, h, tools=tools, tool_choice="none").json()
    assert result["choices"][0]["message"]["content"].startswith("ACTION:")
    tools[0]["function"]["parameters"] = {"$ref": "http://127.0.0.1/private"}
    assert completion(c, h, tools=tools).status_code == 400


def test_error_is_not_successful_assistant_content(env):
    c, a, p, account, h = env
    p.failure = ProviderError("Provider rejected token")
    response = completion(c, h)
    assert (
        response.status_code == 502
        and response.json()["error"]["type"] == "upstream_error"
    )
    response = completion(c, h, stream=True)
    assert '"error"' in response.text and '"finish_reason":"stop"' not in response.text


def test_responses_api_and_previous_response(env):
    c, a, p, account, h = env
    first = c.post(
        "/v1/responses", headers=h, json={"model": "gpt", "input": "Hello"}
    ).json()
    assert first["object"] == "response" and first["output"][0]["content"][0]["text"]
    second = c.post(
        "/v1/responses",
        headers=h,
        json={
            "model": "gpt",
            "input": "Next",
            "previous_response_id": first["id"],
            "stream": True,
        },
    )
    assert (
        "event: response.created" in second.text
        and "event: response.output_text.delta" in second.text
        and "event: response.completed" in second.text
    )
    unstored = c.post(
        "/v1/responses",
        headers=h,
        json={"model": "gpt", "input": "Not stored", "store": False},
    ).json()
    assert (
        c.post(
            "/v1/responses",
            headers=h,
            json={
                "model": "gpt",
                "input": "Next",
                "previous_response_id": unstored["id"],
            },
        ).status_code
        == 404
    )


def test_registered_credentials_are_visible_but_never_revealed(env, tmp_path):
    c, a, p, account, h = env
    response = c.post(
        "/v1/accounts/register",
        headers=h,
        json={"provider": "deepseek", "credential": "another-upstream-secret"},
    )
    assert response.status_code == 200
    state = c.get("/admin/api/state").json()
    added = next(x for x in state["accounts"] if x["provider"] == "deepseek")
    assert not added["enabled"] and added["source"].startswith("client:")
    assert "another-upstream-secret" not in json.dumps(
        state
    ) and "fake-upstream-token" not in json.dumps(state)
    assert b"another-upstream-secret" not in (tmp_path / "proxy.db").read_bytes()
    assert (
        c.post(
            "/admin/api/accounts",
            headers={"Origin": "https://evil.invalid"},
            json={"provider": "chatgpt", "credential": "secret"},
        ).status_code
        == 403
    )


def test_false_gemini_toggle_survives_api_reload(env):
    c, a, p, account, h = env
    gem = c.post(
        "/admin/api/accounts",
        json={"provider": "gemini", "credential": "SID=fake-cookie"},
    ).json()
    for flag in (True, False, True, False):
        assert (
            c.patch(
                "/admin/api/accounts/" + gem["id"],
                json={"settings": {"gemini_paragraph_mode": flag}},
            ).status_code
            == 200
        )
        assert (
            next(
                x
                for x in c.get("/admin/api/state").json()["accounts"]
                if x["id"] == gem["id"]
            )["settings"]["gemini_paragraph_mode"]
            is flag
        )


def test_invalid_requests_and_unknown_messages(env):
    c, a, p, account, h = env
    for patch in (
        {"messages": []},
        {"messages": [{"role": "user", "content": [{"type": "input_audio"}]}]},
        {"temperature": 9},
        {"max_completion_tokens": 0},
        {"n": 2},
        {"tool_choice": "invalid"},
    ):
        assert completion(c, h, **patch).status_code == 400
    assert (
        c.post("/v1/chat/completions", headers=h, content="not-json").status_code == 400
    )


def test_local_data_image_upload_is_forwarded_and_remote_url_rejected(env):
    import base64

    c, a, p, account, h = env
    url = "data:image/png;base64," + base64.b64encode(b"fake-png").decode()
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Read image"},
                {"type": "image_url", "image_url": {"url": url}},
            ],
        }
    ]
    assert completion(c, h, messages).status_code == 200
    assert p.sent[-1]["attachments"][0]["attachment"]["id"] == "upstream-file"
    messages[0]["content"][1]["image_url"]["url"] = "http://127.0.0.1/private"
    assert completion(c, h, messages).status_code == 400


def test_multipart_file_upload_and_ownership(env):
    c, a, p, account, h = env
    response = c.post(
        "/v1/files?model=gpt",
        headers=h,
        files={"file": ("chapter.txt", b"A chapter", "text/plain")},
        data={"purpose": "assistants"},
    )
    assert response.status_code == 200
    fid = response.json()["id"]
    assert (
        completion(
            c,
            h,
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "file", "file": {"file_id": fid}},
                        {"type": "text", "text": "Read chapter"},
                    ],
                }
            ],
        ).status_code
        == 200
    )
    assert c.delete("/v1/files/" + fid, headers=h).json()["deleted"]


def test_gemini_paragraphs_only_on_translation_route_and_can_be_turned_off(env):
    import re

    c, a, p, account, h = env
    gem = c.post(
        "/admin/api/accounts",
        json={"provider": "gemini", "credential": "SID=fake-cookie"},
    ).json()
    c.post("/admin/api/accounts/" + gem["id"] + "/connect")
    c.patch(
        "/admin/api/accounts/" + gem["id"],
        json={"settings": {"gemini_paragraph_mode": True}},
    )
    c.post(
        "/admin/api/routes",
        json={
            "id": "gemini-translation",
            "account_id": gem["id"],
            "config": {"task": "translation"},
        },
    )
    original = p.send

    async def marked(*args, **kwargs):
        await original(*args, **kwargs)
        prompt = args[1]
        nonce = re.search(r"\[\[END_([a-f0-9]+)\]\]", prompt)
        if not nonce:
            return "Normal answer"
        markers = re.findall(r"\[\[" + nonce[1] + r"_\d{6}\]\]", prompt)
        return (
            "\n".join(m + "\n" + "ترجمة عربية مفصلة دون حذف. " * 100 for m in markers)
            + "\n"
            + nonce[0]
        )

    p.send = marked
    source = "First detailed paragraph. " * 130 + "\n\n" + "Last event. " * 150
    result = completion(
        c, h, [{"role": "user", "content": source}], model="gemini-translation"
    )
    assert (
        result.status_code == 200
        and len(p.sent) > 1
        and "[[END_" not in result.json()["choices"][0]["message"]["content"]
    )
    p.sent.clear()
    c.patch(
        "/admin/api/accounts/" + gem["id"],
        json={"settings": {"gemini_paragraph_mode": False}},
    )
    assert (
        completion(
            c, h, [{"role": "user", "content": source}], model="gemini-translation"
        ).status_code
        == 200
    )
    assert len(p.sent) == 1 and "[[END_" not in p.sent[0]["prompt"]
    p.sent.clear()
    c.patch(
        "/admin/api/accounts/" + gem["id"],
        json={"settings": {"gemini_paragraph_mode": True}},
    )
    assert (
        completion(
            c, h, [{"role": "user", "content": source}], model="gemini"
        ).status_code
        == 200
    )
    assert len(p.sent) == 1 and "[[END_" not in p.sent[0]["prompt"]


def test_credential_rotation_preserves_aliases_but_clears_old_context(env):
    c, app, p, account, h = env
    aid = account["id"]
    path = "/admin/api/accounts/" + aid
    c.patch(path, json={"settings": {"project_id": "novel-project"}})
    headers = {
        **h,
        "Idempotency-Key": "before-rotation",
        "X-Conversation-ID": "ios-chat",
    }
    first = completion(c, headers).json()
    assert app.state.store.rows("SELECT * FROM conversations")
    assert (
        c.post(
            path + "/credentials",
            json={"credential": "Bearer replacement-upstream-token"},
        ).status_code
        == 200
    )
    updated = app.state.store.account(aid)
    assert updated["credential"] == "replacement-upstream-token"
    assert updated["settings"]["project_id"] == "" and updated["catalog"] == {}
    assert not app.state.store.rows("SELECT * FROM conversations")
    assert c.post(path + "/connect").status_code == 200
    second = completion(c, headers).json()
    assert second["conversation_id"] != first["conversation_id"] and len(p.sent) == 2
    assert c.get("/v1/models", headers=h).status_code == 200
    assert "replacement-upstream-token" not in c.get("/admin/api/state").text
    other = c.post(
        "/admin/api/accounts",
        json={"provider": "chatgpt", "credential": "another-upstream-token"},
    ).json()
    assert (
        c.post(
            path + "/credentials", json={"credential": "another-upstream-token"}
        ).status_code
        == 409
    )
    assert app.state.store.account(aid)["credential"] == "replacement-upstream-token"


@pytest.mark.parametrize(
    "extra",
    [
        {"response_format": None},
        {"response_format": "text"},
        {"stream_options": []},
        {"tool_choice": {"type": "function", "function": "invalid"}},
        {"max_completion_tokens": 1.5},
        {"messages": [{"role": "assistant", "tool_calls": [{}]}]},
        {"messages": [{"role": "tool", "content": "missing id"}]},
        {"messages": [{"role": "user", "content": [{"type": "text", "text": {}}]}]},
    ],
)
def test_malformed_client_fields_return_400_without_sending(env, extra):
    c, app, p, account, h = env
    response = completion(c, h, **extra)
    assert response.status_code == 400 and "error" in response.json()
    assert not p.sent


@pytest.mark.parametrize(
    "extra",
    [
        {
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_audio", "data": "abc"}],
                }
            ]
        },
        {"input": [{"type": "function_call"}]},
        {"input": [{"type": "function_call_output"}]},
        {"input": "Hello", "tools": [None]},
    ],
)
def test_invalid_responses_fields_fail_before_upstream(env, extra):
    c, app, p, account, h = env
    response = c.post("/v1/responses", headers=h, json={"model": "gpt", **extra})
    assert response.status_code == 400 and "error" in response.json()
    assert not p.sent


def test_qwen_file_bytes_are_encrypted_and_recovered_for_next_request(env):
    c, app, p, account, h = env
    value = c.post(
        "/admin/api/accounts",
        json={"provider": "qwen", "credential": "qwen-test-token"},
    ).json()
    assert c.post("/admin/api/accounts/" + value["id"] + "/connect").status_code == 200

    async def upload(a, name, mime, data):
        return {"bytes": data, "name": name, "mime": mime}

    p.upload = upload
    uploaded = c.post(
        "/v1/files?model=qwen",
        headers=h,
        files={"file": ("chapter.txt", b"Qwen uploaded chapter", "text/plain")},
    )
    assert uploaded.status_code == 200
    result = completion(
        c,
        h,
        model="qwen",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Read"},
                    {"type": "file", "file": {"file_id": uploaded.json()["id"]}},
                ],
            }
        ],
    )
    assert result.status_code == 200
    assert p.sent[-1]["attachments"][0]["bytes"] == b"Qwen uploaded chapter"
    assert "Qwen uploaded chapter" not in str(
        app.state.store.rows("SELECT * FROM files")
    )


def test_changing_client_system_instructions_preserves_same_remote_chat(env):
    c, app, p, account, h = env
    messages = [
        {"role": "system", "content": "Current time: 10:00. Memory: old."},
        {"role": "user", "content": "Remember a code."},
    ]
    first = completion(c, h, messages).json()
    messages[0]["content"] = "Current time: 11:00. Memory: updated."
    second = completion(
        c,
        h,
        messages
        + [first["choices"][0]["message"], {"role": "user", "content": "Repeat it."}],
    ).json()
    assert first["conversation_id"] == second["conversation_id"]
    assert p.sent[-1]["state"].get("conversation_id")
    assert "Memory: updated." in p.sent[-1]["prompt"]
    assert "Remember a code." not in p.sent[-1]["prompt"]


def test_client_reformatted_tool_arguments_do_not_create_another_chat(env):
    c, app, p, account, h = env
    original = p.send

    async def call(*args, **kwargs):
        await original(*args, **kwargs)
        return '{"tool_calls":[{"name":"lookup","arguments":{"b":2,"a":1}}]}'

    p.send = call
    tools = [
        {
            "type": "function",
            "function": {"name": "lookup", "parameters": {"type": "object"}},
        }
    ]
    messages = [{"role": "user", "content": "Look up the code."}]
    first = completion(c, h, messages, tools=tools).json()
    m = first["choices"][0]["message"]
    m["tool_calls"][0]["function"]["arguments"] = '{ "a": 1, "b": 2 }'
    p.send = original
    second = completion(
        c,
        h,
        messages
        + [
            m,
            {
                "role": "tool",
                "tool_call_id": m["tool_calls"][0]["id"],
                "content": "actual result",
            },
        ],
        tools=tools,
    ).json()
    assert second["conversation_id"] == first["conversation_id"]


def test_fixed_remote_chat_uses_durable_state_even_when_provider_list_is_empty(env):
    c, app, p, account, h = env
    # A remote chat created from the dashboard must be resumable after reopening.
    app.state.store.run(
        "UPDATE accounts SET provider='deepseek' WHERE id=?", (account["id"],)
    )
    created = c.post(
        "/admin/api/accounts/" + account["id"] + "/new-chat", json={}
    ).json()
    assert created.get("id")
    assert app.state.store.remote_state(account["id"], created["id"])

    async def missing(*args):
        raise AssertionError("Cached remote state should avoid the empty provider list")

    p.conversation = missing
    assert (
        c.patch(
            "/admin/api/accounts/" + account["id"],
            json={
                "settings": {
                    "conversation_mode": "fixed",
                    "conversation_id": created["id"],
                }
            },
        ).status_code
        == 200
    )
    first = completion(c, h).json()
    second = completion(
        c,
        h,
        [
            {"role": "user", "content": "Hello"},
            first["choices"][0]["message"],
            {"role": "user", "content": "Continue"},
        ],
    ).json()
    assert second["conversation_id"] == first["conversation_id"]
    assert p.sent[-1]["state"]["conversation_id"] == created["id"]


def test_old_remote_session_is_recovered_and_survives_restart(tmp_path):
    from proxy.store import Store
    import time

    state = {
        "conversation_id": "original-upstream-session",
        "parent_message_id": 0,
        "at_token": "private-context-secret",
    }
    store = Store(tmp_path)
    account = store.add_account("deepseek", "fake-recovery-token")
    store.run(
        "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "old-local-chat",
            account["id"],
            "client",
            "scope",
            None,
            "old-format-hash",
            store.seal(state),
            time.time(),
            time.time(),
        ),
    )
    assert store.remote_state(account["id"], state["conversation_id"]) == state
    assert "private-context-secret" not in json.dumps(store.conversation_summaries())
    store.close()
    reopened = Store(tmp_path)
    assert reopened.remote_state(account["id"], state["conversation_id"]) == state
    assert reopened.conversation_summaries()[0]["parent_message_id"] == 0
    reopened.close()
