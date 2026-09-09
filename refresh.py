#!/usr/bin/env python3
"""
Customer display remote -- runs on the POS computer.

    python refresh.py              reload the display (the everyday case)
    python refresh.py set          paste in a new URL, then reload
    python refresh.py set URL      send a URL directly
    python refresh.py status       ask what the display is currently showing
    python refresh.py init         write a starter config

This machine is the trusted side of the pair. If you later want the URL
fetched from Odoo automatically rather than pasted, that belongs here -- the
display laptop never needs to learn anything new.

Standard library only. Tested against Python 3.11 and 3.13 on Windows.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import wire

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "refresh.config.json"

DEFAULT_CONFIG = {
    "agent_host": "192.168.1.51",
    "agent_port": 8765,
    "shared_secret": "",
    "timeout_seconds": 30,
}


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
    if not merged.get("shared_secret"):
        raise SystemExit(
            f'shared_secret is empty in {CONFIG_PATH}\n'
            "Paste in the secret that display_agent.py printed when you ran its init."
        )
    return merged


def cmd_init() -> int:
    if CONFIG_PATH.exists():
        print(f"{CONFIG_PATH} already exists -- leaving it alone.")
        return 1
    with CONFIG_PATH.open("w", encoding="utf-8") as handle:
        json.dump(DEFAULT_CONFIG, handle, indent=2)
        handle.write("\n")
    print(f"Wrote {CONFIG_PATH}\n")
    print("Now edit it and fill in:")
    print('  "agent_host"     the display laptop\'s IP address on the LAN')
    print('  "shared_secret"  the secret display_agent.py printed during its init')
    return 0


def call(config: dict, method: str, path: str, payload: dict | None = None) -> dict:
    """Send one signed request to the agent and return its JSON reply."""
    body = json.dumps(payload).encode("utf-8") if payload is not None else b""
    url = f"http://{config['agent_host']}:{config['agent_port']}{path}"

    headers = wire.build_headers(config["shared_secret"], method, path, body)
    if payload is not None:
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=body or None, headers=headers, method=method)

    try:
        with urllib.request.urlopen(request, timeout=config["timeout_seconds"]) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # The agent explains its refusals in the body; surfacing that is the
        # difference between "403" and "that host is not on the allowlist".
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", "")
        except (ValueError, OSError):
            pass
        raise SystemExit(f"Display refused the request ({exc.code}): {detail or exc.reason}") from None
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"Could not reach the display at {config['agent_host']}:{config['agent_port']}\n"
            f"  {exc.reason}\n"
            "Check the display laptop is on, on the same network, and that the agent is running."
        ) from None


def show(result: dict) -> None:
    where = result.get("url") or "(no URL set)"
    state = "running" if result.get("chrome_running") else "NOT running"
    print(f"  display: Chrome is {state}")
    print(f"  showing: {where}")


def cmd_refresh(config: dict) -> int:
    print("Reloading the customer display...")
    show(call(config, "POST", "/refresh", {}))
    print("Done.")
    return 0


def cmd_set(config: dict, url: str | None) -> int:
    if not url:
        print("Paste the customer display URL from Odoo, then press Enter.")
        print("(Leave it blank and press Enter to cancel.)\n")
        try:
            url = input("URL: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return 1
        if not url:
            print("Cancelled.")
            return 1

    print("\nSending the new URL to the display...")
    show(call(config, "POST", "/url", {"url": url}))
    print("Done.")
    return 0


def cmd_status(config: dict) -> int:
    show(call(config, "GET", "/status"))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reload or repoint the Odoo customer display on the other machine.",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("init", help="write a starter config")
    sub.add_parser("status", help="ask what the display is currently showing")
    set_parser = sub.add_parser("set", help="send a new URL (prompts if you omit it)")
    set_parser.add_argument("url", nargs="?", help="the URL; omit to be prompted for a paste")

    args = parser.parse_args()

    if args.command == "init":
        return cmd_init()

    config = load_config()
    if args.command == "set":
        return cmd_set(config, args.url)
    if args.command == "status":
        return cmd_status(config)
    return cmd_refresh(config)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(1)
