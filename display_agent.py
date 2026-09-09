#!/usr/bin/env python3
"""
Kiosk display agent -- runs on the customer-facing laptop.

It owns the Chrome kiosk process and exposes exactly two actions to the POS
computer on the LAN:

    reload      relaunch Chrome at the URL it already has
    set url     accept a new URL and relaunch at that

That is the whole surface. The agent has no idea Odoo exists and holds no
Odoo credentials -- deliberately, because this machine is the insecure one.
Anything that needs a login happens on the POS computer instead.

Three things stand between the LAN and the display:

  * every request is HMAC-signed with a shared secret (see wire.py),
  * once paired, only the machine that paired is accepted, and
  * a new URL must parse to a hostname on the configured allowlist.

The first is the one that matters; the other two are layers on top. Note that
allowed_hosts defaults to ["*"], which accepts any host -- narrow it to your
Odoo host if you want the guarantee that the display can only ever show a page
from your own server.

Standard library only, and no third-party packages. Developed for Windows;
Chrome is located per-platform, so it also runs on Linux and macOS.

Setup needs no typing on this machine beyond starting it. Until the POS
computer has talked to us once, Chrome shows a full-screen pairing page with
this laptop's address and a twelve-character code -- which is simply the
shared secret in a readable form. Somebody reads it off the screen and types
it on the POS computer, which has a keyboard. There is no pairing endpoint and
nothing unauthenticated is ever exposed: the first correctly-signed request
proves the other machine has the code, and the display returns to normal.

Usage:
    python display_agent.py init       write a starter config and a pairing code
    python display_agent.py run        launch Chrome and serve requests
    python display_agent.py stop       stop a running agent on this machine
    python display_agent.py show-code  print the pairing code and this IP
    python display_agent.py set URL    change the saved URL locally, no network

The agent runs under pythonw.exe with no window and no console, so `stop` is
the intended way to end it -- it will not be obvious in Task Manager's default
view (look under Details, not Processes).
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import wire

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "agent.config.json"
LOG_PATH = HERE / "agent.log"

IS_WINDOWS = os.name == "nt"

# Keeps Chrome and taskkill from flashing a console window on the display.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0

if IS_WINDOWS:
    CHROME_CANDIDATES = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
elif sys.platform == "darwin":
    CHROME_CANDIDATES = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        os.path.expanduser(
            "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        ),
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]
else:
    CHROME_CANDIDATES = [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/opt/google/chrome/chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
        "/var/lib/flatpak/exports/bin/com.google.Chrome",
    ]

# Tried after the fixed paths above, since a distribution may put the browser
# somewhere none of them cover but still have it on PATH.
CHROME_ON_PATH = [
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "chrome",
]

DEFAULT_CONFIG = {
    "listen_host": "0.0.0.0",
    "listen_port": 8765,
    "shared_secret": "",
    "url": "",
    # Any host by default. A fresh config that refuses every URL is a trap --
    # the point of the tool is to put a page on the display, and the first
    # thing a locked-down default does is stop that working with an error that
    # reads like a bug. Narrow it to your Odoo host if you want the tighter
    # guarantee; see the allowlist note in the README for what that buys.
    "allowed_hosts": ["*"],
    "allowed_schemes": ["https"],
    "allowed_clients": [],
    "chrome_path": "auto",
    "user_data_dir": str(HERE / "chrome-profile"),
    "restart_if_chrome_exits": True,
    # False until the POS computer has successfully talked to us once. While
    # it is False the display shows the pairing code instead of the customer
    # display, which is how the code gets from this keyboard-less laptop to
    # the other machine without anyone transcribing a long secret.
    "paired": False,
}

PAIRING_PAGE = HERE / "pairing.html"
WAITING_PAGE = HERE / "waiting.html"

# How many addresses may be remembered in allowed_clients. Enough for a couple
# of POS computers plus a history of DHCP leases, capped so the list cannot
# grow forever.
MAX_ALLOWED_CLIENTS = 8


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------

_log_lock = threading.Lock()


def log(message: str) -> None:
    """
    Append one line to agent.log and, if there is a console, echo it.

    The agent normally runs under pythonw.exe with no console at all, so the
    file is the only record -- hence writing it on every call rather than
    buffering.
    """
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    with _log_lock:
        try:
            with LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass
        if sys.stdout is not None:
            try:
                print(line, flush=True)
            except (OSError, ValueError):
                pass


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit(
            f"No config at {CONFIG_PATH}\n"
            f"Run:  python {Path(__file__).name} init"
        )
    with CONFIG_PATH.open(encoding="utf-8") as handle:
        config = json.load(handle)
    merged = dict(DEFAULT_CONFIG)
    merged.update(config)
    return merged


def save_config(config: dict) -> None:
    """
    Write the config atomically, so a power cut mid-write cannot leave the
    display with a truncated config and no URL to come back to.
    """
    temp = CONFIG_PATH.with_suffix(".json.tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")
    os.replace(temp, CONFIG_PATH)


def cmd_init() -> int:
    if CONFIG_PATH.exists():
        print(f"{CONFIG_PATH} already exists -- leaving it alone.")
        print("Delete it first if you really want a fresh one.")
        return 1

    config = dict(DEFAULT_CONFIG)
    config["shared_secret"] = wire.parse_pairing_code(wire.generate_pairing_code())
    save_config(config)

    print(f"Wrote {CONFIG_PATH}\n")
    print("It works as-is. Optionally set:")
    print('  "url"            the customer display URL from Odoo, if you have')
    print("                   it handy -- otherwise send it after pairing")
    print('  "allowed_hosts"  currently ["*"], so any host is accepted. Narrow')
    print('                   it to e.g. ["yourcompany.odoo.com"] to guarantee')
    print("                   the display can only ever show your own server")
    print("\nThen start the agent. It will show this pairing code on the screen,")
    print("so you do not have to copy anything off this machine by hand:\n")
    print(f"  {wire.format_pairing_code(config['shared_secret'])}\n")
    return 0


def cmd_show_code() -> int:
    """Print the pairing code, for when the screen is not the easiest place to read it."""
    config = load_config()
    if not config.get("shared_secret"):
        print("No pairing code set. Run: python display_agent.py init", file=sys.stderr)
        return 1
    print(f"\n  Pairing code:  {wire.format_pairing_code(config['shared_secret'])}")
    print(f"  This machine:  {local_ip()}")
    if config.get("paired"):
        print("\nAlready paired. To show the code on the display again (say, because the")
        print('POS computer was replaced), set "paired" to false in agent.config.json')
        print("and restart the agent.")
    print()
    return 0


# --------------------------------------------------------------------------
# pairing screen
# --------------------------------------------------------------------------


def local_ip() -> str:
    """
    This machine's address on the LAN.

    Opening a UDP socket toward a routable address makes the OS pick the
    interface it would actually use, which is the one the POS computer will
    reach us on. Nothing is sent -- UDP connect only sets the socket's peer.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "(could not determine -- check ipconfig)"
    finally:
        sock.close()


