<#
.SYNOPSIS
    Keep VMware Workstation's `vmrest` daemon up on a remote-agent host: start it at
    logon, with no console window, and start it again if it dies.

.DESCRIPTION
    The agent reaches Workstation through `vmrest`, and `vmrest` is an ordinary
    foreground console program. It dies when its window is closed, it dies when you log
    out, and a reboot does not bring it back — so the agent container comes up on its
    own after a Windows update and every Workstation job then fails with "could not
    reach vmrest", which reads like a network fault and is not one.

    The agent cannot fix this from its side. It is a Linux container in Docker Desktop's
    VM: it can dial `vmrest` over `host.docker.internal`, but it has no way to launch a
    Windows process on the host. Autostart belongs to the host, which is this script.

    It registers ONE scheduled task, running as the current user, with two triggers:

      * at logon — the same event that starts Docker Desktop and therefore the agent, so
        `vmrest` and the agent come back together;
      * every few minutes — the action exits immediately if `vmrest` is already running,
        so this is a cheap liveness check that also covers a mid-session crash.

    The task runs as *you*, interactively, on purpose. `vmrest -C` writes its credential
    under your profile (`%APPDATA%`) and Workstation's VM inventory is per-user, so a
    task running as SYSTEM would authenticate against a credential it cannot read and
    list an inventory that is not yours.

    Idempotent: re-run it to change the path, the arguments or the interval.

.PARAMETER VmrestPath
    Full path to `vmrest.exe`. Discovered from the Workstation install by default.

.PARAMETER Arguments
    Extra arguments for `vmrest`, passed verbatim — e.g. `-p 8698`, or `-c cert.pem -k
    key.pem` to serve HTTPS. Empty by default, which is 127.0.0.1:8697 over HTTP.

.PARAMETER CheckIntervalMinutes
    How often to re-check that `vmrest` is alive. Default 5.

.PARAMETER Port
    Port to probe with -Status. Default 8697. If you moved the port with `-p` in
    -Arguments, pass the same value here — this script does not parse your arguments.

.PARAMETER NoStart
    Register the task but do not run it now. Without this, `vmrest` starts immediately.

.PARAMETER Force
    Register even though no `vmrest` credential file was found. Only useful if your
    build keeps that credential somewhere this script does not look — read the
    credential note below before reaching for it.

.PARAMETER Status
    Report the task, the process and the port instead of changing anything.

.PARAMETER Unregister
    Remove the task and the generated launcher. A running `vmrest` is left alone.

.EXAMPLE
    .\scripts\Enable-VmrestAutostart.ps1

.EXAMPLE
    .\scripts\Enable-VmrestAutostart.ps1 -Arguments '-p','8698' -Port 8698

.EXAMPLE
    .\scripts\Enable-VmrestAutostart.ps1 -Status

.NOTES
    Registering a task in the root folder normally needs an elevated PowerShell. The
    task itself runs unelevated — `vmrest` does not need administrator.

    **A `vmrest` with no credential configured does not start and 401 — it exits.**
    Verified on vmrest 1.3.1 (Workstation 26.0): "To listen on TCP port, Please use -C
    to update credential / Not listening to either Unix Socket or TCP port, exiting",
    exit code 1. That is why this refuses to register without a credential file: an
    autostart for a daemon that cannot listen is a five-minute relaunch loop whose only
    symptom is a refused port, and there is no console left to read the reason from.
    The launcher writes that diagnosis to a log instead — see -Status.

    This keeps the process alive; it does not health-check the API. A `vmrest` that is
    running but wedged still answers `Get-Process`, so the task will not replace it. If
    the dashboard says "could not reach vmrest" while -Status shows the process up,
    restart it by hand (`Stop-Process -Name vmrest`) and the next check starts a fresh
    one within -CheckIntervalMinutes.

    See docs/integrations/vmware.md for the connection side of this.
