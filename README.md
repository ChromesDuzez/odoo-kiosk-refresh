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
| `Pair With Display.cmd` | POS computer | One-time setup wrapper |
| `Install Agent.cmd` | display laptop | Autostart at logon + firewall rule |
| `Uninstall.cmd` | either | Removes it again; run on both machines |

The `.cmd` files are the intended way in — they are double-clickable and deal
with the execution policy for you. Each wraps the `.ps1` of the same name
(`install-agent.ps1`, `uninstall.ps1`), which you can also call directly from a
PowerShell prompt. `lib-elevate.ps1` is shared plumbing, not run on its own.

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
  as a repoint, and nothing can be replayed at all after 60 seconds. There are
  no unauthenticated endpoints at all, including during pairing.
- **A host allowlist.** A new URL must parse to a hostname you listed in
  `agent.config.json`. This is the control that actually matters: even if the
  shared secret leaked, the display can only ever be pointed at another page on
  your own Odoo server — never at an attacker's page. The check compares the
  *parsed* hostname exactly, so `yourcompany.odoo.com.evil.net` is rejected.

  Set `"allowed_hosts": ["*"]` to turn this off and allow any host — handy for
  putting something else on the screen for a laugh. Just know what it costs:
  the allowlist is the layer that limits the damage of a leaked pairing code,
  so with it off, the code is the only thing standing between the LAN and
  whatever appears in front of customers. Easy to flip back.

The traffic is plain HTTP on your LAN. That is fine here because the signature
is what provides authenticity, and the allowlist bounds the damage — but it does
mean anyone sniffing the LAN can see which URL you sent. The display token in
that URL is read-only, so this is a deliberate trade rather than an oversight.

The pairing code itself never crosses the network, in either direction. It moves
between the machines only by being read off a screen, so there is no window
during setup in which sniffing the LAN would reveal it.

## Setup

Nothing long has to be copied from the display laptop, because there is nothing
long to copy. The shared secret **is** a twelve-character pairing code, and the
display shows it on its own screen in large type along with its IP address. You
read those off the screen and type them on the POS computer, which has a
keyboard. That is the entire cross-machine transfer.

### 1. Display laptop

Give it a **static IP or a DHCP reservation** first — the POS computer needs a
stable address to talk to.

```powershell
cd <this folder>
python display_agent.py init
```

That writes `agent.config.json` with a fresh pairing code already in it. Edit
the config to fill in the rest:

```jsonc
{
  "listen_port": 8765,
  "shared_secret": "K7MQ3XRT9PBW",             // already filled in, leave it
  "url": "https://yourcompany.odoo.com/...",   // the display URL from Odoo
  "allowed_hosts": ["yourcompany.odoo.com"],   // required; ["*"] allows any host
  "allowed_clients": ["192.168.1.50"],         // optional: only the POS computer
  "chrome_path": "auto",
  "restart_if_chrome_exits": true
}
```

`allowed_clients` is worth filling in — with it, only the POS computer can even
attempt a request. Leave it `[]` to accept any address on the network.

To get `url`: in Odoo, **Point of Sale → Configuration → your POS →
Customer Display**. It is the same URL you originally set the kiosk up with. You
can also leave it blank and send it from the POS computer after pairing.

Then install it to start automatically by double-clicking **Install Agent.cmd**.
It asks for Administrator rights itself — accept the UAC prompt, because the
firewall rule needs them and without it Windows silently drops the requests and
the POS computer just sees a timeout.

From a PowerShell prompt instead, if you prefer:

```powershell
powershell -ExecutionPolicy Bypass -File .\install-agent.ps1
```

Log out and back in, or start it now with the command it prints. The screen will
come up showing **its address and a pairing code**. Leave it there and walk to
the other machine — you are finished on this one.

### 2. POS computer

Double-click **Pair With Display**, or:

```powershell
cd <this folder>
python refresh.py pair
```

It asks for the address and then the code, both of which are on the display in
front of you. Dashes, spacing and capitalisation do not matter, and `O`/`0` and
`I`/`1` are treated as the same character, so there is nothing to get wrong by
misreading the screen.