# Shared by both local screens. Sizing notes, since these must fit any panel
# without a scrollbar:
#
#  * border-box everywhere, so the body's padding counts inside its 100% height
#    rather than adding to it. That overflow is what put a scrollbar on the page
#    originally -- 4vh of padding on a 681px viewport is the 54px it overflowed.
#  * text scales on min(vw, vh) so it shrinks for a short screen as well as a
#    narrow one. The code's vw figure is bounded by its own width: 14 characters
#    at roughly 0.6em each, plus letter-spacing and padding, comes to about
#    10.3em, so 8.5vw keeps it inside the viewport with room to spare.
#  * overflow: hidden is the backstop, so an unexpected font metric can never
#    reintroduce a scrollbar.
#
# Not an f-string, so the CSS braces need no doubling.
PAGE_CSS = """
  *, *::before, *::after { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; overflow: hidden; }
  body {
    background: #12161c; color: #e8edf4; display: flex; flex-direction: column;
    align-items: center; justify-content: center; text-align: center;
    font-family: "Segoe UI", system-ui, sans-serif; padding: 3vh 3vw;
  }
  h1 { font-size: min(3.2vw, 5vh); font-weight: 600; margin: 0 0 .5em;
       line-height: 1.2; }
  p  { font-size: min(1.7vw, 2.6vh); color: #9aa7b8; margin: 0 0 4vh;
       max-width: 32em; line-height: 1.5; }
  .label { font-size: min(1.2vw, 1.9vh); letter-spacing: .18em;
           text-transform: uppercase; color: #7c8ba0; margin-bottom: .8em; }
  .code {
    font-family: Consolas, "SF Mono", monospace; font-weight: 700;
    font-size: min(8.5vw, 13vh); letter-spacing: .06em; line-height: 1.1;
    color: #7fd4ff; background: #1b2330; border-radius: .12em;
    padding: .3em .45em; margin-bottom: 4vh; white-space: nowrap;
    max-width: 100%;
  }
  .addr { font-family: Consolas, "SF Mono", monospace;
          font-size: min(2.8vw, 4.2vh); color: #e8edf4; white-space: nowrap; }
  .warn { color: #ffcf70; font-size: min(1.7vw, 2.6vh); margin-top: 4vh;
          max-width: 36em; line-height: 1.5; }
  .warn code { background: #2a2010; padding: .1em .35em; border-radius: .2em; }
  footer { margin-top: 4vh; font-size: min(1.4vw, 2.1vh); color: #6b7a8d; }
"""