#>
#Requires -Version 5.1
[CmdletBinding(DefaultParameterSetName = 'Register')]
param(
    [Parameter(ParameterSetName = 'Register')]
    [string]$VmrestPath,

    [Parameter(ParameterSetName = 'Register')]
    [string[]]$Arguments = @(),

    [Parameter(ParameterSetName = 'Register')]
    [ValidateRange(1, 1440)]
    [int]$CheckIntervalMinutes = 5,

    [Parameter(ParameterSetName = 'Register')]
    [switch]$NoStart,

    [Parameter(ParameterSetName = 'Register')]
    [switch]$Force,

    [Parameter(ParameterSetName = 'Status', Mandatory = $true)]
    [switch]$Status,

    [Parameter(ParameterSetName = 'Unregister', Mandatory = $true)]
    [switch]$Unregister,

    [string]$TaskName = 'VMware vmrest (dashboard agent)',

    [ValidateRange(1, 65535)]
    [int]$Port = 8697
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$LauncherDir = Join-Path $env:LOCALAPPDATA 'dashboard-agent'
$LauncherPath = Join-Path $LauncherDir 'Start-Vmrest.ps1'
$LogPath = Join-Path $LauncherDir 'vmrest-autostart.log'
$ProcessName = 'vmrest'


function Find-Vmrest {
    <#
      Registry first: it is where a non-default install directory is recorded, and both
      Program Files guesses miss that machine entirely.

      Both keys, and in this order, because the answer changed. Older Workstation was a
      32-bit install and registered under WOW6432Node; Workstation 26.0 installs
      64-bit — `C:\Program Files\VMware\VMware Workstation\` — with InstallPath under
      the plain key and no WOW6432Node key at all (verified on 26.0.0/vmrest 1.3.1). A
      script that knew only one of the two reports "not installed" on half the hosts.
    #>
    $candidates = New-Object System.Collections.Generic.List[string]
    foreach ($key in @('HKLM:\SOFTWARE\WOW6432Node\VMware, Inc.\VMware Workstation',
                       'HKLM:\SOFTWARE\VMware, Inc.\VMware Workstation')) {
        try {
            $installPath = (Get-ItemProperty -Path $key -Name 'InstallPath' -ErrorAction Stop).InstallPath
        } catch {
            continue
        }
        if ($installPath) { $candidates.Add((Join-Path $installPath 'vmrest.exe')) }
    }
    $candidates.Add('C:\Program Files (x86)\VMware\VMware Workstation\vmrest.exe')
    $candidates.Add('C:\Program Files\VMware\VMware Workstation\vmrest.exe')

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    return $null
}


function Get-CredentialFileHint {
    <#
      `vmrest -C` stores its API credential under the user profile, and vmrest will not
      listen on TCP at all without it — it prints "Please use -C to update credential"
      and exits 1. So this is a prerequisite, not a nicety: without it the autostart
      has nothing to keep alive.
    #>
    foreach ($path in @((Join-Path $env:APPDATA 'vmrest.cfg'),
                        (Join-Path $env:USERPROFILE 'vmrest.cfg'))) {
        if (Test-Path -LiteralPath $path -PathType Leaf) { return $path }
    }
    return $null
}


function Write-Launcher {
    <#
      A generated launcher rather than an inline -Command, for two reasons: a task
      argument string carrying nested quotes for a path *and* a script block is the
      classic place a paste comes apart silently, and a file on disk is something the
      operator can read to see exactly what runs as them at logon.
    #>
    param([string]$Exe, [string[]]$ExeArguments)

    $literal = { param($s) "'" + ($s -replace "'", "''") + "'" }
    $argList = ''
    if ($ExeArguments -and $ExeArguments.Count -gt 0) {
        $argList = (($ExeArguments | ForEach-Object { & $literal $_ }) -join ', ')
    }

    $lines = @(
        '# Generated by scripts/Enable-VmrestAutostart.ps1. Re-run that script to change'
        '# this file; edits here are overwritten.'
        '#'
        '# Started by the scheduled task, hidden, as the logged-on user.'
        ('$exe = ' + (& $literal $Exe))
        ('$vmrestArgs = @(' + $argList + ')')
        ('$log = ' + (& $literal $LogPath))
        ''
        '# This runs with no console attached, so anything worth knowing has to be written'
        '# down. Kept small: a line per event, trimmed when it passes 200 KB.'
        'function Write-Log {'
        '    param([string]$Message)'
        '    try {'
        '        $item = Get-Item -LiteralPath $log -ErrorAction SilentlyContinue'
        '        if ($item -and $item.Length -gt 200KB) {'
        '            Set-Content -LiteralPath $log -Value (Get-Content -LiteralPath $log -Tail 200)'
        '        }'
        '        Add-Content -LiteralPath $log -Value ("{0:s}  {1}" -f (Get-Date), $Message)'
        '    } catch { }'
        '}'
        ''
        '# Already up: this is the every-few-minutes trigger finding nothing to do. Never'
        '# start a second vmrest — the port is taken and the second one exits confusingly.'
        ("if (Get-Process -Name '" + $ProcessName + "' -ErrorAction SilentlyContinue) { exit 0 }")
        ''
        'if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {'
        '    Write-Log "vmrest is not at $exe — re-run scripts/Enable-VmrestAutostart.ps1"'
        '    exit 1'
        '}'
        ''
        '# -WindowStyle Hidden is the whole point: a visible console is a window someone'
        '# closes, which is how vmrest goes away mid-session. It also rules out capturing'
        '# vmrest''s own output — a redirect forces a real console window back on screen —'
        '# so the exit code below is the whole diagnosis, and it is enough.'
        'if ($vmrestArgs.Count -gt 0) {'
        '    $proc = Start-Process -FilePath $exe -ArgumentList $vmrestArgs -WindowStyle Hidden -PassThru'
        '} else {'
        '    $proc = Start-Process -FilePath $exe -WindowStyle Hidden -PassThru'
        '}'
        ''
        'Start-Sleep -Seconds 2'
        'if ($proc -and $proc.HasExited) {'
        '    Write-Log ("vmrest exited immediately, code {0}. If that code is 1, the cause is" -f $proc.ExitCode)'
        # Single-quoted in the generated file on purpose: `v inside a double-quoted
        # PowerShell string is a vertical tab, so "run `vmrest -C`" would log garbage.
        '    Write-Log ''  almost certainly no configured credential: run "vmrest -C" once,'''
        '    Write-Log ''  in a normal PowerShell window, as this user. It will not listen without one.'''
        '    exit 1'
        '}'
        'Write-Log ("started vmrest, pid {0}" -f $proc.Id)'
    )

    if (-not (Test-Path -LiteralPath $LauncherDir -PathType Container)) {
        New-Item -ItemType Directory -Path $LauncherDir -Force | Out-Null
    }
    # UTF-8 with a BOM is fine for powershell.exe -File; it is only the agent's own
    # config files that must be BOM-less.
    Set-Content -LiteralPath $LauncherPath -Value $lines -Encoding UTF8
    return $LauncherPath
}


