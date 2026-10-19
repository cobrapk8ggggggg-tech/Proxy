import pytest
from fastapi.testclient import TestClient
from proxy.app import create_app


class FakeProviders:
    def __init__(self):
        self.sent = []
        self.gemini_sessions = {}
        self.failure = None

    async def discover(self, a):
        return {
            "models": [
                {"id": "auto", "name": "Auto"},
                {
                    "id": "test-pro",
                    "name": "Test Pro",
                    "reasoning_efforts": ["low", "high"],
                },
            ],
            "projects": (
                [{"id": "novel-project", "name": "Novel project"}]
                if a["provider"] == "chatgpt"
                else []
            ),
            "chats": [{"id": "general", "title": "General chat"}],
            "capabilities": {
                "projects": a["provider"] == "chatgpt",
                "attachments": a["provider"] != "deepseek",
            },
            "warnings": [],
        }

    async def projects(self, a, cursor=None):
        return {"items": [{"id": "novel-project", "name": "Novel project"}]}

    async def chats(self, a, project="", cursor=None, offset=0, limit=50):
        return {
            "items": [
                {
                    "id": "project-chat" if project else "general",
                    "title": "Project translation" if project else "General chat",
                }
            ]
        }

    async def conversation(self, a, cid):
        return {"conversation_id": cid, "parent_message_id": "latest-parent"}

    async def send(self, a, prompt, state, settings, attachments=None):
        import uuid

        if self.failure:
            raise self.failure
        self.sent.append(
            {
                "account": a["id"],
                "prompt": prompt,
                "state": dict(state),
                "settings": dict(settings),
                "attachments": attachments,
            }
        )
        state["conversation_id"] = (
            state.get("conversation_id") or "remote-" + uuid.uuid4().hex
        )
        state["parent_message_id"] = "answer-node"
        return "وصل الرد من الحساب بنجاح."

    async def upload(self, a, name, mime, data):
        return {
            "attachment": {
                "id": "upstream-file",
                "name": name,
                "mime_type": mime,
                "size": len(data),
            }
        }


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "test-admin-password-local")
    upstream = FakeProviders()
    app = create_app(tmp_path, upstream)
    c = TestClient(app, raise_server_exceptions=False)
    assert (
        c.post(
            "/admin/api/login", json={"password": "test-admin-password-local"}
        ).status_code
        == 200
    )
    account = c.post(
        "/admin/api/accounts",
        json={
            "provider": "chatgpt",
            "credential": "fake-upstream-token",
            "label": "GPT Test Account",
        },
    ).json()
    assert (
        c.post("/admin/api/accounts/" + account["id"] + "/connect").status_code == 200
    )
    key = c.post("/admin/api/keys", json={"name": "OpenMinis", "models": ["*"]}).json()[
        "key"
    ]
    yield c, app, upstream, account, {"Authorization": "Bearer " + key}
    c.close()
    app.state.store.close()