def _write_page(path: Path, title: str, body: str) -> str:
    """Wrap a body fragment in the shared shell and return a file:// URL."""
    html = (
        '<!doctype html>\n<html><head><meta charset="utf-8">'
        f"<title>{title}</title><style>{PAGE_CSS}</style></head>\n"
        f"<body>\n{body}\n</body></html>\n"
    )
    path.write_text(html, encoding="utf-8")
    return path.resolve().as_uri()


def write_pairing_page(config: dict) -> str:
    """
    Render the pairing screen and return a file:// URL for Chrome.

    This is the whole point of the pairing design: the display laptop has no
    usable keyboard, so instead of typing a secret into it, it *shows* one.
    Big enough to read from across the counter.
    """
    code = wire.format_pairing_code(config["shared_secret"])
    body = f"""  <h1>Customer display &mdash; not paired yet</h1>
  <p>On the POS computer, run <strong>Pair With Display</strong> and enter these.</p>
  <div class="label">Pairing code</div>
  <div class="code">{code}</div>
  <div class="label">This display's address</div>
  <div class="addr">{local_ip()}:{config.get("listen_port", 8765)}</div>
  <footer>This screen disappears by itself once pairing succeeds.</footer>"""
    return _write_page(PAIRING_PAGE, "Pairing", body)


def write_waiting_page(config: dict) -> str:
    """
    Render the "paired, but nothing to show yet" screen.

    Without this the display would sit on a blank desktop whenever no URL is
    configured -- which gives no clue what is wrong, and is exactly the state
    reached by pairing before a URL has been set. Worse, if allowed_hosts is
    still empty then every URL sent from the POS computer is refused, so the
    blank screen would never resolve on its own. That specific dead end is
    called out here rather than left to be discovered.
    """
    warning = ""
    if not (config.get("allowed_hosts") or []):
        warning = (
            '\n  <div class="warn"><code>allowed_hosts</code> is empty in '
            "agent.config.json, so every URL sent to this display will be refused. "
            "Set it to your Odoo host &mdash; or to <code>[&quot;*&quot;]</code> to "
            "allow any host &mdash; then restart the agent.</div>"
        )

    body = f"""  <h1>Customer display &mdash; nothing to show yet</h1>
  <p>Paired and listening. Send it a page from the POS computer with
     <strong>Change Display URL</strong>.</p>
  <div class="label">This display's address</div>
  <div class="addr">{local_ip()}:{config.get("listen_port", 8765)}</div>{warning}
  <footer>This screen goes away as soon as a URL arrives.</footer>"""
    return _write_page(WAITING_PAGE, "Waiting for a URL", body)