function New-RepeatTrigger {
    param([int]$Minutes)

    $interval = New-TimeSpan -Minutes $Minutes
    $start = (Get-Date).AddMinutes(1)
    try {
        # MaxValue is how "repeat indefinitely" is expressed here. It is accepted on
        # Windows 10/11 and Server 2016+; the fallback covers older builds that reject
        # it, where a decade is indistinguishable from forever.
        return New-ScheduledTaskTrigger -Once -At $start `
            -RepetitionInterval $interval -RepetitionDuration ([TimeSpan]::MaxValue)
    } catch {
        return New-ScheduledTaskTrigger -Once -At $start `
            -RepetitionInterval $interval -RepetitionDuration (New-TimeSpan -Days 3650)
    }
}


function Test-VmrestPort {
    param([int]$TcpPort)

    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $connect = $client.BeginConnect('127.0.0.1', $TcpPort, $null, $null)
        if (-not $connect.AsyncWaitHandle.WaitOne(2000)) { return $false }
        $client.EndConnect($connect)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}


function Show-Status {
    Write-Host ''
    Write-Host "Task      : $TaskName"
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        Write-Host ("            registered, state $($task.State)")
        Write-Host ("            last run $($info.LastRunTime), result 0x{0:X}" -f $info.LastTaskResult)
        Write-Host ("            next run $($info.NextRunTime)")
    } else {
        Write-Host '            NOT registered — run this script with no arguments' -ForegroundColor Yellow
    }

    Write-Host "Launcher  : $LauncherPath"
    if (-not (Test-Path -LiteralPath $LauncherPath -PathType Leaf)) {
        Write-Host '            missing' -ForegroundColor Yellow
    }

    $process = Get-Process -Name $ProcessName -ErrorAction SilentlyContinue
    if ($process) {
        Write-Host ("Process   : running, pid $(($process | ForEach-Object { $_.Id }) -join ', ')")
    } else {
        Write-Host 'Process   : not running' -ForegroundColor Yellow
    }

    if (Test-VmrestPort -TcpPort $Port) {
        Write-Host "Port      : 127.0.0.1:$Port accepting connections"
    } else {
        Write-Host "Port      : 127.0.0.1:$Port refused" -ForegroundColor Yellow
    }

    $cfg = Get-CredentialFileHint
    if ($cfg) {
        Write-Host "Credential: $cfg"
    } else {
        Write-Host 'Credential: none found — run "vmrest -C" once, or vmrest will not listen' -ForegroundColor Yellow
    }

    if (Test-Path -LiteralPath $LogPath -PathType Leaf) {
        Write-Host ''
        Write-Host "Last lines of $LogPath"
        Get-Content -LiteralPath $LogPath -Tail 6 | ForEach-Object { Write-Host "  $_" }
    }

    Write-Host ''
    Write-Host 'A 401 from an unauthenticated curl is a HEALTHY vmrest: it answered.'
    Write-Host "The agent dials this from its container as host.docker.internal:$Port"
    Write-Host ''
}


