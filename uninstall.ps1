<#
.SYNOPSIS
    Removes the customer display refresh tool from this machine.

.DESCRIPTION
    Safe to run on either machine, and safe to run twice -- it reports what it
    finds and skips what is not there. It works out which half is installed
    from the config files present, so you do not have to tell it.

    By default it undoes the install but keeps your settings:

      * stops the running agent
      * closes the kiosk Chrome window it opened
      * removes the Startup shortcut
      * removes the firewall rule (needs Administrator)

    Config files survive, so re-running install-agent.ps1 later brings
    everything back with the same pairing code and no need to re-pair.

    Add -Purge to delete the settings too. That destroys the pairing code, so
    the two machines would have to be paired again from scratch.

    Nothing here touches Odoo, and nothing touches any Chrome window other than
    the kiosk one this tool launched -- see the note on the profile filter below.

.PARAMETER Purge
    Also delete agent.config.json, refresh.config.json, agent.log, pairing.html
    and the chrome-profile folder. Prompts first unless -Force.

.PARAMETER Force
    Skip the -Purge confirmation prompt.

.PARAMETER NoElevate
    Do not ask for Administrator rights. The firewall rule is then left in
    place and reported, and an agent running as another user cannot be stopped.

.NOTES
    Easiest way to run this is to double-click "Uninstall.cmd", which launches
    PowerShell with -ExecutionPolicy Bypass for that process only.

.EXAMPLE
    .\uninstall.ps1
    .\uninstall.ps1 -WhatIf          # show what would happen, change nothing
    .\uninstall.ps1 -Purge           # also delete settings and the pairing code
    .\uninstall.ps1 -NoElevate       # no UAC prompt; skips what needs rights
#>

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [switch]$Purge,
    [switch]$Force,
    [switch]$NoElevate,

    # Set automatically on the elevated relaunch; see install-agent.ps1 and
    # lib-elevate.ps1 for why the Startup path has to be carried across.
    # Do not pass these by hand.
    [switch]$Elevated,
    [string]$StartupDir
)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $Root 'lib-elevate.ps1')

# Resolved before elevating, so the shortcut removed is the signed-in user's
# and not that of whichever admin account approved the UAC prompt.
if (-not $StartupDir) { $StartupDir = [Environment]::GetFolderPath('Startup') }

$forward = @{} + $PSBoundParameters
$forward.Remove('Elevated') | Out-Null
$forward['StartupDir'] = $StartupDir

Assert-Elevated -ScriptPath $MyInvocation.MyCommand.Path `
    -BoundParameters $forward `
    -AlreadyElevated:$Elevated `
    -NoElevate:$NoElevate

$Agent = Join-Path $Root 'display_agent.py'
$AgentConfig = Join-Path $Root 'agent.config.json'
$ClientConfig = Join-Path $Root 'refresh.config.json'
$ShortcutPath = Join-Path $StartupDir 'Odoo Customer Display Agent.lnk'
$FirewallRuleName = 'Odoo Customer Display Agent'

$IsAdmin = Test-Administrator

$done = [System.Collections.Generic.List[string]]::new()
$skipped = [System.Collections.Generic.List[string]]::new()

# Under -WhatIf nothing is actually removed, so these report the intent rather
# than claiming a change that did not happen.
function Write-Removed($text) {
    if ($WhatIfPreference) {
        Write-Host "  would remove  $text" -ForegroundColor Yellow
    }
    else {
        $script:done.Add($text)
        Write-Host "  removed  $text" -ForegroundColor Green
    }
}

function Write-Absent($text) {
    $script:skipped.Add($text)
    Write-Host "  absent   $text" -ForegroundColor DarkGray
}

Write-Host "`nUninstalling from: $Root" -ForegroundColor Cyan
Write-Host ("-" * 60)

# Which half is installed here? Used only to tailor the closing message.
$hasAgent = Test-Path $AgentConfig
$hasClient = Test-Path $ClientConfig

# Read the Chrome profile path from the config while it still exists, since
# -Purge may delete it below.
$ProfileDir = Join-Path $Root 'chrome-profile'
if ($hasAgent) {
    try {
        $parsed = Get-Content $AgentConfig -Raw | ConvertFrom-Json
        if ($parsed.user_data_dir) { $ProfileDir = $parsed.user_data_dir }
    }
    catch {
        Write-Host "  note     agent.config.json is unreadable; assuming the default profile path" -ForegroundColor Yellow
    }
}