# --------------------------------------------------------------------------
# URL validation
# --------------------------------------------------------------------------


class UrlRejected(Exception):
    """The requested URL is not one this display is allowed to show."""


def validate_url(url: str, config: dict) -> str:
    """
    Return the URL if the display is permitted to show it, else raise.

    The check is an exact match against the *parsed* hostname rather than a
    string prefix, which is what makes it hold up: a prefix test on
    "https://yourcompany.odoo.com" would happily accept
    "https://yourcompany.odoo.com.example.net/phish".
    """
    url = (url or "").strip()
    if not url:
        raise UrlRejected("empty URL")

    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise UrlRejected(f"unparseable URL: {exc}") from None

    scheme = parsed.scheme.lower()
    allowed_schemes = [s.lower() for s in config.get("allowed_schemes") or ["https"]]
    if "*" not in allowed_schemes and scheme not in allowed_schemes:
        raise UrlRejected(f"scheme {scheme!r} is not one of {allowed_schemes}")

    if parsed.username or parsed.password:
        raise UrlRejected("URLs carrying credentials are not accepted")

    # Required even under a wildcard host: it keeps schemes with no host part,
    # file:// above all, from turning a remote request into "show me whatever
    # is on this laptop's disk".
    host = (parsed.hostname or "").lower()
    if not host:
        raise UrlRejected("URL has no hostname")

    allowed_hosts = [h.lower().strip() for h in config.get("allowed_hosts") or []]
    if not allowed_hosts:
        raise UrlRejected(
            "allowed_hosts is empty in agent.config.json, so no URL can be accepted"
        )

    # "*" turns the allowlist off: any host goes. Worth knowing what that
    # costs -- the allowlist is what keeps a leaked pairing code from being
    # able to put an arbitrary page in front of customers, so with it off the
    # code becomes the only thing standing there.
    if "*" not in allowed_hosts and host not in allowed_hosts:
        raise UrlRejected(f"host {host!r} is not one of {allowed_hosts}")

    return url


# --------------------------------------------------------------------------
# Chrome supervision
# --------------------------------------------------------------------------


def find_chrome(configured: str) -> str:
    if configured and configured != "auto":
        if not Path(configured).exists():
            raise SystemExit(f"chrome_path points at a file that does not exist: {configured}")
        return configured
    for candidate in CHROME_CANDIDATES:
        if candidate and Path(candidate).exists():
            return candidate

    for name in CHROME_ON_PATH:
        found = shutil.which(name)
        if found:
            return found

    binary = "chrome.exe" if IS_WINDOWS else "Chrome or Chromium"
    raise SystemExit(
        f"Could not find {binary} automatically.\n"
        'Set "chrome_path" in agent.config.json to its full path.\n'
        "Looked in:\n  " + "\n  ".join(CHROME_CANDIDATES)
    )


