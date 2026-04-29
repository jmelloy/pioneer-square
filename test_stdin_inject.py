#!/usr/bin/env python3
"""
Test whether stdin injection mid-tool-run is picked up between turns.

Claude is given a task that runs two slow bash commands sequentially.
We inject a message during the first sleep and observe when it appears.
"""

import asyncio
import json
import sys
import time

CLAUDE = "/opt/node22/bin/claude"

PROMPT = (
    "Run these two bash commands in sequence: "
    "first `sleep 4 && echo FIRST_DONE`, "
    "then `sleep 4 && echo SECOND_DONE`. "
    "After both finish, report what you observed."
)


async def main():
    cmd = [
        CLAUDE,
        "--output-format", "stream-json",
        "--verbose",
        "--max-turns", "10",
        "--dangerously-skip-permissions",
        "-p", PROMPT,
    ]

    print(f"[test] Starting claude at t=0", flush=True)
    t0 = time.monotonic()

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    inject_done = False

    async def inject_after_delay():
        nonlocal inject_done
        await asyncio.sleep(2.0)  # inject during the first sleep
        msg = "INJECT_TEST: please note this message was injected mid-run"
        elapsed = time.monotonic() - t0
        print(f"\n[test] >>> INJECTING at t={elapsed:.1f}s: {msg!r}\n", flush=True)
        proc.stdin.write((msg + "\n").encode())
        await proc.stdin.drain()
        inject_done = True

    async def drain_stderr():
        async for raw in proc.stderr:
            line = raw.decode(errors="replace").strip()
            if line:
                elapsed = time.monotonic() - t0
                print(f"[t={elapsed:.1f}s] STDERR: {line[:120]}", flush=True)

    async def read_stdout():
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            elapsed = time.monotonic() - t0
            try:
                event = json.loads(line)
                etype = event.get("type")
                if etype == "assistant":
                    for blk in event.get("message", {}).get("content", []):
                        if blk.get("type") == "text" and blk.get("text", "").strip():
                            print(f"[t={elapsed:.1f}s] assistant text: {blk['text'][:120]}", flush=True)
                        elif blk.get("type") == "tool_use":
                            inp = blk.get("input", {})
                            cmd_str = inp.get("command", json.dumps(inp)[:80])
                            print(f"[t={elapsed:.1f}s] tool_use {blk.get('name')}: {cmd_str[:80]}", flush=True)
                elif etype == "user":
                    for blk in event.get("message", {}).get("content", []):
                        if blk.get("type") == "tool_result":
                            content = blk.get("content", "")
                            if isinstance(content, list):
                                content = " ".join(b.get("text","") for b in content)
                            print(f"[t={elapsed:.1f}s] tool_result: {str(content)[:120]}", flush=True)
                        elif blk.get("type") == "text":
                            txt = blk.get("text","").strip()
                            if txt:
                                print(f"[t={elapsed:.1f}s] user text (injected?): {txt[:120]}", flush=True)
                elif etype == "result":
                    print(f"[t={elapsed:.1f}s] RESULT: {event.get('subtype')} turns={event.get('num_turns')}", flush=True)
                elif etype == "system" and event.get("subtype") == "init":
                    print(f"[t={elapsed:.1f}s] system init", flush=True)
            except json.JSONDecodeError:
                print(f"[t={elapsed:.1f}s] non-json: {line[:80]}", flush=True)

    await asyncio.gather(
        inject_after_delay(),
        drain_stderr(),
        read_stdout(),
    )

    rc = await proc.wait()
    print(f"\n[test] Process exited rc={rc} inject_done={inject_done}", flush=True)


asyncio.run(main())