# ------------------------------------------------------------ startup entry

# Removed first so that if anything below fails, the agent at least does not
# come back at the next logon.
if (Test-Path $ShortcutPath) {
    if ($PSCmdlet.ShouldProcess($ShortcutPath, 'Remove startup shortcut')) {
        Remove-Item $ShortcutPath -Force
    }
    Write-Removed 'Startup shortcut'
}
else {
    Write-Absent 'Startup shortcut'
}

# ------------------------------------------------------------ agent process

# Stop the agent BEFORE Chrome, for two reasons: its watchdog relaunches Chrome
# within a few seconds of seeing it gone, so the other order just makes the
# agent put the window straight back; and Chrome is a child process of the
# agent, so killing the agent's tree usually takes the kiosk window with it.
# The Chrome sweep below is therefore normally a no-op -- it exists to catch a
# window that outlived its parent, say because the agent crashed earlier.
$agentProcs = @(
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*display_agent.py*" }
)

# Only ours: a second copy of this tool installed in another folder is none of
# our business. A process started as `python display_agent.py run` from inside
# the folder has no path on its command line and so cannot be matched either --
# in that case it is reported rather than killed, because guessing wrong here
# means killing something that is not ours.
$ours = @($agentProcs | Where-Object { $_.CommandLine -like "*$Root*" })
if ($agentProcs.Count -gt 0 -and $ours.Count -eq 0) {
    Write-Host "  note     display_agent.py is running, but not from this folder" -ForegroundColor Yellow
    Write-Host "           (or it was started with a relative path). Left alone:" -ForegroundColor Yellow
    foreach ($p in $agentProcs) { Write-Host "             pid $($p.ProcessId): $($p.CommandLine)" -ForegroundColor DarkGray }
    Write-Host "           Stop it from Task Manager if it is in fact this one." -ForegroundColor Yellow
}

if ($ours.Count -gt 0) {
    foreach ($proc in $ours) {
        if ($PSCmdlet.ShouldProcess("pid $($proc.ProcessId)", 'Stop display agent')) {
            & taskkill.exe /PID $proc.ProcessId /T /F 2>&1 | Out-Null
        }
    }
    Write-Removed "running agent ($($ours.Count) process(es))"
    if (-not $WhatIfPreference) { Start-Sleep -Milliseconds 500 }
}
else {
    Write-Absent 'running agent'
}

# ------------------------------------------------------------ kiosk browser

# The filter on --user-data-dir is what makes this safe. The kiosk runs in a
# dedicated profile, so matching on it cannot catch the ordinary Chrome windows
# somebody has open -- which on the POS computer is very much not something to
# close out from under them.
# Belt and braces: an empty $ProfileDir would turn the filter below into "*",
# matching every Chrome on the machine. It cannot be empty by construction, but
# the cost of being wrong is closing someone's browser mid-transaction, so the
# sweep is skipped outright rather than trusted.
$chromeProcs = @()
if ([string]::IsNullOrWhiteSpace($ProfileDir)) {
    Write-Host "  note     no kiosk profile path known; skipping the Chrome sweep" -ForegroundColor Yellow
}
else {
    $chromeProcs = @(
        Get-CimInstance Win32_Process -Filter "Name = 'chrome.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine -like "*$ProfileDir*" }
    )
}

if ($chromeProcs.Count -gt 0) {
    if ($PSCmdlet.ShouldProcess("$($chromeProcs.Count) Chrome process(es)", 'Close the kiosk window')) {
        foreach ($proc in $chromeProcs) {
            # Children die with the parent, so some of these PIDs are already
            # gone by the time we reach them. That is expected, not an error.
            & taskkill.exe /PID $proc.ProcessId /T /F 2>&1 | Out-Null
        }
    }
    Write-Removed "kiosk Chrome ($($chromeProcs.Count) process(es))"
}
else {
    Write-Absent 'kiosk Chrome'
}

# --------------------------------------------------------------- firewall

$rule = $null
if ($IsAdmin) {
    $rule = Get-NetFirewallRule -DisplayName $FirewallRuleName -ErrorAction SilentlyContinue
}