class ChromeSupervisor:
    """
    Owns one Chrome process and can swap the URL under it.

    Chrome is launched against a dedicated --user-data-dir. That matters more
    than it looks: without it, launching chrome.exe while Chrome is already
    running just hands the URL to the existing process and exits, so we would
    never hold a live handle to anything and could not restart the display.
    A private profile guarantees our own process.
    """

    def __init__(self, config: dict):
        self.config = config
        self.chrome_path = find_chrome(config.get("chrome_path", "auto"))
        self.user_data_dir = Path(config["user_data_dir"])
        self.proc: subprocess.Popen | None = None
        self.started_at: float | None = None
        # Re-entrant because restart() calls _kill()/_launch(), which the
        # watchdog thread also reaches for under the same lock.
        self.lock = threading.RLock()
        self.stopping = False
        # When set, shown instead of the configured URL. Used for the pairing
        # screen, which is a local file:// page and so never goes through the
        # allowlist -- that check guards URLs arriving over the network, and
        # this one originates here.
        self.override_url: str | None = None

    # -- process control -------------------------------------------------

    def _target_url(self) -> str:
        return self.override_url or self.config.get("url", "")

    def _launch(self) -> None:
        url = self._target_url()
        if not url:
            # Never leave the customer looking at a bare desktop: say what the
            # display is waiting for instead.
            url = write_waiting_page(self.config)
            log("no URL configured; showing the waiting screen")

        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        args = [
            self.chrome_path,
            "--kiosk",
            f"--user-data-dir={self.user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--noerrdialogs",
            "--disable-session-crashed-bubble",
            "--disable-infobars",
            "--disable-pinch",
            "--overscroll-history-navigation=0",
            # Stop Chrome offering to restore the previous session after we
            # kill it -- that dialog would sit on the customer's screen.
            "--hide-crash-restore-bubble",
            url,
        ]
        self.proc = subprocess.Popen(args, creationflags=NO_WINDOW)
        self.started_at = time.time()
        log(f"launched Chrome (pid {self.proc.pid}) at {url}")

    def _kill(self) -> None:
        proc = self.proc
        self.proc = None
        self.started_at = None
        if proc is None or proc.poll() is not None:
            return

        # Chrome spawns a tree of child processes; terminating only the parent
        # leaves renderers behind and can leave a window on screen.
        if IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                creationflags=NO_WINDOW,
                capture_output=True,
            )
        else:
            proc.terminate()

        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log(f"stopped Chrome (pid {proc.pid})")

    # -- public API ------------------------------------------------------

    def start(self) -> None:
        with self.lock:
            self._launch()

    def show_pairing_screen(self) -> None:
        with self.lock:
            self.override_url = write_pairing_page(self.config)
            log("showing the pairing screen")

    def drop_pairing_override(self) -> None:
        """
        Stop targeting the pairing page without relaunching.

        Separate from clear_pairing_screen so a request that is going to
        restart Chrome anyway does not restart it twice -- once to leave the
        pairing screen and again to apply what was actually asked for.
        """
        with self.lock:
            if self.override_url is None:
                return
            self.override_url = None
            PAIRING_PAGE.unlink(missing_ok=True)

    def clear_pairing_screen(self) -> None:
        """Drop the pairing screen and return to the customer display now."""
        with self.lock:
            if self.override_url is None:
                return
            self.drop_pairing_override()
            self._kill()
            time.sleep(1.0)
            self._launch()

    def restart(self, url: str | None = None) -> None:
        """Relaunch Chrome, optionally at a new URL which is persisted first."""
        with self.lock:
            if url is not None and url != self.config.get("url"):
                self.config["url"] = url
                save_config(self.config)
                log(f"saved new URL: {url}")
            self._kill()
            # Chrome holds a lock on its profile directory for a moment after
            # exiting; relaunching instantly can land on a "profile in use"
            # state and show an error page to the customer.
            time.sleep(1.0)
            self._launch()

    def shutdown(self) -> None:
        with self.lock:
            self.stopping = True
            self._kill()

    def status(self) -> dict:
        with self.lock:
            alive = self.proc is not None and self.proc.poll() is None
            return {
                "url": self.config.get("url", ""),
                "paired": bool(self.config.get("paired")),
                "showing_pairing_screen": self.override_url is not None,
                "chrome_running": alive,
                "chrome_pid": self.proc.pid if alive and self.proc else None,
                "running_for_seconds": (
                    int(time.time() - self.started_at) if alive and self.started_at else None
                ),
            }

    # -- watchdog --------------------------------------------------------

    def watch(self) -> None:
        """
        Relaunch Chrome if it dies on its own.

        Without this, a Chrome crash leaves the customer looking at a blank
        desktop until someone notices. restart() holds the lock across its
        whole kill-then-launch sequence, so this loop can never mistake a
        deliberate restart for a crash.
        """
        while True:
            time.sleep(3)
            if not self.config.get("restart_if_chrome_exits", True):
                continue
            with self.lock:
                # No check for a configured URL any more: with the waiting
                # screen there is always something to keep on screen.
                if self.stopping:
                    continue
                died = self.proc is None or self.proc.poll() is not None
                if died:
                    log("Chrome is not running; relaunching")
                    self._launch()


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------


