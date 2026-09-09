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

Two things stand between the LAN and the display:

  * every request is HMAC-signed with a shared secret (see wire.py), and
  * a new URL must parse to a hostname on the configured allowlist,

so even if the secret leaked, the worst anyone could do is point the display
at a different page on your own Odoo server.

Standard library only. Tested against Python 3.11 and 3.13 on Windows.

Usage:
    python display_agent.py init     write a starter config and a fresh secret
    python display_agent.py run      launch Chrome and serve requests
    python display_agent.py set URL  change the saved URL locally, no network
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
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

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]

DEFAULT_CONFIG = {
    "listen_host": "0.0.0.0",
    "listen_port": 8765,
    "shared_secret": "",
    "url": "",
    "allowed_hosts": [],
    "allowed_schemes": ["https"],
    "allowed_clients": [],
    "chrome_path": "auto",
    "user_data_dir": str(HERE / "chrome-profile"),
    "restart_if_chrome_exits": True,
}


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
    config["shared_secret"] = wire.generate_secret()
    save_config(config)

    print(f"Wrote {CONFIG_PATH}\n")
    print("Now edit it and fill in:")
    print('  "url"            the customer display URL from Odoo')
    print('  "allowed_hosts"  e.g. ["yourcompany.odoo.com"] -- the display can')
    print("                   only ever be pointed at a host in this list")
    print('  "allowed_clients" optionally the POS computer\'s IP, e.g. ["192.168.1.50"]')
    print("\nThen copy this shared secret into refresh.config.json on the POS computer:\n")
    print(f"  {config['shared_secret']}\n")
    return 0


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
    if scheme not in allowed_schemes:
        raise UrlRejected(f"scheme {scheme!r} is not one of {allowed_schemes}")

    if parsed.username or parsed.password:
        raise UrlRejected("URLs carrying credentials are not accepted")

    host = (parsed.hostname or "").lower()
    if not host:
        raise UrlRejected("URL has no hostname")

    allowed_hosts = [h.lower().strip() for h in config.get("allowed_hosts") or []]
    if not allowed_hosts:
        raise UrlRejected(
            "allowed_hosts is empty in agent.config.json, so no URL can be accepted"
        )
    if host not in allowed_hosts:
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
    raise SystemExit(
        "Could not find chrome.exe automatically.\n"
        'Set "chrome_path" in agent.config.json to its full path.'
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

    # -- process control -------------------------------------------------

    def _launch(self) -> None:
        url = self.config.get("url", "")
        if not url:
            log("no URL configured; not launching Chrome")
            return

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
                if self.stopping or not self.config.get("url"):
                    continue
                died = self.proc is None or self.proc.poll() is not None
                if died:
                    log("Chrome is not running; relaunching")
                    self._launch()


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------


def make_handler(config: dict, chrome: ChromeSupervisor):
    seen_nonces: dict[str, float] = {}
    nonce_lock = threading.Lock()
    secret = config["shared_secret"]
    allowed_clients = [c.strip() for c in config.get("allowed_clients") or [] if c.strip()]

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
            if allowed_clients and self.client_address[0] not in allowed_clients:
                raise wire.AuthError(f"client {self.client_address[0]} is not in allowed_clients")
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

            if self.path == "/status" and self.command == "GET":
                self._send(200, {"ok": True, **chrome.status()})
                return

            if self.path == "/refresh" and self.command == "POST":
                log(f"reload requested by {self.client_address[0]}")
                chrome.restart()
                self._send(200, {"ok": True, **chrome.status()})
                return

            if self.path == "/url" and self.command == "POST":
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
                chrome.restart(url)
                self._send(200, {"ok": True, **chrome.status()})
                return

            self._send(404, {"error": f"no such endpoint: {self.command} {self.path}"})

    return Handler


def cmd_run() -> int:
    config = load_config()

    if not config.get("shared_secret"):
        raise SystemExit(
            'shared_secret is empty in agent.config.json.\n'
            f"Delete the file and run:  python {Path(__file__).name} init"
        )
    if not config.get("allowed_hosts"):
        log("WARNING: allowed_hosts is empty -- reloads will work, but no new URL can be set")

    chrome = ChromeSupervisor(config)
    log(f"agent starting on {config['listen_host']}:{config['listen_port']}")
    chrome.start()

    watchdog = threading.Thread(target=chrome.watch, daemon=True, name="chrome-watchdog")
    watchdog.start()

    handler = make_handler(config, chrome)
    server = ThreadingHTTPServer((config["listen_host"], int(config["listen_port"])), handler)
    server.daemon_threads = True

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("interrupted; shutting down")
    finally:
        server.server_close()
        chrome.shutdown()
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
    sub.add_parser("init", help="write a starter config with a fresh shared secret")
    sub.add_parser("run", help="launch Chrome and serve refresh requests (default)")
    set_parser = sub.add_parser("set", help="change the saved URL locally, without the network")
    set_parser.add_argument("url")

    args = parser.parse_args()
    if args.command == "init":
        return cmd_init()
    if args.command == "set":
        return cmd_set(args.url)
    return cmd_run()


if __name__ == "__main__":
    sys.exit(main())
