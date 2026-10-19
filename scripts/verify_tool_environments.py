"""Opt-in real ChatGPT check; never executes model-generated shell commands.

The one client-side action is reading a random local fixture with Python.
The native-compute check also requests an independent calculation in ChatGPT.
"""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy.engine import Engine
from proxy.providers import Providers, ProviderError
from proxy.store import Store

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "shell_execute",
            "description": "Execute a command in an isolated Linux process in the client application (OpenMinis iSH/Alpine Linux), separate from ChatGPT's native environment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_title": {"type": "string"},
                    "command": {"type": "string"},
                    "timeout": {"type": "integer"},
                    "delay": {"type": "integer"},
                },
                "required": ["tool_title", "command"],
            },
        },
    }
]


async def verify(args):
    credential = os.getenv("CHATGPT_ACCESS_TOKEN", "").strip()
    if not credential:
        raise ValueError("Set CHATGPT_ACCESS_TOKEN privately before running.")
    report = {"policy": args.policy, "real_account": True, "iphone_app_run": False}
    with tempfile.TemporaryDirectory(prefix="aurum-tool-check-") as directory:
        fixture = Path(directory) / "application-only-proof.txt"
        nonce = secrets.token_hex(24)
        fixture.write_text(nonce)
        store = Store(Path(directory) / "vault")
        try:
            account = store.add_account("chatgpt", credential)
            store.update_account(
                account["id"],
                settings={**account["settings"], "tool_environment": args.policy},
            )
            store.set_route(
                "test", account["id"], {"model": "auto", "chatgpt_mode": "chat"}
            )
            engine = Engine(store, Providers())
            client = {"id": "private-live-tool-check", "models": ["*"]}
            request = f"Read {fixture} in the CLIENT APPLICATION using shell_execute with exactly cat and the file path as arguments. Do not create the file or guess its contents. Return its actual text after receiving the tool result."
            expected = None
            if args.native_compute:
                seed = "aurum-native-" + secrets.token_hex(8)
                digest = seed.encode()
                for _ in range(100003):
                    digest = hashlib.sha256(digest).digest()
                expected = digest.hex()
                request = (
                    f"First, in ChatGPT's INTERNAL Python only, compute SHA256 iteratively 100003 times starting with the UTF8 bytes of {seed}; each iteration hashes the previous raw bytes. Keep the final hex digest. Do not perform this calculation in the client application. Then: "
                    + request
                    + " In your final response include both the native digest and the application file contents, identifying each environment."
                )
            messages = [
                {
                    "role": "system",
                    "content": "You are an autonomous assistant in OpenMinis. You have a Linux environment with shell and filesystem. Execute directly using shell_execute for application shell commands.",
                },
                {"role": "user", "content": request},
            ]
            body = {
                "model": "test",
                "messages": messages,
                "tools": TOOLS,
                "tool_choice": "auto",
            }
            first = await engine.complete(body, client)
            message = first["choices"][0]["message"]
            calls = message.get("tool_calls", [])
            report.update(
                external_call_received=bool(calls),
                native_tools_observed=first["execution"]["native_tools_observed"],
            )
            if len(calls) != 1 or calls[0]["function"]["name"] != "shell_execute":
                report["status"] = "no_expected_external_call"
                return report
            arguments = json.loads(calls[0]["function"]["arguments"])
            if shlex.split(arguments["command"]) != ["cat", str(fixture)]:
                report["status"] = "unexpected_command_not_executed"
                return report
            # Validate the model's request, then read ONLY our known fixture.
            # Never pass generated command text to a shell or subprocess.
            output = json.dumps(
                {"stdout": fixture.read_text(), "stderr": "", "exit_code": 0}
            )
            followup = {
                **body,
                "messages": messages
                + [
                    message,
                    {"role": "tool", "tool_call_id": calls[0]["id"], "content": output},
                ],
            }
            final = await engine.complete(followup, client)
            text = final["choices"][0]["message"].get("content") or ""
            report["real_file_output_returned"] = nonce in text
            if expected:
                report["native_tools_observed"] |= final["execution"][
                    "native_tools_observed"
                ]
                report["native_digest_correct"] = expected in text
            passed = nonce in text and (
                expected is None
                or (expected in text and report["native_tools_observed"])
            )
            report["status"] = "passed" if passed else "failed"
            return report
        finally:
            store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--send-test",
        action="store_true",
        help="Create a real conversation and send two requests",
    )
    parser.add_argument(
        "--policy", choices=("application", "hybrid"), default="application"
    )
    parser.add_argument(
        "--native-compute",
        action="store_true",
        help="Also verify native Python calculation; requires hybrid",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.send_test or (args.native_compute and args.policy != "hybrid"):
        parser.error("Use --send-test; --native-compute requires --policy hybrid")
    try:
        report = asyncio.run(verify(args))
    except (ProviderError, ValueError) as exc:
        report = {"status": "failed", "error": str(exc)}
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    print(text, end="")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