def make_handler(config: dict, chrome: ChromeSupervisor, request_shutdown=None):
    seen_nonces: dict[str, float] = {}
    nonce_lock = threading.Lock()
    secret = config["shared_secret"]

    class Handler(BaseHTTPRequestHandler):
        server_version = "KioskDisplayAgent/1.0"
        protocol_version = "HTTP/1.1"

        # -- plumbing ----------------------------------------------------

        def log_message(self, fmt, *args):  # noqa: A002 - signature set by base class
            log(f"{self.client_address[0]} {fmt % args}")

        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> bytes:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise UrlRejected("malformed Content-Length") from None
            if length < 0 or length > wire.MAX_BODY_BYTES:
                raise UrlRejected("request body too large")
            return self.rfile.read(length) if length else b""

        def _authenticate(self, body: bytes) -> None:
            # Signature only. The client allowlist is applied after this, in
            # _dispatch, so that a rejection can explain itself and so a
            # deliberate re-claim can override it.
            with nonce_lock:
                wire.verify(
                    secret,
                    self.command,
                    self.path,
                    self.headers,
                    body,
                    seen_nonces,
                )

        # -- routes ------------------------------------------------------

        def do_GET(self):  # noqa: N802 - name required by base class
            self._dispatch(b"")

        def do_POST(self):  # noqa: N802 - name required by base class
            try:
                body = self._read_body()
            except UrlRejected as exc:
                self._send(400, {"error": str(exc)})
                return
            self._dispatch(body)

        def _dispatch(self, body: bytes) -> None:
            try:
                self._authenticate(body)
            except wire.AuthError as exc:
                log(f"rejected {self.command} {self.path} from {self.client_address[0]}: {exc}")
                self._send(401, {"error": str(exc)})
                return

            # Reaching here means the request carried a valid signature, so
            # whoever sent it already knows the pairing code. That is exactly
            # what pairing was waiting to establish -- so the code comes off
            # the screen and the customer display goes back up, with no
            # separate pairing endpoint and nothing unauthenticated exposed.
            peer = self.client_address[0]

            # "claim=1" rides in the query string rather than a header so that
            # it falls inside the signed path -- a header could be bolted onto
            # an intercepted request, a query string cannot.
            route = urlparse(self.path).path
            claiming = "claim=1" in urlparse(self.path).query
            allowed = [c.strip() for c in (config.get("allowed_clients") or []) if c.strip()]

            # The allowlist is a second, weaker layer on top of the signature:
            # anyone able to sign already holds the pairing code. So a caller
            # that can sign is allowed to take the display over deliberately,
            # which is what stops a DHCP lease change from locking the POS
            # computer out with no way back short of walking to the display.
            if allowed and peer not in allowed and not claiming:
                log(f"refused {self.command} {self.path} from {peer}: not in allowed_clients {allowed}")
                self._send(403, {
                    # The reason code lets the client recognise this exact case
                    # and re-claim by itself. Matching on the prose would break
                    # the moment the wording changed.
                    "reason": "client_not_allowed",
                    "expected": allowed,
                    "you": peer,
                    "error": (
                        f"this display only accepts requests from {', '.join(allowed)}, "
                        f"and you are {peer}. Run 'refresh.py pair' from this machine "
                        f"to take it over, or edit allowed_clients on the display."
                    ),
                })
                return

            dirty = False
            newly_paired = False
            if not config.get("paired"):
                # First pairing starts the list clean.
                config["paired"] = True
                config["allowed_clients"] = [peer]
                newly_paired = True
                dirty = True
                log(f"paired with {peer}; allowed_clients set to [{peer}]")

            elif claiming and peer not in allowed:
                # Add rather than replace. Replacing would make two POS
                # computers sharing one display take it from each other on
                # every request, re-claiming and rewriting the config each
                # time. Newest first, oldest evicted past the cap, so a run of
                # DHCP changes cannot grow the list without bound.
                #
                # Stale entries are harmless: this list only filters *who may
                # ask*, and every request still has to carry a valid signature.
                merged = [peer] + [a for a in allowed if a != peer]
                config["allowed_clients"] = merged[:MAX_ALLOWED_CLIENTS]
                dirty = True
                log(f"{peer} claimed access; allowed_clients now {config['allowed_clients']}")

            if dirty:
                save_config(config)

            if route == "/status" and self.command == "GET":
                if newly_paired:
                    chrome.clear_pairing_screen()
                self._send(200, {"ok": True, "newly_paired": newly_paired, **chrome.status()})
                return

            if route == "/refresh" and self.command == "POST":
                log(f"reload requested by {self.client_address[0]}")
                chrome.drop_pairing_override()
                chrome.restart()
                self._send(200, {"ok": True, "newly_paired": newly_paired, **chrome.status()})
                return

            if route == "/url" and self.command == "POST":
                try:
                    payload = json.loads(body.decode("utf-8")) if body else {}
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    self._send(400, {"error": f"body is not valid JSON: {exc}"})
                    return
                if not isinstance(payload, dict):
                    self._send(400, {"error": "body must be a JSON object"})
                    return
                try:
                    url = validate_url(payload.get("url", ""), config)
                except UrlRejected as exc:
                    log(f"refused URL from {self.client_address[0]}: {exc}")
                    self._send(403, {"error": f"URL refused: {exc}"})
                    return
                log(f"new URL from {self.client_address[0]}: {url}")
                chrome.drop_pairing_override()
                chrome.restart(url)
                self._send(200, {"ok": True, "newly_paired": newly_paired, **chrome.status()})
                return

            if route == "/shutdown" and self.command == "POST":
                # Deliberately reachable from the POS computer, not just from
                # loopback. The display is a full-screen kiosk whose watchdog
                # relaunches Chrome within seconds, on a laptop with no usable
                # keyboard -- so there is no practical way to run anything on
                # the machine itself. A local-only stop would be a stop nobody
                # could ever reach.
                #
                # The signature is what guards it, as with every other route.
                # The worst a stop can do is blank the display until someone
                # starts it again, which is both obvious and recoverable --
                # unlike a repointed display, which is the thing the host
                # allowlist exists to prevent.
                log(f"shutdown requested by {self.client_address[0]}")
                # Answer before stopping, or the caller sees a dropped
                # connection rather than a confirmation.
                self._send(200, {"ok": True, "stopping": True})
                if request_shutdown is not None:
                    request_shutdown()
                return

            self._send(404, {"error": f"no such endpoint: {self.command} {self.path}"})

    return Handler


