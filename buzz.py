#!/usr/bin/env python3
"""Phone buzzers for Buzz! on PCSX2: phones on your home Wi-Fi become Buzz! controllers.

Start:  python3 buzz.py           (options: --port 8001, --dry-run, --help)

The phones open the address shown in the terminal, pick player 1–4 and press the buttons.
The server holds real keys down for as long as a button is held ("down" → key down,
"up" → key up). In PCSX2 (USB Port 1 → "Buzz Controller") you bind the buttons to these keys.

https://github.com/sgassner/pcsx2-phone-buzzers
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import errno
import itertools
import json
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections import Counter
from urllib.parse import urlparse

__version__ = "1.0.0"
INSTALL_HINT = "py -m pip install -r requirements.txt" if sys.platform == "win32" else "pip3 install -r requirements.txt"

try:
    from aiohttp import WSCloseCode, WSMsgType, web
except ImportError:
    sys.exit(f"aiohttp is missing – install the requirements first:  {INSTALL_HINT}")


# ═══════════════════════════════════════════════════════════════════════════
#  Key mapping – change it here
#  Player → button → key, as printed on your keyboard.
#  Special keys work by name too, e.g. "space", "enter", "up", "f1".
#  The exact keys don't matter much: in PCSX2 you bind each button by pressing
#  it on the phone, and PCSX2 records whatever key arrives.
# ═══════════════════════════════════════════════════════════════════════════
KEYMAP = {
    1: {"red": "1", "blue": "q", "orange": "a", "green": "y", "yellow": "x"},
    2: {"red": "2", "blue": "w", "orange": "s", "green": "c", "yellow": "v"},
    3: {"red": "3", "blue": "e", "orange": "d", "green": "b", "yellow": "n"},
    4: {"red": "4", "blue": "r", "orange": "f", "green": "m", "yellow": ","},
}

HOST = "0.0.0.0"
PORT = 8000
LOG_BUTTONS = True   # show every button press in the terminal
HEARTBEAT = 3.0      # seconds – a phone that drops off the network is noticed after ~5 s

BUTTONS = ("red", "blue", "orange", "green", "yellow")
BUTTON_NAMES = {"red": "Red", "blue": "Blue", "orange": "Orange", "green": "Green", "yellow": "Yellow"}


# ─── Terminal output ───────────────────────────────────────────────────────

TTY = sys.stdout.isatty()
BUTTON_STYLE = {"red": "1;31", "blue": "1;34", "orange": "1;38;5;208", "green": "1;32", "yellow": "1;38;5;220"}


def style(text, code):
    return f"\033[{code}m{text}\033[0m" if TTY else text


def log(message):
    line = f"{style(time.strftime('%H:%M:%S'), '2')}  {message}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:  # output that can't show ● ▼ … (e.g. redirected on Windows, cp1252)
        encoding = sys.stdout.encoding or "ascii"
        print(line.encode(encoding, "replace").decode(encoding), flush=True)


def enable_windows_colors():
    """Turn on color codes in the classic Windows console (Windows Terminal has them on already)."""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        return bool(kernel32.GetConsoleMode(handle, ctypes.byref(mode))
                    and kernel32.SetConsoleMode(handle, mode.value | 0x0004))  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        return False


# ─── Key presses ───────────────────────────────────────────────────────────

def mac_keycodes():
    """Character → macOS key code on the keyboard layout that is currently selected.

    pynput does this itself, but for characters that exist on several keys it takes
    the last match – "1" would then come from the numeric keypad instead of the
    number row, and PCSX2 would see a different key ("Keypad 1"). We take the first
    match (main key block). Because the real layout is read, QWERTZ/AZERTY work too:
    the key that gets pressed is exactly the one labelled "y" on your keyboard.
    """
    try:
        from pynput._util.darwin import keycode_context, keycode_to_string

        codes = {}
        with keycode_context() as context:
            for code in range(128):
                codes.setdefault(keycode_to_string(context, code), code)
        return codes
    except Exception:  # internal pynput API – fall back to pynput's own lookup
        return {}


class SystemKeyboard:
    """Sends real key presses via pynput.

    On Windows pynput asks the current keyboard layout itself (VkKeyScan), which
    already picks the main key block, so no extra lookup is needed there.
    """

    def __init__(self, chars):
        from pynput.keyboard import Controller, Key, KeyCode

        self._controller = Controller()
        codes = mac_keycodes() if sys.platform == "darwin" else {}
        self._keys = {}
        self.unknown = []  # characters that have no key on the current layout
        for char in chars:
            if len(char) > 1 and char in Key.__members__:
                self._keys[char] = Key[char]
                continue
            vk = codes.get(char, codes.get(char.lower()))
            if vk is None:
                if codes:
                    self.unknown.append(char)
                self._keys[char] = KeyCode.from_char(char)
            else:
                self._keys[char] = KeyCode.from_vk(vk)

    def press(self, char):
        self._controller.press(self._keys[char])

    def release(self, char):
        self._controller.release(self._keys[char])


class DryRunKeyboard:
    """Presses nothing – for testing without real key presses (--dry-run)."""

    unknown = []

    def press(self, char):
        pass

    def release(self, char):
        pass


class HeldKeys:
    """Counts who is holding which key: if two phones hold the same key,
    it is only released once both have let go."""

    def __init__(self, keyboard):
        self.keyboard = keyboard
        self.count = Counter()

    def down(self, char):
        self.count[char] += 1
        if self.count[char] == 1:
            self._send(self.keyboard.press, char)

    def up(self, char):
        if self.count[char] <= 0:
            return
        self.count[char] -= 1
        if self.count[char] == 0:
            del self.count[char]
            self._send(self.keyboard.release, char)

    def release_all(self):
        for char in list(self.count):
            self._send(self.keyboard.release, char)
        self.count.clear()

    @staticmethod
    def _send(action, char):
        try:
            action(char)
        except Exception as exc:
            log(style(f"Could not send key {char!r}: {exc}", "1;31"))


def may_post_keys():
    """Does this program have the macOS "Accessibility" permission? None = unknown / not macOS."""
    if sys.platform != "darwin":
        return None
    results = []
    try:
        import Quartz
        results.append(bool(Quartz.CGPreflightPostEventAccess()))
    except Exception:
        pass
    try:
        import HIServices
        results.append(bool(HIServices.AXIsProcessTrusted()))
    except Exception:
        pass
    return any(results) if results else None


def request_key_permission():
    """Shows the macOS dialog that leads to the permission in System Settings."""
    try:
        import Quartz
        Quartz.CGRequestPostEventAccess()
    except Exception:
        pass


# ─── WebSocket server ──────────────────────────────────────────────────────

def device_name(user_agent):
    for marker, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                         ("Macintosh", "Mac"), ("Windows", "Windows"), ("Linux", "Linux")):
        if marker in user_agent:
            return name
    return "Device"


def valid_player(value):
    # type() instead of isinstance(): JSON true would otherwise count as player 1
    return value if type(value) is int and value in KEYMAP else None


class Client:
    _ids = itertools.count(1)

    def __init__(self, ws, request):
        self.id = next(Client._ids)
        self.ws = ws
        self.label = f"#{self.id} {device_name(request.headers.get('User-Agent', ''))} ({request.remote})"
        self.player = None
        self.pressed = set()  # (player, button) this phone is holding right now

    def __str__(self):
        return self.label


class BuzzServer:
    def __init__(self, keyboard):
        self.keys = HeldKeys(keyboard)
        self.clients = set()

    def make_app(self):
        app = web.Application()
        app.router.add_get("/", fixed(PAGE, "text/html"))
        app.router.add_get("/manifest.webmanifest", fixed(MANIFEST, "application/manifest+json"))
        app.router.add_get("/icon.svg", fixed(ICON, "image/svg+xml"))
        app.router.add_get("/ws", self.websocket)
        app.on_shutdown.append(self.close_all)
        app.on_cleanup.append(self.release_all)
        return app

    async def release_all(self, app=None):
        self.keys.release_all()

    async def websocket(self, request):
        # Other web pages (opened by someone on the same Wi-Fi) must not press keys.
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc != request.host:
            log(f"Rejected connection from another site: {origin}")
            raise web.HTTPForbidden(text="Foreign origin")

        ws = web.WebSocketResponse(heartbeat=HEARTBEAT, timeout=2.0, max_msg_size=4096)
        await ws.prepare(request)
        client = Client(ws, request)
        self.clients.add(client)
        log(f"{style('+', '1;32')} {client} connected")
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self.on_message(client, msg.data)
        finally:
            # Connection gone (also on Wi-Fi loss / heartbeat timeout): let go of everything
            self.clients.discard(client)
            released = self.release_client(client)
            note = f" – released {released} held key(s)" if released else ""
            log(f"{style('−', '1;31')} {client} disconnected{note}   │ {self.summary()}")
        return ws

    async def on_message(self, client, raw):
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            log(f"{client}: ignored unreadable message: {raw[:80]!r}")
            return
        kind = data.get("type", "button")
        if kind == "ping":
            await client.ws.send_str('{"type":"pong"}')
        elif kind == "hello":
            player = data.get("player")
            if player is None or valid_player(player):
                self.set_player(client, player)
        elif kind == "button":
            self.on_button(client, data)
        else:
            log(f"{client}: ignored unknown message: {data}")

    def on_button(self, client, data):
        player, button, state = valid_player(data.get("player")), data.get("button"), data.get("state")
        if player is None or button not in BUTTONS or state not in ("down", "up"):
            log(f"{client}: ignored invalid button message: {data}")
            return
        char = KEYMAP[player].get(button)
        if char is None:
            return
        if player != client.player:
            self.set_player(client, player)
        held = (player, button)
        if state == "down":
            if held in client.pressed:  # repeated "down" – key is already down
                return
            client.pressed.add(held)
            self.keys.down(char)
        else:
            if held not in client.pressed:
                return
            client.pressed.discard(held)
            self.keys.up(char)
        if LOG_BUTTONS:
            name = style(BUTTON_NAMES[button].ljust(6), BUTTON_STYLE[button])
            action = "▼ pressed " if state == "down" else "▲ released"
            log(f"  Player {player}  {name}  {action}  key {char}")

    def set_player(self, client, player):
        if player == client.player:
            return
        self.release_client(client)
        client.player = player
        what = f"is {style(f'Player {player}', '1')}" if player else "is choosing a player"
        log(f"{style('●', '1;33')} {client} {what}   │ {self.summary()}")

    def release_client(self, client):
        count = len(client.pressed)
        for player, button in client.pressed:
            self.keys.up(KEYMAP[player][button])
        client.pressed.clear()
        return count

    def summary(self):
        players = Counter(c.player for c in self.clients)
        parts = [f"Player {p}" + (f" ({n} phones)" if n > 1 else "")
                 for p, n in sorted((p, n) for p, n in players.items() if p)]
        if players[None]:
            parts.append(f"{players[None]} without a player")
        return "Connected: " + (", ".join(parts) if parts else "nobody")

    async def close_all(self, app):
        await asyncio.gather(*(c.ws.close(code=WSCloseCode.GOING_AWAY, message=b"Server stopped")
                               for c in list(self.clients)), return_exceptions=True)


def fixed(body, content_type):
    async def handler(request):
        return web.Response(text=body, content_type=content_type, headers={"Cache-Control": "no-store"})
    return handler


# ─── Address & start screen ────────────────────────────────────────────────

def lan_addresses():
    """IPv4 addresses of this computer on the home network, most likely first.
    On macOS, VPN addresses (utun…) are skipped when there is Wi-Fi/Ethernet."""
    primary = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # routing lookup only, nothing is sent
            primary = s.getsockname()[0]
    except OSError:
        pass
    found, iface, output = [], "", ""
    if sys.platform != "win32":
        try:
            output = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            pass
    for line in output.splitlines():
        match = re.match(r"(\S+): flags=", line)
        if match:
            iface = match.group(1)
            continue
        match = re.match(r"\s+inet (\d+\.\d+\.\d+\.\d+)", line)
        if match and iface.startswith(("en", "eth", "wlan")) and not match.group(1).startswith(("127.", "169.254.")):
            found.append(match.group(1))
    if primary in found:
        found.remove(primary)
        found.insert(0, primary)
    elif not found and primary and not primary.startswith("127."):
        found = [primary]
    return found or ["127.0.0.1"]


FONT = {  # 3×5 pixel digits for the big address
    "0": ("###", "# #", "# #", "# #", "###"), "1": (" # ", "## ", " # ", " # ", "###"),
    "2": ("###", "  #", "###", "#  ", "###"), "3": ("###", "  #", " ##", "  #", "###"),
    "4": ("# #", "# #", "###", "  #", "  #"), "5": ("###", "#  ", "###", "  #", "###"),
    "6": ("###", "#  ", "###", "# #", "###"), "7": ("###", "  #", "  #", "  #", "  #"),
    "8": ("###", "# #", "###", "# #", "###"), "9": ("###", "# #", "###", "  #", "###"),
    ".": (" ", " ", " ", " ", "#"), ":": (" ", "#", " ", "#", " "),
}


def big_text(text, width):
    """Text in block letters – twice as big if the terminal is wide enough."""
    if any(ch not in FONT for ch in text):
        return []
    rows = [" ".join(FONT[ch][r] for ch in text) for r in range(5)]
    if 2 * len(rows[0]) <= width:
        return [row.replace("#", "██").replace(" ", "  ") for row in rows]
    if len(rows[0]) <= width:  # two pixel rows per text line
        rows.append(" " * len(rows[0]))
        halves = {("#", "#"): "█", ("#", " "): "▀", (" ", "#"): "▄", (" ", " "): " "}
        return ["".join(halves[p] for p in zip(rows[i], rows[i + 1])) for i in range(0, 6, 2)]
    return []


def qr_lines(url):
    """QR code as text lines, black on white – whether the terminal is light or dark."""
    try:
        import qrcode
    except ImportError:
        return []
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    if len(matrix) % 2:
        matrix.append([False] * len(matrix[0]))
    # ▀ = upper half in foreground color, lower half in background color (30/40 black, 97/107 white)
    return ["".join(f"\033[{30 if top else 97};{40 if bottom else 107}m▀" for top, bottom in zip(a, b)) + "\033[0m"
            for a, b in zip(matrix[::2], matrix[1::2])]


def print_banner(urls, keyboard, dry_run):
    width = shutil.get_terminal_size((80, 24)).columns - 4
    url = urls[0]
    print()
    print("  " + style(" BUZZ! ", "1;97;41") + style("  Phone buzzers for PCSX2", "1") + style(f"  v{__version__}", "2"))
    print()
    print("  Open this on the phones (same Wi-Fi):")
    print()
    for line in big_text(urlparse(url).netloc, width):
        print("  " + style(line, "1"))
    print()
    print("  " + style(f"→  {url}", "1;4"))
    if len(urls) > 1:
        print(style("     if that doesn't work: " + "   ".join(urls[1:]), "2"))
    if TTY:
        lines = qr_lines(url)
        if lines:
            print()
            for line in lines:
                print("  " + line)
    print()
    print("  " + style("Key mapping   ", "2") + "".join(style(f"{BUTTON_NAMES[b]:>8}", BUTTON_STYLE[b]) for b in BUTTONS))
    for player, keys in KEYMAP.items():
        print(f"    Player {player}    " + "".join(f"{keys.get(b, '–'):>8}" for b in BUTTONS))
    print()
    if dry_run:
        print("  " + style("TEST MODE (--dry-run): no keys are pressed.", "1;33"))
    else:
        if keyboard.unknown:
            print("  " + style(f"!  These characters have no key on the current keyboard layout: "
                               f"{' '.join(keyboard.unknown)}", "1;33"))
        if may_post_keys() is False:
            print("  " + style("!  No permission to send key presses – PCSX2 won't receive anything!", "1;31"))
            print("     System Settings → Privacy & Security → Accessibility →")
            print("     turn on your terminal app (e.g. “Terminal”), then start buzz.py again.")
            request_key_permission()
    print("  Ctrl+C stops the server (all keys are released).")
    print(flush=True)


# ─── Start ─────────────────────────────────────────────────────────────────

def listen_for_stop(loop, stop):
    """Ctrl+C – and closing the terminal on macOS/Linux – stops the server gracefully."""
    if sys.platform == "win32":
        # asyncio can't add signal handlers on Windows, plain ones work (Ctrl+C, Ctrl+Break)
        for sig in (signal.SIGINT, signal.SIGBREAK):
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    else:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            loop.add_signal_handler(sig, stop.set)


def release_keys_on_console_close(keys):
    """Windows ends the process right away when the console window is closed –
    release the keys directly in the console handler before that happens."""
    import ctypes
    from ctypes import wintypes

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    def handler(event):
        if event in (2, 5, 6):  # CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT
            keys.release_all()
        return False  # let Python's own handler run as well

    ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True)
    return handler  # the caller must keep this reference alive


async def serve(server, port, dry_run):
    runner = web.AppRunner(server.make_app(), access_log=None, shutdown_timeout=3.0)
    await runner.setup()
    try:
        await web.TCPSite(runner, HOST, port).start()
    except OSError as exc:
        await runner.cleanup()
        if exc.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", errno.EADDRINUSE)):
            print(f"Port {port} is already in use (is buzz.py already running?). "
                  f"Pick another port:  python3 buzz.py --port {port + 1}", file=sys.stderr)
        else:
            print(f"Could not start the server: {exc}", file=sys.stderr)
        return 1

    stop = asyncio.Event()
    listen_for_stop(asyncio.get_running_loop(), stop)
    print_banner([f"http://{ip}:{port}" for ip in lan_addresses()], server.keys.keyboard, dry_run)
    try:
        await stop.wait()
    finally:
        print()
        server.keys.release_all()  # first – before anything else can go wrong
        log("Server stopping – all keys released.")
        await runner.cleanup()
    return 0


def main():
    global TTY
    parser = argparse.ArgumentParser(description="Phones on your home Wi-Fi as Buzz! buzzers for PCSX2.")
    parser.add_argument("--port", type=int, default=PORT, help=f"server port (default: {PORT})")
    parser.add_argument("--dry-run", action="store_true",
                        help="don't send real key presses, only show them in the terminal (for testing)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):  # never crash on block characters in odd consoles
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    if sys.platform == "win32" and TTY:
        TTY = enable_windows_colors()

    if args.dry_run:
        keyboard = DryRunKeyboard()
    else:
        try:
            keyboard = SystemKeyboard({char for keys in KEYMAP.values() for char in keys.values()})
        except ImportError:
            sys.exit(f"pynput is missing – install the requirements first:  {INSTALL_HINT}")

    server = BuzzServer(keyboard)
    atexit.register(server.keys.release_all)  # last line of defence against stuck keys
    if sys.platform == "win32":
        console_handler = release_keys_on_console_close(server.keys)  # noqa: F841 – keep alive
    try:
        return asyncio.run(serve(server, args.port, args.dry_run))
    except KeyboardInterrupt:
        return 0
    finally:
        server.keys.release_all()


# ─── Phone web page ────────────────────────────────────────────────────────

MANIFEST = json.dumps({
    "name": "Buzz! Buzzer", "short_name": "Buzzer", "start_url": "/", "display": "standalone",
    "orientation": "portrait", "background_color": "#0c0c0f", "theme_color": "#0c0c0f",
    "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
})

ICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<defs><radialGradient id="g" cx=".38" cy=".3" r=".75"><stop offset="0" stop-color="#ffc2b8"/>
<stop offset=".35" stop-color="#ff2b1f"/><stop offset="1" stop-color="#7a0000"/></radialGradient></defs>
<rect width="64" height="64" rx="14" fill="#18181d"/><circle cx="32" cy="32" r="22" fill="#0b0b0e"/>
<circle cx="32" cy="32" r="18" fill="url(#g)"/></svg>"""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
<meta name="theme-color" content="#0c0c0f">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Buzzer">
<meta name="format-detection" content="telephone=no">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<title>Buzz! Buzzer</title>
<style>
:root {
  --bg: #0c0c0f; --shell: #1d1d23; --shell-2: #141418;
  --text: #f5f5f7; --muted: #9c9ca8;
  --ok: #22c55e; --bad: #ef4444; --wait: #f59e0b;
}
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
html, body {
  margin: 0; height: 100%; overflow: hidden; overscroll-behavior: none;
  background: var(--bg); color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  -webkit-user-select: none; user-select: none; -webkit-touch-callout: none;
  touch-action: none; -webkit-text-size-adjust: 100%;
}
body { position: fixed; inset: 0; }
button {
  font: inherit; color: inherit; border: 0; margin: 0; padding: 0; background: none;
  cursor: pointer; touch-action: none; -webkit-user-select: none; user-select: none;
}
button:focus { outline: none; }
[hidden] { display: none !important; }

