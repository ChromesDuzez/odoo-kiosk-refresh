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
| `Stop Display Agent.cmd` | POS computer | Stops the display remotely; asks first |
| `Pair With Display.cmd` | POS computer | One-time setup wrapper |
| `Check Setup.cmd` | display laptop | Read-only: says why it isn't working |
| `Stop Agent.cmd` | display laptop | Stops the agent (it has no window to close) |
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
python refresh.py stop      stop it entirely; the screen goes blank
```

## Getting out of kiosk mode

Once the agent is running, the display is a full-screen Chrome window whose
watchdog puts it back a few seconds after anyone closes it — and that laptop's
keyboard is face-down and disabled. So closing Chrome over there achieves
nothing, and there is no practical way to reach a prompt on the machine itself.

To actually stop it, do it **from the POS computer**: double-click **Stop
Display Agent** (it asks you to type `YES` first, since the screen stays blank
afterwards), or run `python refresh.py stop`.

Chrome closes with the agent and stays closed, which gives you the desktop over
there to work on. To bring it back, on the display laptop run
`python display_agent.py run` — or just reboot it, since the startup entry
starts it again.

If you *are* at the display with a working keyboard, **Stop Agent.cmd** does the
same thing locally.

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

  `stop` is reachable from the POS computer rather than locked to the display
  itself. That is deliberate: a kiosk with a relaunching watchdog and no usable
  keyboard has no reachable local prompt, so a local-only stop would be one
  nobody could ever use. It still needs the pairing code, and the worst it can
  do — a blank screen until someone restarts it — is obvious and recoverable,
  unlike a repointed display.
- **A client lock.** Pairing records the address of the machine that paired,
  and from then on only that machine is accepted.

  **This repairs itself.** If the POS computer's address changes — a DHCP lease
  moving is the usual reason — the next command notices the refusal, re-claims
  the display and retries, printing one line to say so. Nothing to notice, no
  command to re-run. New addresses are added rather than swapped in, so two POS
  computers can share one display without taking it from each other, and the
  list is capped at 8 with the oldest dropped. Re-claiming needs the pairing
  code, so this is a convenience layer rather than a barrier against someone
  who already has the code. Stale entries are harmless: the list only filters
  who may *ask*, and every request still has to be correctly signed.

- **A host allowlist.** A new URL must parse to a hostname listed in
  `agent.config.json`. **This defaults to `["*"]`, which accepts any host** —
  the useful default, since the point of the tool is to put a page on a screen.

  Narrow it to `["yourcompany.odoo.com"]` if you want the stronger guarantee:
  with a specific host listed, even a leaked pairing code could only ever point
  the display at another page on your own Odoo server, never at an attacker's.
  The check compares the *parsed* hostname exactly, so
  `yourcompany.odoo.com.evil.net` is rejected. Worth doing on the real shop
  display; leave it open while you are playing with it.

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
  "shared_secret": "K7MQ3XRT9PBW",   // already filled in, leave it
  "url": "",                         // optional; you can send it after pairing
  "allowed_hosts": ["*"],            // any host by default; narrow it to lock down
  "allowed_clients": [],             // fills in on pairing; self-repairs on IP change
  "chrome_path": "auto",             // set a full path if Chrome is somewhere odd
  "restart_if_chrome_exits": true
}
```

It works as written — you do not have to change anything to get started.
`allowed_clients` fills itself in when you pair, and `chrome_path: "auto"` finds
Chrome in the usual places on Windows, Linux and macOS.

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

**Start here: double-click "Check Setup".** It is read-only — it starts nothing
and changes nothing — and it walks the whole chain in order: Python, the config,
Chrome, the startup shortcut, whether the agent is running, what holds the port,
the firewall rule, and the last dozen log lines. Anything it finds wrong comes
with the fix. Most of what follows is only needed if you want the detail.

**Nothing starts at logon.** Usually one of three things, all of which Check
Setup names: there is no startup shortcut (the installer never finished), Python
or `pythonw.exe` is not on PATH for that account, or **Chrome is not where the
agent expects it**. That last one is worth knowing about — the agent looks Chrome
up before it does anything else, so a Chrome installed somewhere non-standard
stops it dead. Set `"chrome_path"` in `agent.config.json` to the full path to
`chrome.exe`. The reason is now written to `agent.log` in every case.

**A PowerShell window flashed open and vanished.** That was the elevated window
(elevation always opens a second one) hitting an error. Every failure now pauses
so you can read it, and every install writes `install.log` next to the scripts
regardless. If you saw this on an older copy, the usual cause was a missing
`agent.config.json` after an uninstall with `-Purge` — the installer now just
creates one instead of failing.

**I can't find the agent to stop it.** It runs under `pythonw.exe`, which has no
window and no console, so closing the Chrome window does nothing to it — and a
few seconds later its watchdog puts Chrome straight back. Double-click **Stop
Agent** on the display, or run `python display_agent.py stop`. In Task Manager
it only appears under the **Details** tab, never under Processes. To find it by
hand from PowerShell:

```powershell
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
  Select-Object ProcessId, CommandLine | Format-List

Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*display_agent.py*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

**Starting the agent seems to do nothing.** Almost always a second copy is
already running and holding the port, so the new one exits immediately — and
with no console, invisibly. Stop the old one first as above. The log at
`agent.log` records the reason; look for `cannot listen on`. To see what holds
the port: `Get-NetTCPConnection -LocalPort 8765 -State Listen`.

**The display says "nothing to show yet".** It is paired and working, but no URL
has been set. Send one with **Change Display URL**. If that screen also warns
that `allowed_hosts` is empty, every URL will be refused until you set it — put
your Odoo host in it, or `["*"]` to allow anything, then restart the agent.

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
