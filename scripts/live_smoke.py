"""Explicit live account checks. Credentials/titles/IDs/replies are never printed."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy.providers import Providers, ProviderError


def read_cookie_file(path):
    raw = Path(path).read_text().strip()
    match = re.search(r'^export BROWSER_COOKIE_HEADER=([\'"])(.*?)\1\s*$', raw, re.M)
    if match:
        return match[2]
    if re.search(r"^export ", raw, re.M):
        raise ValueError(
            "Unsupported cookie file; it must contain BROWSER_COOKIE_HEADER"
        )
    return raw


async def main(args):
    credential = os.getenv(
        {
            "chatgpt": "CHATGPT_ACCESS_TOKEN",
            "gemini": "GEMINI_COOKIE_HEADER",
            "deepseek": "DEEPSEEK_ACCESS_TOKEN",
            "qwen": "QWEN_ACCESS_TOKEN",
        }[args.provider]
    )
    if (
        args.provider == "gemini"
        and not credential
        and os.getenv("GOOGLE_COOKIES_FILE")
    ):
        credential = read_cookie_file(os.environ["GOOGLE_COOKIES_FILE"])
    if not credential:
        raise SystemExit(
            "Set the provider credential environment variable; never commit credentials."
        )
    provider = Providers()
    account = {
        "id": "explicit-live-test",
        "provider": args.provider,
        "credential": credential.strip(),
    }
    report = {
        "date": datetime.now(timezone.utc).isoformat(),
        "provider": args.provider,
        "mode": (
            "read-only-catalog" if not args.send_test else "catalog-and-new-test-chat"
        ),
        "credentials_included": False,
    }
    try:
        catalog = await provider.discover(account)
        report.update(
            models=len(catalog["models"]),
            projects=len(catalog.get("projects", [])),
            chats=len(catalog.get("chats", [])),
        )
        if args.send_test:
            state = {}
            settings = {
                "model": catalog["models"][0]["id"],
                "thinking_enabled": False,
                "search_enabled": False,
            }
            text = await provider.send(
                account,
                "اختبار تقني لربط حسابي بالبروكسي. أجب بكلمة واحدة: جاهز.",
                state,
                settings,
            )
            first_id = state.get("conversation_id")
            report["reply_received"] = bool(text.strip())
            followup = await provider.send(
                account,
                "اختبار متابعة في المحادثة نفسها. أجب بكلمة واحدة: متصل.",
                state,
                settings,
            )
            report["followup_received"] = bool(followup.strip())
            report["same_upstream_conversation"] = bool(
                first_id and state.get("conversation_id") == first_id
            )
        report["status"] = "passed"
    except Exception as error:
        report["status"] = "failed"
        report["error"] = (
            error.message
            if isinstance(error, ProviderError)
            else "Provider protocol check failed."
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--provider", choices=["chatgpt", "gemini", "deepseek", "qwen"], required=True
    )
    parser.add_argument(
        "--send-test",
        action="store_true",
        help="Creates a real remote test conversation and sends two short messages.",
    )
    parser.add_argument("--output")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
