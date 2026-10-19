import json
import httpx
import pytest
from proxy.chatgpt_models import model_catalog
from proxy.providers import Providers, ProviderError


def fixture_catalog():
    return {
        "default_model_slug": "gpt-current",
        "models": [
            {
                "slug": "gpt-current",
                "title": "GPT Current",
                "reasoning_type": "auto",
                "is_work_mode_model": False,
            },
            {
                "slug": "gpt-current-instant",
                "title": "GPT Current",
                "reasoning_type": "none",
                "is_work_mode_model": False,
            },
            {
                "slug": "gpt-current-thinking",
                "title": "GPT Current",
                "reasoning_type": "reasoning",
                "is_work_mode_model": False,
                "thinking_efforts": [
                    {"thinking_effort": "standard"},
                    {"thinking_effort": "extended"},
                ],
            },
            {
                "slug": "gpt-current-wm",
                "title": "GPT Current",
                "reasoning_type": "reasoning",
                "is_work_mode_model": True,
            },
            {"slug": "research", "title": "Research", "is_work_mode_model": False},
        ],
    }


def test_real_catalog_flags_are_preserved_and_auto_is_distinct():
    models, current = model_catalog(fixture_catalog())
    assert (
        current == "gpt-current"
        and models[0]["id"] == "auto"
        and models[0]["upstream_id"] == "auto"
    )
    assert len({m["name"] for m in models[:5]}) == 5
    by_id = {m["id"]: m for m in models}
    assert by_id["gpt-current-wm"]["mode"] == "work"
    assert by_id["gpt-current-thinking"]["mode"] == "chat"
    assert by_id["gpt-current-thinking"]["reasoning_efforts"] == [
        "standard",
        "extended",
    ]
    assert by_id["research"]["mode"] == "research"


@pytest.mark.asyncio
async def test_normal_auto_sends_native_auto_and_rejects_work_model_mismatch():
    models, _ = model_catalog(fixture_catalog())
    sent = []

    def handle(req):
        if "/accounts/check/" in req.url.path:
            return httpx.Response(200, json={"default_account_id": "test"})
        sent.append(json.loads(req.content))
        return httpx.Response(
            200,
            text="data: "
            + json.dumps(
                {
                    "conversation_id": "remote",
                    "message": {
                        "id": "answer",
                        "author": {"role": "assistant"},
                        "channel": "final",
                        "status": "finished_successfully",
                        "content": {"parts": ["Ready"]},
                    },
                }
            )
            + "\n\ndata: [DONE]\n\n",
        )

    p = Providers(httpx.MockTransport(handle))
    a = {
        "id": "test",
        "provider": "chatgpt",
        "credential": "fake-test-token",
        "catalog": {"models": models},
    }
    state = {}
    assert (
        await p.send(a, "Hello", state, {"model": "auto", "chatgpt_mode": "chat"})
        == "Ready"
    )
    assert sent[-1]["model"] == "auto" and "conversation_mode" not in sent[-1]
    with pytest.raises(ProviderError, match="لا يطابق"):
        await p.send(
            a, "Hello", {}, {"model": "gpt-current-wm", "chatgpt_mode": "chat"}
        )
    assert len(sent) == 1
    await p.send(a, "Hello", {}, {"model": "gpt-current-wm", "chatgpt_mode": "work"})
    assert sent[-1]["model"] == "gpt-current-wm"


def test_mode_changes_do_not_reuse_chat_context_and_wrong_selections_fail(env):
    c, app, p, account, h = env
    headers = {**h, "X-Conversation-ID": "same-client-chat"}
    body = {"model": "gpt", "messages": [{"role": "user", "content": "Hello"}]}
    first = c.post("/v1/chat/completions", headers=headers, json=body).json()
    path = "/admin/api/accounts/" + account["id"]
    wrong = c.patch(
        path, json={"settings": {"model": "test-work", "chatgpt_mode": "chat"}}
    )
    assert wrong.status_code == 400
    assert (
        c.patch(
            path, json={"settings": {"model": "test-work", "chatgpt_mode": "work"}}
        ).status_code
        == 200
    )
    second = c.post("/v1/chat/completions", headers=headers, json=body).json()
    assert second["conversation_id"] != first["conversation_id"]
    assert p.sent[-1]["settings"]["chatgpt_mode"] == "work" and not p.sent[-1][
        "state"
    ].get("conversation_id")


def test_pwa_manifest_worker_and_icons_are_public_but_accounts_stay_protected(env):
    c, app, p, account, h = env
    manifest = c.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    value = manifest.json()
    assert value["display"] == "standalone" and value["start_url"] == "/admin"
    for icon in value["icons"]:
        r = c.get(icon["src"])
        assert r.status_code == 200 and r.content.startswith(b"\x89PNG")
    worker = c.get("/sw.js")
    assert worker.status_code == 200 and worker.headers["service-worker-allowed"] == "/"
    assert c.get("/offline").status_code == 200
    c.post("/admin/api/logout")
    assert c.get("/admin/api/state").status_code == 401


def test_empty_model_response_does_not_invent_a_working_auto_model():
    assert model_catalog({"models": []}) == ([], None)


def test_create_remote_chat_honors_unsaved_mode_selection(env):
    c, app, p, account, h = env
    result = c.post(
        "/admin/api/accounts/" + account["id"] + "/new-chat",
        json={"model": "test-work", "chatgpt_mode": "work"},
    )
    assert result.status_code == 200
    assert (
        p.sent[-1]["settings"]["model"] == "test-work"
        and p.sent[-1]["settings"]["chatgpt_mode"] == "work"
    )
    assert not p.sent[-1]["state"]