def cmd_run() -> int:
    config = load_config()

    if not config.get("shared_secret"):
        raise SystemExit(
            "No pairing code in agent.config.json.\n"
            f"Delete the file and run:  python {Path(__file__).name} init"
        )
    if not config.get("allowed_hosts"):
        log("WARNING: allowed_hosts is empty -- reloads will work, but no new URL can be set")

    # Locating Chrome can fail (installed somewhere non-standard), and it
    # happens before anything else. Log it: under pythonw.exe there is no
    # console, so an unlogged exit here looks exactly like nothing happening.
    try:
        chrome = ChromeSupervisor(config)
    except SystemExit as exc:
        log(f"FATAL: {exc}")
        raise

    host, port = config["listen_host"], int(config["listen_port"])

    # The server object is not built until after the handler needs to be able
    # to reach it, so it arrives by way of this holder.
    server_holder: dict = {}

    def request_shutdown() -> None:
        srv = server_holder.get("server")
        if srv is not None:
            # serve_forever() cannot be stopped from the thread serving the
            # request, so hand the stop to another one.
            threading.Thread(target=srv.shutdown, daemon=True).start()

    handler = make_handler(config, chrome, request_shutdown)

    # Bind BEFORE launching Chrome. A second copy of the agent would otherwise
    # put another Chrome on screen, fail to take the port, and exit -- and
    # under pythonw.exe there is no console for that error to appear in, so it
    # looks simply like nothing happened.
    try:
        server = ThreadingHTTPServer((host, port), handler)
    except OSError as exc:
        in_use = (
            getattr(exc, "errno", None) == errno.EADDRINUSE
            or getattr(exc, "winerror", None) == 10048
        )
        log(f"FATAL: cannot listen on {host}:{port} -- {exc}")
        hint = ""
        if in_use:
            hint = (
                "Another copy of the agent is almost certainly already running.\n"
                f"Stop it with:  python {Path(__file__).name} stop"
            )
            log(hint.replace("\n", " "))
        raise SystemExit(f"Cannot listen on {host}:{port}: {exc}\n{hint}".rstrip())

    server_holder["server"] = server
    server.daemon_threads = True
    log(f"agent listening on {host}:{port}")

    if not config.get("paired"):
        code = wire.format_pairing_code(config["shared_secret"])
        log(f"not paired yet -- showing pairing code {code} at {local_ip()}")
        chrome.show_pairing_screen()

    chrome.start()

    watchdog = threading.Thread(target=chrome.watch, daemon=True, name="chrome-watchdog")
    watchdog.start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("interrupted; shutting down")
    finally:
        server.server_close()
        chrome.shutdown()
        WAITING_PAGE.unlink(missing_ok=True)
    log("agent stopped")
    return 0


