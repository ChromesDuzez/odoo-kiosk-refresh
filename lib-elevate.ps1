<#
.SYNOPSIS
    Self-elevation helper, dot-sourced by install-agent.ps1 and uninstall.ps1.

.DESCRIPTION
    Relaunches the calling script through UAC when it is not already running
    with Administrator rights, forwarding its original parameters.

    The relaunch uses `powershell.exe -ExecutionPolicy Bypass`, which sets the
    policy for that one process only. Nothing is written to the registry and
    the machine's policy is untouched -- it is the launch-argument equivalent
    of `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`.

    One subtlety this exists to handle: the Startup folder is per-user. If the
    signed-in account is a standard user, UAC asks for a *different* admin
    account's credentials and the elevated process runs as that account, whose
    Startup folder is somewhere else entirely. A shortcut written there would
    never run for the kiosk user. So the caller resolves the Startup path
    *before* elevating and forwards it, and the elevated instance uses the
    value it was handed rather than looking it up again.
#>

function Test-Administrator {
    <# True when the current process holds the Administrators role. #>
    return ([Security.Principal.WindowsPrincipal] `
            [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function ConvertTo-ArgumentList {
    <#
    .SYNOPSIS
        Turn a $PSBoundParameters hashtable back into command-line arguments.
    .DESCRIPTION
        Values are quoted here rather than left to Start-Process, which joins
        an -ArgumentList array with plain spaces and does no quoting of its
        own -- so an unquoted path containing a space would arrive split in
        two.
    #>
    param([hashtable]$BoundParameters = @{})

    $out = @()
    foreach ($key in $BoundParameters.Keys) {
        $value = $BoundParameters[$key]

        if ($value -is [System.Management.Automation.SwitchParameter]) {
            if ($value.IsPresent) { $out += "-$key" }
        }
        elseif ($value -is [bool]) {
            $out += "-$key"
            $out += (&{ if ($value) { '$true' } else { '$false' } })
        }
        elseif ($null -ne $value -and "$value" -ne '') {
            $text = "$value" -replace '"', '\"'

            # A value ending in a backslash -- any directory path might -- would
            # otherwise escape the closing quote and swallow the rest of the
            # command line. Windows resolves this by doubling the trailing run.
            if ($text -match '(\\+)$') { $text += $Matches[1] }

            $out += "-$key"
            $out += '"{0}"' -f $text
        }
    }
    return $out
}

function Assert-Elevated {
    <#
    .SYNOPSIS
        Return if already elevated; otherwise relaunch elevated and exit.
    .PARAMETER ScriptPath
        Full path of the calling script, i.e. $MyInvocation.MyCommand.Path.
    .PARAMETER BoundParameters
        The caller's $PSBoundParameters, forwarded to the elevated instance.
    .PARAMETER AlreadyElevated
        The caller's -Elevated switch. Stops an endless UAC loop if elevation
        somehow does not produce Administrator rights.
    .PARAMETER NoElevate
        Skip elevation and let the caller carry on with what rights it has.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$ScriptPath,
        [hashtable]$BoundParameters = @{},
        [switch]$AlreadyElevated,
        [switch]$NoElevate
    )

    if (Test-Administrator) { return }

    if ($NoElevate) {
        Write-Host "Running without Administrator rights (-NoElevate)." -ForegroundColor Yellow
        Write-Host "Anything needing them will be reported and skipped.`n" -ForegroundColor Yellow
        return
    }

    if ($AlreadyElevated) {
        # Relaunched once already and still not admin. Elevating again would
        # just prompt forever, so carry on and let the caller report what it
        # cannot do.
        Write-Host "Elevation did not take effect; continuing without it.`n" -ForegroundColor Yellow
        return
    }

    $workingDirectory = Split-Path -Parent $ScriptPath

    # -NoProfile keeps a user's profile script from interfering, and matters
    # more than usual here because this process runs as Administrator.
    $psArgs = @(
        '-NoProfile'
        '-ExecutionPolicy', 'Bypass'
        '-File', ('"{0}"' -f $ScriptPath)
    )
    $psArgs += ConvertTo-ArgumentList -BoundParameters $BoundParameters
    $psArgs += '-Elevated'

    Write-Host "Asking for Administrator rights..." -ForegroundColor Cyan
    Write-Host "(a UAC prompt will appear; the rest happens in a new window)`n" -ForegroundColor DarkGray

    try {
        Start-Process -FilePath 'powershell.exe' `
            -Verb RunAs `
            -ArgumentList $psArgs `
            -WorkingDirectory $workingDirectory `
            -ErrorAction Stop
    }
    catch {
        Write-Host "Elevation was declined or failed." -ForegroundColor Red
        Write-Host "  $($_.Exception.Message)`n" -ForegroundColor DarkGray
        Write-Host "Either accept the UAC prompt, or re-run with -NoElevate to" -ForegroundColor Yellow
        Write-Host "continue without Administrator rights." -ForegroundColor Yellow
        exit 1
    }

    # The elevated instance owns the job from here.
    exit 0
}

function Wait-BeforeClosing {
    <#
    .SYNOPSIS
        Hold an elevated window open so its output can be read.
    .DESCRIPTION
        Elevation opens a fresh window that would otherwise vanish the moment
        the script finishes, taking every message with it.
    #>
    param([switch]$WhenElevated)

    if (-not $WhenElevated) { return }
    Write-Host ''
    Read-Host 'Press Enter to close this window' | Out-Null
}