function Remove-Autostart {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "No scheduled task '$TaskName' to remove."
    }
    if (Test-Path -LiteralPath $LauncherPath -PathType Leaf) {
        Remove-Item -LiteralPath $LauncherPath -Force
        Write-Host "Removed $LauncherPath."
    }
    if (Get-Process -Name $ProcessName -ErrorAction SilentlyContinue) {
        Write-Host 'vmrest is still running — this only removed the autostart.'
        Write-Host 'Stop it with: Stop-Process -Name vmrest'
    }
}


function Register-Autostart {
    $exe = $VmrestPath
    if (-not $exe) { $exe = Find-Vmrest }
    if (-not $exe) {
        throw ("Could not find vmrest.exe. Workstation Pro ships it; Workstation Player " +
               "does not. Pass -VmrestPath if it is installed somewhere unusual.")
    }
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
        throw "No such file: $exe"
    }

    # Refuse rather than register an autostart for a daemon that cannot listen. See
    # .NOTES: no credential is not a 401, it is an immediate exit, and the resulting
    # relaunch-every-5-minutes loop looks exactly like a network problem.
    if (-not (Get-CredentialFileHint) -and -not $Force) {
        throw ("No vmrest credential file found (looked for vmrest.cfg under " +
               "$env:APPDATA and $env:USERPROFILE). Run `"vmrest -C`" once as this user, " +
               "then re-run this script. vmrest refuses to listen on TCP without it — it " +
               "exits instead, so there would be nothing here to keep alive. Pass -Force " +
               "if your build stores that credential somewhere this does not look.")
    }

    $launcher = Write-Launcher -Exe $exe -ExeArguments $Arguments
    $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

    $action = New-ScheduledTaskAction -Execute $powershell `
        -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden ' +
                   '-ExecutionPolicy Bypass -File "' + $launcher + '"') `
        -WorkingDirectory (Split-Path -Parent $exe)

    $triggers = @((New-ScheduledTaskTrigger -AtLogOn -User $me),
                  (New-RepeatTrigger -Minutes $CheckIntervalMinutes))

    # Interactive, unelevated, as this user: see the .DESCRIPTION note on %APPDATA%.
    $principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited

    # IgnoreNew so a slow check cannot stack; ExecutionTimeLimit 0 so nothing kills it.
    # StartWhenAvailable covers a missed run while the machine was asleep.
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero)

    try {
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
            -Principal $principal -Settings $settings -Force `
            -Description ('Starts VMware vmrest for the dashboard remote agent, and ' +
                          'restarts it if it stops. Managed by ' +
                          'scripts/Enable-VmrestAutostart.ps1.') | Out-Null
    } catch {
        # Not a typed catch: the ScheduledTasks cmdlets are CIM wrappers, so an
        # access-denied surfaces as a CimException and never as
        # UnauthorizedAccessException. Match the message, and rethrow anything else
        # rather than blaming elevation for an unrelated failure.
        if ($_.Exception.Message -match 'denied|Access is denied|0x80070005') {
            throw ("Access denied registering the task. Re-run this in an elevated " +
                   "PowerShell (Run as administrator) — the task itself still runs " +
                   "unelevated, as you. Original error: " + $_.Exception.Message)
        }
        throw
    }

    Write-Host ''
    Write-Host "Registered '$TaskName'."
    Write-Host "  vmrest    : $exe"
    if ($Arguments -and $Arguments.Count -gt 0) {
        Write-Host ("  arguments : " + ($Arguments -join ' '))
    }
    Write-Host "  launcher  : $launcher"
    Write-Host "  triggers  : at logon, then every $CheckIntervalMinutes minute(s) while logged on"
    Write-Host "  runs as   : $me (interactive, unelevated)"

    if ($NoStart) {
        Write-Host ''
        Write-Host 'Not started (-NoStart). It will start at your next logon.'
        return
    }

    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 5
    if (Get-Process -Name $ProcessName -ErrorAction SilentlyContinue) {
        Write-Host ''
        Write-Host 'vmrest is running.' -ForegroundColor Green
    } else {
        Write-Host ''
        Write-Warning ('vmrest is not running. The launcher logs why to ' + $LogPath +
                       ' — an immediate exit with code 1 means the credential, not the ' +
                       'task. Task Scheduler''s History tab covers the case where the ' +
                       'launcher itself never ran.')
    }
}


switch ($PSCmdlet.ParameterSetName) {
    'Status'     { Show-Status }
    'Unregister' { Remove-Autostart }
    default      { Register-Autostart; Show-Status }
}