def cmd_stop() -> int:
    """
    Ask a running agent on this machine to stop.

    Goes through the same signed HTTP interface rather than hunting for a
    process, which means it works however the agent was started -- and matters
    on this machine in particular, where the agent runs under pythonw.exe with
    no window and no console and so is genuinely hard to find by hand.
    """
    config = load_config()
    port = int(config.get("listen_port", 8765))
    body = b"{}"
    headers = wire.build_headers(config["shared_secret"], "POST", "/shutdown", body)
    headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/shutdown", data=body, headers=headers, method="POST"
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()
    except urllib.error.URLError as exc:
        print(f"Nothing answering on 127.0.0.1:{port} -- the agent is probably not running.")
        print(f"  ({getattr(exc, 'reason', exc)})\n")
        print("If you think it is running anyway, find it from PowerShell with:")
        print("  Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR Name='python.exe'\" |")
        print("    Select-Object ProcessId, CommandLine | Format-List")
        return 1

    print("Agent stopped, and its Chrome window with it.")
    return 0


def cmd_set(url: str) -> int:
    """Change the saved URL from the display laptop itself, without the network."""
    config = load_config()
    try:
        checked = validate_url(url, config)
    except UrlRejected as exc:
        print(f"URL refused: {exc}", file=sys.stderr)
        return 1
    config["url"] = checked
    save_config(config)
    print(f"Saved. Restart the agent to show it:\n  {checked}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Kiosk display agent for the Odoo customer display.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("init", help="write a starter config with a fresh pairing code")
    sub.add_parser("run", help="launch Chrome and serve refresh requests (default)")
    sub.add_parser("stop", help="stop the agent running on this machine")
    sub.add_parser("show-code", help="print the pairing code and this machine's address")
    set_parser = sub.add_parser("set", help="change the saved URL locally, without the network")
    set_parser.add_argument("url")

    args = parser.parse_args()
    if args.command == "init":
        return cmd_init()
    if args.command == "stop":
        return cmd_stop()
    if args.command == "show-code":
        return cmd_show_code()
    if args.command == "set":
        return cmd_set(args.url)
    return cmd_run()


if __name__ == "__main__":
    sys.exit(main())
