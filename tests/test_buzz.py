"""Tests for buzz.py – no real key presses (a recorder stands in for the keyboard).

Run:  python tests/test_buzz.py
"""
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import buzz  # noqa: E402
from aiohttp import ClientSession, WSMsgType  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

WINDOWS = sys.platform == "win32"
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS  " if cond else "FAIL  ") + name + (f"   -> {detail}" if not cond and detail != "" else ""), flush=True)


def skip(name, reason):
    print(f"SKIP  {name} ({reason})", flush=True)


class Recorder:
    unknown = []

    def __init__(self):
        self.events = []

    def press(self, char):
        self.events.append(("press", char))

    def release(self, char):
        self.events.append(("release", char))


async def sync(ws):
    """Ping/pong as a barrier: afterwards all messages sent before have been handled."""
    await ws.send_json({"type": "ping"})
    while True:
        msg = await ws.receive(timeout=5)
        if msg.type == WSMsgType.TEXT and json.loads(msg.data) == {"type": "pong"}:
            return


async def wait_for(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end and not pred():
        await asyncio.sleep(0.02)
    return pred()


def msg(player, button, state):
    return {"player": player, "button": button, "state": state}


async def server_tests():
    print("\n== Server logic (aiohttp TestServer, recorder keyboard) ==")
    rec = Recorder()
    server = buzz.BuzzServer(rec)
    async with TestClient(TestServer(server.make_app())) as client:
        r = await client.get("/")
        html = await r.text()
        check("GET / serves the phone page", r.status == 200 and r.content_type == "text/html"
              and 'data-btn="red"' in html and "touchstart" in html)
        r = await client.get("/manifest.webmanifest")
        check("manifest is served", r.status == 200 and json.loads(await r.text())["display"] == "standalone")
        r = await client.get("/icon.svg")
        check("icon is served", r.status == 200)

        a = await client.ws_connect("/ws")
        check("WebSocket connects", await wait_for(lambda: len(server.clients) == 1))
        await a.send_json({"type": "hello", "player": 1})
        await sync(a)
        check("hello sets player 1", next(iter(server.clients)).player == 1)

        await a.send_json(msg(1, "red", "down"))
        await sync(a)
        check("down → key '1' pressed", rec.events == [("press", "1")], rec.events)
        await a.send_json(msg(1, "red", "down"))
        await sync(a)
        check("repeated down → no second press", rec.events == [("press", "1")], rec.events)
        await asyncio.sleep(0.3)
        check("key stays held until 'up' (no tap)", rec.events == [("press", "1")], rec.events)
        await a.send_json(msg(1, "red", "up"))
        await sync(a)
        check("up → key released", rec.events == [("press", "1"), ("release", "1")], rec.events)
        await a.send_json(msg(1, "red", "up"))
        await sync(a)
        check("repeated up → ignored", len(rec.events) == 2, rec.events)

        rec.events.clear()
        expected = []
        for p in (1, 2, 3, 4):
            for b in buzz.BUTTONS:
                await a.send_json(msg(p, b, "down"))
                await a.send_json(msg(p, b, "up"))
                expected += [("press", buzz.KEYMAP[p][b]), ("release", buzz.KEYMAP[p][b])]
        await sync(a)
        check("all 20 buttons → the right key (per KEYMAP)", rec.events == expected, rec.events)
        await a.send_json({"type": "hello", "player": 1})
        await sync(a)

        rec.events.clear()
        b = await client.ws_connect("/ws")
        await b.send_json({"type": "hello", "player": 1})
        await sync(b)
        await a.send_json(msg(1, "blue", "down"))
        await sync(a)
        await b.send_json(msg(1, "blue", "down"))
        await sync(b)
        check("2 phones, same player: key pressed only once", rec.events == [("press", "q")], rec.events)
        await a.send_json(msg(1, "blue", "up"))
        await sync(a)
        check("first lets go, second still holds → key stays down", rec.events == [("press", "q")], rec.events)
        await b.send_json(msg(1, "blue", "up"))
        await sync(b)
        check("both let go → key released", rec.events == [("press", "q"), ("release", "q")], rec.events)
        check("terminal summary lists both", server.summary() == "Connected: Player 1 (2 phones)", server.summary())

        rec.events.clear()
        await b.send_json(msg(1, "green", "down"))
        await b.send_json(msg(1, "yellow", "down"))
        await sync(b)
        await b.close()
        await wait_for(lambda: len(server.clients) == 1)
        check("connection lost → all keys of that phone released",
              sorted(rec.events) == sorted([("press", "y"), ("press", "x"), ("release", "y"), ("release", "x")]), rec.events)

        rec.events.clear()
        await a.send_json(msg(1, "orange", "down"))
        await a.send_json({"type": "hello", "player": None})
        await sync(a)
        check("switching player → held key released", rec.events == [("press", "a"), ("release", "a")], rec.events)
        await a.send_json({"type": "hello", "player": 1})

        rec.events.clear()
        bad = ["not json", "[1, 2]", "null", "42",
               json.dumps(msg(True, "red", "down")), json.dumps(msg(5, "red", "down")),
               json.dumps(msg("1", "red", "down")), json.dumps(msg(1, "purple", "down")),
               json.dumps(msg(1, ["red"], "down")), json.dumps(msg(1, "red", "hold")),
               json.dumps({"type": "nonsense"}), json.dumps({"type": "hello", "player": 9})]
        for raw in bad:
            await a.send_str(raw)
        await sync(a)
        check("12 invalid messages → no key, connection stays open", rec.events == [] and not a.closed, rec.events)
        check("invalid hello doesn't change the player", next(iter(server.clients)).player == 1)

        ws_headers = {"Connection": "Upgrade", "Upgrade": "websocket", "Sec-WebSocket-Version": "13",
                      "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="}
        r = await client.get("/ws", headers={**ws_headers, "Origin": "http://evil.example"})
        check("other web page (Origin) → 403", r.status == 403, r.status)
        c = await client.ws_connect("/ws", origin=f"http://{client.host}:{client.port}")
        check("own page (same origin) → allowed", not c.closed)
        await c.close()

        # Wi-Fi loss: the phone stops answering pings → heartbeat disconnects and releases
        rec.events.clear()
        d = await client.ws_connect("/ws", autoping=False)
        await d.send_json(msg(2, "red", "down"))
        await wait_for(lambda: rec.events == [("press", "2")])
        t0 = time.monotonic()
        released = await wait_for(lambda: ("release", "2") in rec.events, timeout=10)
        check(f"silent phone → key released by heartbeat after {time.monotonic() - t0:.1f}s", released, rec.events)

        # Server stops while a key is held (fresh connection – 'a' didn't read during the
        # heartbeat wait and was dropped too; real browsers answer pings automatically)
        rec.events.clear()
        e = await client.ws_connect("/ws")
        await e.send_json(msg(1, "red", "down"))
        await sync(e)
    check("server stops → held key released", rec.events == [("press", "1"), ("release", "1")], rec.events)
    check("afterwards no key is counted as held", not server.keys.count, server.keys.count)


def layout_tests():
    print("\n== Keyboard layout (only reads the layout, sends nothing) ==")
    chars = {c for keys in buzz.KEYMAP.values() for c in keys.values()}
    if sys.platform == "darwin":
        codes = buzz.mac_keycodes()
        if not codes:
            skip("macOS layout lookup", "no keyboard layout available")
            return
        for ch, vk in {"1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15}.items():
            check(f"'{ch}' → number row key {vk} (not the keypad)", codes.get(ch) == vk, codes.get(ch))
        check("'y' and ',' exist on this layout", "y" in codes and "," in codes)
        kb = buzz.SystemKeyboard(chars)
        check("SystemKeyboard finds all 20 characters", kb.unknown == [], kb.unknown)
        check("SystemKeyboard sends the number row for '1' (vk 18)", kb._keys["1"].vk == 18, kb._keys["1"])
    elif WINDOWS:
        import ctypes
        scan = {ch: ctypes.windll.user32.VkKeyScanW(ord(ch)) for ch in chars}
        check("every character has a key without modifiers (no Unicode fallback)",
              all(v != -1 and (v >> 8) & 0xFF == 0 for v in scan.values()), scan)
        check("'1'–'4' are the number row keys (VK 0x31–0x34)",
              [scan[c] & 0xFF for c in "1234"] == [0x31, 0x32, 0x33, 0x34], scan)
        buzz.SystemKeyboard(chars)
        check("SystemKeyboard can be created", True)
    else:
        skip("layout tests", "only macOS and Windows")


async def e2e_tests():
    print("\n== End to end: buzz.py --dry-run as its own process ==")
    port = 8765
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    extra = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS else {}
    run = dict(cwd=ROOT, env=env, text=True, encoding="utf-8", errors="replace")

    version = subprocess.run([sys.executable, "buzz.py", "--version"], capture_output=True, timeout=30, **run)
    check("--version prints the version", version.returncode == 0 and buzz.__version__ in version.stdout, version)

    proc = subprocess.Popen([sys.executable, "buzz.py", "--dry-run", "--port", str(port)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1, **run, **extra)
    lines = []

    async def read_until(text, timeout=15):
        end = time.monotonic() + timeout
        loop = asyncio.get_running_loop()
        while time.monotonic() < end:
            if any(text in line for line in lines):
                return True
            try:
                line = await asyncio.wait_for(loop.run_in_executor(None, proc.stdout.readline),
                                              timeout=end - time.monotonic())
            except asyncio.TimeoutError:
                break
            if not line:
                break
            lines.append(line.rstrip("\n"))
        return any(text in line for line in lines)

    try:
        check("server starts and shows the banner", await read_until("Ctrl+C"), lines[-5:])
        check("banner shows the phone URL with port", any(f":{port}" in l and "http://" in l for l in lines))
        check("banner shows the test mode note", any("TEST MODE" in l for l in lines))

        second = subprocess.run([sys.executable, "buzz.py", "--dry-run", "--port", str(port)],
                                capture_output=True, timeout=30, **run)
        check("port in use → exit code 1 with a hint", second.returncode == 1 and "already in use" in second.stderr,
              (second.returncode, second.stderr[-300:]))

        async with ClientSession() as session:
            r = await session.get(f"http://127.0.0.1:{port}/")
            check("HTTP: page is served", r.status == 200)
            ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws",
                                          headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)"})
            await ws.send_json({"type": "hello", "player": 3})
            await sync(ws)
            check("terminal logs the connected player", await read_until("is Player 3"), lines[-3:])
            check("terminal shows the device (iPhone)", any("iPhone" in l for l in lines))
            await ws.send_json({"player": 3, "button": "green", "state": "down"})
            await sync(ws)
            check("terminal logs the button press", await read_until("key b"), lines[-3:])
            ws2 = await session.ws_connect(f"http://127.0.0.1:{port}/ws")
            await ws2.send_json({"player": 4, "button": "yellow", "state": "down"})
            await sync(ws2)
            await ws.close()
            check("disconnect while holding → 'released' in the log", await read_until("released 1 held key(s)"), lines[-3:])

            # Ctrl+C (Ctrl+Break on Windows) while player 4 still holds yellow
            proc.send_signal(signal.CTRL_BREAK_EVENT if WINDOWS else signal.SIGINT)
            check("Ctrl+C → 'all keys released'", await read_until("all keys released"), lines[-4:])
            m = await ws2.receive(timeout=10)
            check("phone gets a clean close", m.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING),
                  (m.type, ws2.close_code))
        code = await asyncio.get_running_loop().run_in_executor(None, proc.wait, 15)
        check("process exits cleanly (exit code 0)", code == 0, code)
        lines.extend(proc.stdout.read().splitlines())
        check("no tracebacks in the output", not any("Traceback" in l for l in lines))
    finally:
        if proc.poll() is None:
            proc.kill()
    print("\n--- server output ---")
    print("\n".join(lines))


async def main():
    await server_tests()
    layout_tests()
    await e2e_tests()
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} tests passed")
    if failed:
        print("Failed:", *failed, sep="\n  ")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