#app {
  position: fixed; inset: 0; display: flex; flex-direction: column; gap: 12px;
  padding: max(10px, env(safe-area-inset-top)) max(14px, env(safe-area-inset-right))
           max(14px, env(safe-area-inset-bottom)) max(14px, env(safe-area-inset-left));
  background: radial-gradient(120% 70% at 50% 0%, #1e1e27 0%, var(--bg) 62%);
}

/* Header: connection · player · switch */
.bar {
  display: grid; grid-template-columns: 1fr auto 1fr; align-items: center;
  min-height: 30px; font-size: 14px; color: var(--muted);
  width: 100%; max-width: 460px; align-self: center;
}
.status { display: flex; align-items: center; gap: 8px; justify-self: start; white-space: nowrap; }
.status i { width: 10px; height: 10px; border-radius: 50%; background: var(--wait); flex: none; }
body[data-state=on] .status i { background: var(--ok); box-shadow: 0 0 0 3px rgba(34,197,94,.22), 0 0 10px rgba(34,197,94,.7); }
body[data-state=off] .status i { background: var(--bad); box-shadow: 0 0 0 3px rgba(239,68,68,.25); }
body[data-state=wait] .status i, body[data-state=off] .status i { animation: blink 1s ease-in-out infinite; }
@keyframes blink { 50% { opacity: .3; } }
.who { justify-self: center; color: var(--text); font-weight: 750; font-size: 16px; }
.switch {
  justify-self: end; color: var(--muted); font-size: 13px; padding: 6px 0 6px 10px; white-space: nowrap;
  text-decoration: underline; text-underline-offset: 3px; touch-action: manipulation;
}

