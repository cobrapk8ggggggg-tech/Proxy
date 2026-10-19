"""Isolated browser fixture: real FastAPI/SQLite, simulated upstream accounts only."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["ADMIN_PASSWORD"] = "browser-test-password"
from conftest import FakeProviders
from proxy.app import create_app
import uvicorn

app = create_app(tempfile.mkdtemp(prefix="aurum-browser-"), FakeProviders())


@app.post("/__reset")
async def reset():
    # This endpoint exists ONLY in this test server, never production.
    store = app.state.store
    for table in (
        "accounts",
        "routes",
        "clients",
        "conversations",
        "events",
        "responses",
        "files",
        "idempotency",
    ):
        store.run("DELETE FROM " + table)
    for provider, label in [
        ("chatgpt", "حساب ChatGPT الأساسي"),
        ("gemini", "حساب Gemini الشخصي"),
        ("deepseek", "DeepSeek Telegram"),
    ]:
        value = store.add_account(
            provider, "fake-browser-credential-" + provider, label
        )
        catalog = await app.state.providers.discover(value)
        store.update_account(value["id"], catalog=catalog, status="connected")
        store.set_route(
            {"chatgpt": "gpt", "gemini": "gemini", "deepseek": "deepseek-telegram"}[
                provider
            ],
            value["id"],
            {"model": "auto"},
        )
    return {"ok": True}


if __name__ == "__main__":
    import asyncio

    asyncio.run(reset())
    uvicorn.run(app, host="127.0.0.1", port=4071, log_level="warning")
