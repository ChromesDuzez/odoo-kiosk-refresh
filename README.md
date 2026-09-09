# Odoo customer display refresh

Reload or repoint the customer-facing display from the POS computer next to it,
without touching the display laptop (which has no usable keyboard, since it is
folded back on itself).

Two pieces, both stdlib-only Python — nothing to `pip install`, no venv:

| File | Machine | What it does |
|---|---|---|
| `display_agent.py` | display laptop | Owns the Chrome kiosk process, listens on the LAN |
| `refresh.py` | POS computer | Sends it a reload, or a new URL |
| `Refresh Display.cmd` | POS computer | Double-click wrapper around `refresh.py` |
| `Change Display URL.cmd` | POS computer | Double-click wrapper that prompts for a paste |
| `install-agent.ps1` | display laptop | Autostart at logon + firewall rule |

Copy this whole folder to both machines. Each one uses its own half plus the
shared `wire.py`.

## Everyday use

At the counter, when the display goes wrong:

1. Double-click **Refresh Display** on the POS computer. It closes and
   relaunches Chrome on the other screen at the URL it already has. This fixes
   almost everything.
2. If the URL itself has gone stale, double-click **Change Display URL**, paste
   the new one from Odoo, press Enter.

From a terminal, the same things:

```
python refresh.py           reload
python refresh.py set       prompt for a URL, then reload
python refresh.py set URL   send a URL directly
python refresh.py status    what is it showing right now?
```

## Why it is built this way

The display laptop is the insecure machine — it is physically customer-facing
and deliberately has very little on it. So **it holds no Odoo credentials and
does not know Odoo exists.** It only knows how to restart Chrome at a URL.
Anything requiring a login stays on the POS computer.

Two controls protect the display:

- **Signed requests.** Every request carries an HMAC-SHA256 signature over the
  method, path, timestamp, nonce and body. A captured reload cannot be replayed
  as a repoint, and nothing can be replayed at all after 60 seconds.
- **A host allowlist.** A new URL must parse to a hostname you listed in
  `agent.config.json`. This is the control that actually matters: even if the
  shared secret leaked, the display can only ever be pointed at another page on
  your own Odoo server — never at an attacker's page. The check compares the
  *parsed* hostname exactly, so `yourcompany.odoo.com.evil.net` is rejected.

The traffic is plain HTTP on your LAN. That is fine here because the signature
is what provides authenticity, and the allowlist bounds the damage — but it does
mean anyone sniffing the LAN can see which URL you sent. The display token in
that URL is read-only, so this is a deliberate trade rather than an oversight.

## Setup

### 1. Display laptop

Give it a **static IP or a DHCP reservation** first — the POS computer needs a
stable address to talk to.

```powershell
cd <this folder>
python display_agent.py init
```

That writes `agent.config.json` and prints a shared secret. **Copy the secret**,
you need it in step 2. Then edit the config:

```jsonc
{
  "listen_port": 8765,
  "shared_secret": "...",                      // already filled in
  "url": "https://yourcompany.odoo.com/...",   // the display URL from Odoo
  "allowed_hosts": ["yourcompany.odoo.com"],   // required, or no URL can be set
  "allowed_clients": ["192.168.1.50"],         // optional: only the POS computer
  "chrome_path": "auto",
  "restart_if_chrome_exits": true
}
```

`allowed_clients` is worth filling in — with it, only the POS computer can even
attempt a request. Leave it `[]` to accept any address on the network.

To get `url`: in Odoo, **Point of Sale → Configuration → your POS →
Customer Display**. It is the same URL you originally set the kiosk up with.

Then install it to start automatically. Run this **as Administrator** so it can
also open the firewall — without that rule, Windows silently drops the requests
and the POS computer just sees a timeout:

```powershell
.\install-agent.ps1
```

It prints the laptop's IP addresses at the end. Note the LAN one.

Log out and back in, or start it now with the command it prints. Chrome should
come up in kiosk mode on its own.

### 2. POS computer

```powershell
cd <this folder>
python refresh.py init
```

Edit `refresh.config.json`:

```jsonc
{
  "agent_host": "192.168.1.51",   // the display laptop's IP
  "agent_port": 8765,             // must match listen_port
  "shared_secret": "..."          // paste the secret from step 1
}
```

Check it:

```powershell
python refresh.py status
```

If that reports the URL, you are done. Put shortcuts to the two `.cmd` files on
the desktop for the counter staff.

## Troubleshooting

**"Could not reach the display"** — the laptop is off, on a different network,
the agent is not running, or the firewall rule is missing. Confirm the agent is
alive: Task Manager on the laptop should show `pythonw.exe`. Check
`agent.log` next to `display_agent.py` for what it has been doing.

**"signature mismatch"** — the two `shared_secret` values differ. Copy it again.

**"timestamp outside the accepted window"** — the machines' clocks disagree by
more than a minute. Fix the clock on whichever one has drifted.

**"host ... is not one of ..."** — working as intended: the URL you pasted is not
on `allowed_hosts`. If your Odoo host genuinely changed, add it to the list on
the display laptop and restart the agent.

**Chrome shows a "restore pages?" bar** — it is launched with
`--hide-crash-restore-bubble` and `--disable-session-crashed-bubble`, which
should suppress it. If a Chrome update reintroduces it, delete the
`chrome-profile` folder next to the agent and let it rebuild.

**The display went blank on its own** — the agent watches Chrome and relaunches
it within a few seconds if it exits. Set `restart_if_chrome_exits` to `false` if
you ever need to stop that while debugging.

## Changing it later

The URL is pasted by hand today. If that gets tedious, the natural next step is
to have **the POS computer** fetch it from Odoo automatically — an API key in
Windows Credential Manager, a call to read the POS config's display token, then
the same `POST /url` this already sends.

That change lives entirely in `refresh.py`. The display laptop does not change,
does not gain credentials, and does not need to be touched — which is the whole
reason the split is drawn where it is.