/* Player selection */
.pick { flex: 1; min-height: 0; display: flex; flex-direction: column; width: 100%; max-width: 460px; align-self: center; }
.pick h1 { margin: 6px 0 16px; text-align: center; font-size: clamp(22px, 6.4vw, 30px); font-weight: 800; letter-spacing: -.01em; }
.tiles { flex: 1; min-height: 0; display: grid; grid-template-columns: 1fr 1fr; grid-auto-rows: 1fr; gap: 12px; }
.tile {
  display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px;
  border-radius: 26px; touch-action: manipulation;
  background: linear-gradient(180deg, var(--shell), var(--shell-2));
  box-shadow: inset 0 1px 0 rgba(255,255,255,.07), 0 8px 24px rgba(0,0,0,.45);
  transition: transform .08s ease-out;
}
.tile:active { transform: scale(.97); }
.tile.last { box-shadow: inset 0 0 0 2px rgba(255,64,48,.85), 0 8px 24px rgba(0,0,0,.45); }
.tile .mini {
  width: 34px; height: 34px; border-radius: 50%;
  background: radial-gradient(circle at 36% 30%, #ffc2b8 0 7%, #ff3b2f 32%, #b40000 78%);
  box-shadow: 0 0 0 4px #0b0b0e, 0 0 16px 4px rgba(255,40,30,.35);
}
.tile b { font-size: clamp(56px, 18vw, 96px); line-height: 1; font-weight: 800; }
.tile .label { font-size: 14px; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: .16em; }

/* The buzzer */
.pad {
  flex: 1; min-height: 0; width: 100%; max-width: 460px; align-self: center;
  display: flex; flex-direction: column; align-items: center;
  gap: clamp(14px, 2.6vh, 26px); gap: clamp(14px, 2.6dvh, 26px);
  padding: clamp(18px, 3vh, 30px) clamp(14px, 4vw, 22px) clamp(14px, 2.4vh, 22px);
  border-radius: 44px 44px 32px 32px;
  background: linear-gradient(180deg, #26262d 0%, #19191e 42%, #121216 100%);
  box-shadow: inset 0 1px 0 rgba(255,255,255,.08), inset 0 -2px 0 rgba(0,0,0,.4), 0 18px 40px rgba(0,0,0,.55);
  transition: filter .3s;
}
body[data-state=off] .pad, body[data-state=wait] .pad { filter: grayscale(.7) brightness(.6); }
.dome-ring {
  flex: none; border-radius: 50%; padding: 5%;
  width: min(66vw, 34vh, 300px); height: min(66vw, 34vh, 300px);
  width: min(66vw, 34dvh, 300px); height: min(66vw, 34dvh, 300px);
  background: radial-gradient(circle at 50% 28%, #4c4c56 0%, #24242a 45%, #0b0b0e 75%);
  box-shadow: 0 8px 20px rgba(0,0,0,.6), inset 0 2px 1px rgba(255,255,255,.12), inset 0 -3px 6px rgba(0,0,0,.6);
}
.dome {
  display: block; width: 100%; height: 100%; border-radius: 50%;
  background: radial-gradient(circle at 36% 28%, #ffdcd5 0 5%, #ff7b6c 13%, #ff2b1f 32%, #d10a00 58%, #780000 100%);
  box-shadow: inset 0 -14px 26px rgba(80,0,0,.55), inset 0 10px 18px rgba(255,255,255,.22),
              0 5px 0 #4d0000, 0 9px 14px rgba(0,0,0,.5);
  transition: transform .07s ease-out, filter .07s ease-out, box-shadow .07s ease-out;
}
.dome.down {
  transform: translateY(4px) scale(.965); filter: brightness(1.32) saturate(1.15);
  box-shadow: inset 0 -10px 22px rgba(80,0,0,.4), inset 0 8px 16px rgba(255,255,255,.3),
              0 1px 0 #4d0000, 0 0 40px 10px rgba(255,45,30,.7), 0 0 110px 34px rgba(255,20,0,.35);
}
.keys { flex: 1; min-height: 0; width: 100%; display: flex; flex-direction: column; gap: clamp(10px, 1.8vh, 16px); }
.key {
  flex: 1 1 0; min-height: 44px; width: 100%; border-radius: 20px;
  font-size: clamp(13px, 2.2vh, 18px); font-weight: 800; letter-spacing: .16em; text-transform: uppercase;
  color: var(--ink);
  background: linear-gradient(180deg, var(--hi) 0%, var(--c) 55%, var(--lo) 100%);
  box-shadow: inset 0 2px 0 rgba(255,255,255,.4), inset 0 -3px 0 rgba(0,0,0,.15),
              0 6px 0 var(--edge), 0 10px 16px rgba(0,0,0,.45);
  transition: transform .07s ease-out, filter .07s ease-out, box-shadow .07s ease-out;
}
.key.down {
  transform: translateY(5px); filter: brightness(1.18) saturate(1.1);
  box-shadow: inset 0 2px 0 rgba(255,255,255,.35), inset 0 -2px 0 rgba(0,0,0,.12),
              0 1px 0 var(--edge), 0 0 30px 6px var(--glow);
}
.blue   { --hi: #7ea5ff; --c: #2f6bff; --lo: #1f4fd8; --edge: #12318f; --glow: rgba(60,120,255,.75); --ink: rgba(255,255,255,.92); }
.orange { --hi: #ffc07a; --c: #ff8a1f; --lo: #ec6f00; --edge: #8f4300; --glow: rgba(255,140,30,.75); --ink: rgba(70,30,0,.72); }
.green  { --hi: #7ee8a2; --c: #22c55e; --lo: #15a34a; --edge: #0b5f2b; --glow: rgba(40,210,100,.7);  --ink: rgba(0,50,20,.7); }
.yellow { --hi: #fff08a; --c: #ffd60a; --lo: #f2bf00; --edge: #8f7000; --glow: rgba(255,215,20,.75); --ink: rgba(70,55,0,.75); }

/* Phone held sideways: buzzer left, buttons right */
@media (orientation: landscape) and (max-height: 520px) {
  .pad { flex-direction: row; max-width: 760px; border-radius: 32px; }
  .dome-ring { width: min(40vw, 62vh); height: min(40vw, 62vh); width: min(40vw, 62dvh); height: min(40vw, 62dvh); }
  .keys { align-self: stretch; }
  .pick { max-width: 760px; }
  .tiles { grid-template-columns: repeat(4, 1fr); }
}
</style>
</head>
<body data-state="wait">
<div id="app">
  <header class="bar">
    <div class="status"><i></i><span id="status-text">Connecting …</span></div>
    <div class="who" id="who" hidden>Player <span id="who-n">1</span></div>
    <button type="button" class="switch" id="switch" hidden>Switch player</button>
  </header>

  <section class="pick" id="pick" hidden>
    <h1>Which buzzer are you?</h1>
    <div class="tiles">
      <button type="button" class="tile" data-player="1"><span class="mini"></span><b>1</b><span class="label">Player</span></button>
      <button type="button" class="tile" data-player="2"><span class="mini"></span><b>2</b><span class="label">Player</span></button>
      <button type="button" class="tile" data-player="3"><span class="mini"></span><b>3</b><span class="label">Player</span></button>
      <button type="button" class="tile" data-player="4"><span class="mini"></span><b>4</b><span class="label">Player</span></button>
    </div>
  </section>

  <section class="pad" id="game" hidden>
    <div class="dome-ring"><button type="button" class="dome" data-btn="red" aria-label="Red buzzer"></button></div>
    <div class="keys">
      <button type="button" class="key blue" data-btn="blue">Blue</button>
      <button type="button" class="key orange" data-btn="orange">Orange</button>
      <button type="button" class="key green" data-btn="green">Green</button>
      <button type="button" class="key yellow" data-btn="yellow">Yellow</button>
    </div>
  </section>
</div>

<script>
(() => {
  'use strict';
  const BUTTONS = ['red', 'blue', 'orange', 'green', 'yellow'];
  const STORE_KEY = 'buzz.player';
  const $ = (id) => document.getElementById(id);
  const pickEl = $('pick'), gameEl = $('game'), whoEl = $('who'), switchEl = $('switch');
  const btnEls = {};
  document.querySelectorAll('[data-btn]').forEach((el) => { btnEls[el.dataset.btn] = el; });

  function loadPlayer() {
    try {
      const n = Number(localStorage.getItem(STORE_KEY));
      return Number.isInteger(n) && n >= 1 && n <= 4 ? n : null;
    } catch (e) { return null; }
  }
  function savePlayer(n) { try { localStorage.setItem(STORE_KEY, String(n)); } catch (e) {} }

  let player = null;

  // ── Connection ────────────────────────────────────────────────────────
  let ws = null, retries = 0, retryTimer = 0, lastSeen = 0, startedAt = 0;

  function setStatus(state) {
    document.body.dataset.state = state;
    $('status-text').textContent = state === 'on' ? 'Connected' : state === 'off' ? 'Disconnected' : 'Connecting …';
  }
  function send(msg) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
  }
  function hello() { send({type: 'hello', player: player}); }

  function forget(sock) {
    if (!sock) return;
    sock.onopen = sock.onmessage = sock.onclose = sock.onerror = null;
    try { sock.close(); } catch (e) {}
  }
  function connect() {
    clearTimeout(retryTimer);
    forget(ws);
    setStatus('wait');
    const sock = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
    ws = sock;
    startedAt = Date.now();
    sock.onopen = () => { retries = 0; lastSeen = Date.now(); setStatus('on'); hello(); };
    sock.onmessage = () => { lastSeen = Date.now(); };
    sock.onclose = () => { if (ws === sock) lost(); };
  }
  function lost() {
    forget(ws);
    ws = null;
    releaseAll(false);  // the server has already released this connection's keys
    setStatus('off');
    clearTimeout(retryTimer);
    retryTimer = setTimeout(connect, Math.min(400 * 2 ** retries++, 3000));
  }
  // Ping every 2 s; if nothing comes back for 6 s, the connection is dead.
  setInterval(() => {
    if (!ws) return;
    if (ws.readyState === WebSocket.OPEN) {
      if (Date.now() - lastSeen > 6000) lost(); else send({type: 'ping'});
    } else if (ws.readyState === WebSocket.CONNECTING && Date.now() - startedAt > 5000) {
      lost();
    }
  }, 2000);

  // ── Buttons: pressed as long as at least one finger is on them ────────
  const owners = new Map();  // finger/mouse id → button
  const holds = {};          // button → ids holding it
  BUTTONS.forEach((b) => { holds[b] = new Set(); });

  function vibrate(ms) { try { if (navigator.vibrate) navigator.vibrate(ms); } catch (e) {} }

  function press(id, btn) {
    if (!player || owners.has(id)) return;
    owners.set(id, btn);
    holds[btn].add(id);
    if (holds[btn].size === 1) {
      btnEls[btn].classList.add('down');
      send({player: player, button: btn, state: 'down'});
      vibrate(btn === 'red' ? 35 : 18);
    }
  }
  function release(id) {
    const btn = owners.get(id);
    if (!btn) return;
    owners.delete(id);
    holds[btn].delete(id);
    if (holds[btn].size === 0) {
      btnEls[btn].classList.remove('down');
      send({player: player, button: btn, state: 'up'});
    }
  }
  function releaseAll(notify) {
    owners.clear();
    BUTTONS.forEach((b) => {
      if (!holds[b].size) return;
      holds[b].clear();
      btnEls[b].classList.remove('down');
      if (notify) send({player: player, button: b, state: 'up'});
    });
  }

  const buttonOf = (node) => {
    const el = node && (node.closest ? node : node.parentElement);
    return el ? el.closest('[data-btn]') : null;
  };

  // Touch (phones)
  gameEl.addEventListener('touchstart', (e) => {
    e.preventDefault();  // no zoom, no scrolling, no emulated mouse events
    for (let i = 0; i < e.changedTouches.length; i++) {
      const t = e.changedTouches[i], el = buttonOf(t.target);
      if (el) press('t' + t.identifier, el.dataset.btn);
    }
  }, {passive: false});
  const endTouches = (e) => {
    for (let i = 0; i < e.changedTouches.length; i++) release('t' + e.changedTouches[i].identifier);
  };
  document.addEventListener('touchend', endTouches);
  document.addEventListener('touchcancel', endTouches);

  // Mouse/pen (desktop) – touch pointers are already handled by the touch code above
  const HAS_TOUCH = 'ontouchstart' in window;
  gameEl.addEventListener('pointerdown', (e) => {
    if ((e.pointerType === 'touch' && HAS_TOUCH) || e.button !== 0) return;
    const el = buttonOf(e.target);
    if (!el) return;
    e.preventDefault();
    try { el.setPointerCapture(e.pointerId); } catch (err) {}
    press('p' + e.pointerId, el.dataset.btn);
  });
  const endPointer = (e) => release('p' + e.pointerId);
  document.addEventListener('pointerup', endPointer);
  document.addEventListener('pointercancel', endPointer);
  document.addEventListener('lostpointercapture', endPointer, true);

  // Page hidden or left (phone call, app switch …): nothing may stay pressed
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { releaseAll(true); return; }
    if (!ws || ws.readyState > WebSocket.OPEN) connect();
    else if (ws.readyState === WebSocket.OPEN) { lastSeen = Date.now(); send({type: 'ping'}); }
  });
  window.addEventListener('pagehide', () => releaseAll(true));
  window.addEventListener('blur', () => releaseAll(true));
  window.addEventListener('online', () => { if (!ws || ws.readyState !== WebSocket.OPEN) connect(); });

  // No zooming, scrolling, pull-to-refresh, context menu or text selection
  document.addEventListener('touchmove', (e) => e.preventDefault(), {passive: false});
  ['gesturestart', 'gesturechange', 'gestureend', 'contextmenu', 'dblclick', 'selectstart']
    .forEach((type) => document.addEventListener(type, (e) => e.preventDefault()));

  // ── Screens ───────────────────────────────────────────────────────────
  function showPick() {
    releaseAll(true);
    player = null;
    hello();
    const last = loadPlayer();
    document.querySelectorAll('.tile').forEach((t) => t.classList.toggle('last', Number(t.dataset.player) === last));
    gameEl.hidden = whoEl.hidden = switchEl.hidden = true;
    pickEl.hidden = false;
  }
  function showGame(n) {
    player = n;
    savePlayer(n);
    hello();
    $('who-n').textContent = n;
    pickEl.hidden = true;
    gameEl.hidden = whoEl.hidden = switchEl.hidden = false;
  }
  document.querySelectorAll('.tile').forEach((t) => {
    t.addEventListener('click', () => showGame(Number(t.dataset.player)));
  });
  switchEl.addEventListener('click', showPick);

  const saved = loadPlayer();
  if (saved) showGame(saved); else showPick();
  connect();
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