if ($rule) {
    if ($PSCmdlet.ShouldProcess($FirewallRuleName, 'Remove firewall rule')) {
        $rule | Remove-NetFirewallRule
    }
    Write-Removed 'firewall rule'
}
elseif (-not $IsAdmin) {
    # Reached only under -NoElevate, or if elevation was declined. Without
    # rights we cannot even see whether the rule exists, so it is reported as
    # unknown rather than claimed absent.
    Write-Host "  unknown  firewall rule (re-run without -NoElevate to remove it)" -ForegroundColor Yellow
    $skipped.Add('firewall rule (not checked)')
}
else {
    Write-Absent 'firewall rule'
}

# ------------------------------------------------------------------ purge

if ($Purge) {
    Write-Host ""
    # The @() around the whole pipeline matters: filtering down to exactly one
    # hashtable would otherwise leave $targets as a bare Hashtable, whose .Count
    # is its number of keys rather than the number of items.
    $targets = @(
        @(
            @{ Path = $AgentConfig;                     Label = 'agent.config.json (holds the pairing code)' }
            @{ Path = $ClientConfig;                    Label = 'refresh.config.json (holds the pairing code)' }
            @{ Path = (Join-Path $Root 'agent.log');    Label = 'agent.log' }
            @{ Path = (Join-Path $Root 'pairing.html'); Label = 'pairing.html' }
            @{ Path = $ProfileDir;                      Label = 'chrome-profile folder' }
            @{ Path = (Join-Path $Root '__pycache__');  Label = '__pycache__' }
        ) | Where-Object { Test-Path $_.Path }
    )

    if ($targets.Count -eq 0) {
        Write-Host "Nothing left to purge." -ForegroundColor DarkGray
    }
    else {
        Write-Host "About to delete:" -ForegroundColor Yellow
        foreach ($t in $targets) { Write-Host "  $($t.Label)" }

        $go = $Force
        if (-not $Force -and -not $WhatIfPreference) {
            Write-Host "`nThis destroys the pairing code. The two machines would have to be" -ForegroundColor Yellow
            Write-Host "paired again from scratch." -ForegroundColor Yellow
            $answer = Read-Host "`nType YES to continue"
            $go = ($answer -ceq 'YES')
            if (-not $go) { Write-Host "Left the settings alone." -ForegroundColor Cyan }
        }

        if ($go -or $WhatIfPreference) {
            Write-Host ""
            foreach ($t in $targets) {
                if ($PSCmdlet.ShouldProcess($t.Path, 'Delete')) {
                    Remove-Item $t.Path -Recurse -Force -ErrorAction SilentlyContinue
                }
                Write-Removed $t.Label
            }
        }
    }
}

# ----------------------------------------------------------------- summary

Write-Host "`n$('-' * 60)"
if ($WhatIfPreference) {
    Write-Host "-WhatIf: nothing was actually changed." -ForegroundColor Cyan
}
elseif ($done.Count -eq 0) {
    Write-Host "Nothing to remove -- this machine was already clean." -ForegroundColor Cyan
}
else {
    Write-Host "Removed $($done.Count) item(s)." -ForegroundColor Green
}

# Parenthesised deliberately: -and binds tighter than -or, so without the outer
# grouping this would announce "settings were kept" straight after a purge that
# deleted them, whenever refresh.config.json happened to be the survivor.
$settingsRemain = (Test-Path $AgentConfig) -or (Test-Path $ClientConfig)
if ($settingsRemain) {
    Write-Host "`nSettings were kept. Re-run install-agent.ps1 to bring it back with the" -ForegroundColor Cyan
    Write-Host "same pairing code, or re-run this with -Purge to delete them." -ForegroundColor Cyan
}

if ($hasClient -and -not $hasAgent) {
    Write-Host "`nThis is the POS computer -- there was no service to stop here, only" -ForegroundColor DarkGray
    Write-Host "settings. Delete the folder whenever you like." -ForegroundColor DarkGray
}

Write-Host "`nThe scripts themselves are still here. Delete the folder to finish:" -ForegroundColor DarkGray
Write-Host "  $Root`n" -ForegroundColor DarkGray

Wait-BeforeClosing -WhenElevated:$Elevated