When it succeeds, the display drops the pairing screen by itself and goes back
to the customer view. Put shortcuts to the three `.cmd` files on the desktop for
the counter staff, and you are done.

### Re-pairing later

If the POS computer is replaced, run `python display_agent.py show-code` on the
display to print the code again. To put it back on the screen instead, set
`"paired": false` in `agent.config.json` and restart the agent.

## Troubleshooting

**"Could not reach the display"** — the laptop is off, on a different network,
the agent is not running, or the firewall rule is missing. Confirm the agent is
alive: Task Manager on the laptop should show `pythonw.exe`. Check
`agent.log` next to `display_agent.py` for what it has been doing.

**"signature mismatch"** or **"The display rejected that code"** — the pairing
code does not match. Run `python display_agent.py show-code` on the display to
check it, then `python refresh.py pair` again.

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

## Uninstalling

Double-click **Uninstall.cmd**, and accept the UAC prompt.

Safe on either machine and safe to run twice — it works out which half is
installed from the config files present, reports what it finds, and skips what
is not there. If you decline elevation it carries on without it, but cannot
remove the firewall rule and says so rather than claiming it is gone.

It stops the running agent, closes the kiosk Chrome window, removes the Startup
shortcut and removes the firewall rule. **Your settings are kept**, so
re-running the installer later brings everything back with the same pairing code
and no need to pair again.

```
Uninstall.cmd -WhatIf     rem show what it would do, change nothing
Uninstall.cmd -Purge      rem also delete settings, logs and the Chrome profile
Uninstall.cmd -NoElevate  rem no UAC prompt; skips whatever needs rights
```

`-Purge` destroys the pairing code, so both machines would have to be paired
again from scratch. It lists what it is about to delete and makes you type
`YES` first, unless you add `-Force`.

The one thing worth knowing: it only ever closes the kiosk Chrome, never an
ordinary browser window. The kiosk runs in a dedicated profile directory, and
the script matches on that path — so the staff member's own Chrome tabs on the
POS computer are not touched. If it cannot determine the profile path for any
reason, it skips closing Chrome entirely rather than guessing.

Afterwards the scripts themselves are still on disk. Delete the folder to
finish.

## Execution policy and elevation

You never need to run `Set-ExecutionPolicy`, and nothing here changes the
machine's policy.

The `.cmd` launchers start PowerShell like this:

```
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "...\install-agent.ps1"
```

`-ExecutionPolicy` **on the command line applies to that one process**. It is
the launch-argument form of `Set-ExecutionPolicy -Scope Process -ExecutionPolicy
Bypass` — same scope, same lifetime, except you do not have to type it first and
there is no window in which a policy is loosened for anything else. The registry
is untouched, `Get-ExecutionPolicy -List` is unchanged, and the machine-wide and
user-wide policies stay exactly as your environment set them.

The elevated relaunch uses the same flags, so the Administrator process is also
process-scoped. `-NoProfile` is there deliberately too: it stops a user's
profile script from running in a process that holds Administrator rights.

**On elevation and the Startup folder.** The scripts self-elevate through UAC
rather than requiring you to right-click → Run as administrator. There is a
catch that they handle for you: the Startup folder is per-user, so if the kiosk
account is a *standard* user, UAC asks for some other admin account's
credentials and the elevated process runs as that account — whose Startup folder
is a different directory. A shortcut written there would never run at the kiosk
user's logon, and nothing would visibly fail. So the Startup path is resolved
*before* elevating and passed across to the elevated instance, which uses the
value it was handed instead of looking it up again. If the two accounts differ,
it tells you which account it is installing for.

If the kiosk account is itself an administrator — the common case on a
small-business laptop — UAC keeps the same user and none of this applies.

Pass `-NoElevate` to skip the UAC prompt entirely. The scripts then do
everything that does not need rights and report what they skipped.

## Changing it later

The URL is pasted by hand today. If that gets tedious, the natural next step is
to have **the POS computer** fetch it from Odoo automatically — an API key in
Windows Credential Manager, a call to read the POS config's display token, then
the same `POST /url` this already sends.

That change lives entirely in `refresh.py`. The display laptop does not change,
does not gain credentials, and does not need to be touched — which is the whole
reason the split is drawn where it is.
