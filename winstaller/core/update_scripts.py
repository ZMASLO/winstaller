"""PowerShell scripts used by :mod:`winstaller.core.updates` (Windows Update, Store).

Kept as Python constants on purpose (not ``.ps1`` files): they end up inside the
exe's PYZ archive and are passed to ``powershell.exe -EncodedCommand``, so no script
file ever exists on disk where a non-elevated process could swap it (the onefile
``_MEIPASS`` directory is writable by the user). Nothing is interpolated into the
scripts: every parameter comes from environment variables of the child process
(``WINSTALLER_*``), which the scripts validate themselves.

Each script is self-contained (sets UTF-8 output itself) and prints, where it reports
back, exactly one JSON line with a ``"winstaller"`` marker field.
"""

# PowerShell source lines are kept readable rather than wrapped at 100 columns.
# ruff: noqa: E501

from __future__ import annotations

#: Documented WUA syntax (OR only at the top level, AND binds tighter); returns both
#: ``Installation`` (regular) and ``OptionalInstallation`` (Preview LCU/.NET, optional
#: drivers). Without a DeploymentAction term WUA implies 'Installation' only.
WU_CRITERIA = (
    "IsInstalled=0 and IsHidden=0 and DeploymentAction='Installation' or "
    "IsInstalled=0 and IsHidden=0 and DeploymentAction='OptionalInstallation'"
)
#: Undocumented equivalent, used only when WUA rejects the primary criteria.
WU_FALLBACK_CRITERIA = "IsInstalled=0 and IsHidden=0 and DeploymentAction=*"

# Shared helpers (PowerShell). COM errors arrive wrapped (MethodInvocationException),
# so the HRESULT is taken from the first COMException in the chain.
_PS_COMMON = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
function Get-HResult([System.Exception]$Err) {
    $x = $Err
    while ($null -ne $x) {
        if ($x -is [System.Runtime.InteropServices.COMException]) { return ('0x{0:X8}' -f $x.HResult) }
        $x = $x.InnerException
    }
    return ('0x{0:X8}' -f $Err.HResult)
}
# One line of plain text: control characters, U+0085/U+2028/U+2029 (raw in PS 5.1
# ConvertTo-Json) and bidi/zero-width characters become spaces.
function Get-CleanText($Value) {
    return (([string]$Value) -replace '[\x00-\x1f\x7f-\x9f\u2028\u2029\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]', ' ').Trim()
}
"""

_PS_CRITERIA = f"""
$Criteria = "{WU_CRITERIA}"
$FallbackCriteria = '{WU_FALLBACK_CRITERIA}'
"""

# ------------------------------------------------------------------ scan (read-only)

#: Read-only Windows Update search. Works without elevation. Exit 0 = scan done,
#: 1 = scan failed (``error.hresult``/``error.message`` in the JSON line).
WU_SCAN_SCRIPT = (
    _PS_COMMON
    + _PS_CRITERIA
    + r"""
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$out = [ordered]@{
    winstaller = 'wu-scan'; ok = $false; criteria = $Criteria; seconds = 0; resultCode = $null
    rebootPending = $false; isAdmin = $false; updates = @(); error = $null
}
try {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    $out.isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    try { $out.rebootPending = [bool](New-Object -ComObject Microsoft.Update.SystemInfo).RebootRequired } catch { }

    $session = New-Object -ComObject Microsoft.Update.Session
    $session.ClientApplicationID = 'Winstaller'
    $searcher = $session.CreateUpdateSearcher()
    $searcher.Online = $true
    $sw = [Diagnostics.Stopwatch]::StartNew()
    try {
        $result = $searcher.Search($Criteria)
    } catch {
        $hr = Get-HResult $_.Exception
        # WU_E_INVALID_CRITERIA / E_INVALIDARG: an older agent rejects the criteria.
        if ($hr -ne '0x80240032' -and $hr -ne '0x80070057') { throw }
        $out.criteria = $FallbackCriteria
        $result = $searcher.Search($FallbackCriteria)
    }
    $sw.Stop()
    $out.seconds = [math]::Round($sw.Elapsed.TotalSeconds, 1)
    $out.resultCode = [int]$result.ResultCode
    # orcFailed (4) / orcAborted (5): the list is not trustworthy, report a failure
    # (3 = SucceededWithErrors is passed on; Winstaller warns about it).
    if ($out.resultCode -ge 4) {
        throw (New-Object System.Exception ('Wyszukiwanie zakończone kodem wyniku {0}.' -f $out.resultCode))
    }

    $seen = @{}
    $list = New-Object System.Collections.ArrayList
    foreach ($u in $result.Updates) {
        $id = ([string]$u.Identity.UpdateID).ToLowerInvariant()
        $rev = [int]$u.Identity.RevisionNumber
        $key = '{0}/{1}' -f $id, $rev
        if ($seen.ContainsKey($key)) { continue }
        $seen[$key] = $true
        $isDriver = ([int]$u.Type -eq 2)
        $entry = [ordered]@{ id = $id; revision = $rev; title = (Get-CleanText $u.Title) }
        try {
            $entry.kb = @($u.KBArticleIDs | ForEach-Object { 'KB' + [string]$_ })
            $entry.type = $(if ($isDriver) { 'driver' } else { 'software' })
            $entry.optional = ([int]$u.DeploymentAction -eq 4)
            $entry.browseOnly = [bool]$u.BrowseOnly
            $entry.downloaded = [bool]$u.IsDownloaded
            $entry.categories = @($u.Categories | ForEach-Object { [string]$_.Name })
            $entry.rebootBehavior = [int]$u.InstallationBehavior.RebootBehavior
            $entry.canRequestUserInput = [bool]$u.InstallationBehavior.CanRequestUserInput
            if ($isDriver) {
                $entry.driverClass = Get-CleanText $u.DriverClass
                $entry.driverModel = Get-CleanText $u.DriverModel
                $entry.driverVendor = Get-CleanText $u.DriverManufacturer
            }
        } catch {
            $entry.type = $(if ($isDriver) { 'driver' } else { 'software' })
        }
        [void]$list.Add($entry)
    }
    $out.updates = $list.ToArray()
    $out.ok = $true
} catch {
    $out.error = [ordered]@{ hresult = (Get-HResult $_.Exception); message = [string]$_.Exception.Message }
}
[Console]::Out.WriteLine(($out | ConvertTo-Json -Depth 6 -Compress))
if ($out.ok) { exit 0 } else { exit 1 }
"""
)

# ------------------------------------------------------ background download + install

#: Detached, hidden: downloads and installs Windows updates, never restarts.
#: Environment: WINSTALLER_WU_LOG, WINSTALLER_WU_STATUS (absolute paths, required),
#: WINSTALLER_WU_IDS (comma separated UpdateIDs), WINSTALLER_WU_ALL ("1" = everything
#: found; an empty list without it is a parameter error),
#: WINSTALLER_WU_SKIP_DRIVERS / WINSTALLER_WU_SKIP_DISPLAY_DRIVERS /
#: WINSTALLER_WU_SKIP_INTERACTIVE ("1" = skip; all off by default).
#: Exit codes: 0 ok / nothing to do, 1 unexpected error, 2 some updates failed,
#: 3 another instance running, 4 not elevated, 5 restart required before installing,
#: 6 Windows Update busy, 7 bad parameters.
WU_INSTALL_SCRIPT = (
    _PS_COMMON
    + _PS_CRITERIA
    + r"""
$MutexName = 'Global\Winstaller.WindowsUpdate'
$LogPath = [string]$env:WINSTALLER_WU_LOG
$StatusPath = [string]$env:WINSTALLER_WU_STATUS
$IdList = @(([string]$env:WINSTALLER_WU_IDS).Split(',') | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ })
$InstallAll = ([string]$env:WINSTALLER_WU_ALL -eq '1')
$SkipDrivers = ([string]$env:WINSTALLER_WU_SKIP_DRIVERS -eq '1')
$SkipDisplayDrivers = ([string]$env:WINSTALLER_WU_SKIP_DISPLAY_DRIVERS -eq '1')
# WUA marks most drivers CanRequestUserInput, yet Windows Update installs them quietly;
# ForceQuiet is set and a failure is reported per update, so they are not skipped.
$SkipInteractive = ([string]$env:WINSTALLER_WU_SKIP_INTERACTIVE -eq '1')
$LogEncoding = New-Object System.Text.UTF8Encoding $true
$JsonEncoding = New-Object System.Text.UTF8Encoding $false
if (-not $LogPath -or -not $StatusPath -or -not [IO.Path]::IsPathRooted($LogPath) -or -not [IO.Path]::IsPathRooted($StatusPath) -or (-not $InstallAll -and $IdList.Count -eq 0)) { exit 7 }

function Write-Log([string]$Message) {
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), (Get-CleanText $Message)
    try { [IO.File]::AppendAllText($LogPath, $line + "`r`n", $LogEncoding) } catch { }
}

function Get-ResultName([int]$Code) {
    switch ($Code) {
        0 { 'nie rozpoczęto' } 1 { 'w toku' } 2 { 'OK' } 3 { 'OK z błędami' }
        4 { 'błąd' } 5 { 'przerwano' } default { "kod $Code" }
    }
}

$status = [ordered]@{
    winstaller = 'wu-install'; state = 'running'; pid = $PID; started = (Get-Date).ToString('o')
    finished = $null; rebootRequired = $false; exitCode = $null; error = $null; updates = @()
}

# Atomic: temp file created fresh (an old one, possibly a planted link, is deleted
# first), then renamed over the status file.
function Save-Status {
    $tmp = $StatusPath + '.tmp'
    try {
        $bytes = $JsonEncoding.GetBytes(($status | ConvertTo-Json -Depth 6))
        if ([IO.File]::Exists($tmp)) { [IO.File]::Delete($tmp) }
        $fs = New-Object IO.FileStream($tmp, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $fs.Write($bytes, 0, $bytes.Length); $fs.Flush($true) } finally { $fs.Dispose() }
        if ([IO.File]::Exists($StatusPath)) { [IO.File]::Replace($tmp, $StatusPath, $null) } else { [IO.File]::Move($tmp, $StatusPath) }
    } catch {
        Write-Log ('Nie udało się zapisać pliku statusu: {0}' -f $_.Exception.Message)
    }
}

function Invoke-WindowsUpdate {
    $session = New-Object -ComObject Microsoft.Update.Session
    $session.ClientApplicationID = 'Winstaller'
    $searcher = $session.CreateUpdateSearcher()
    $searcher.Online = $true
    Write-Log 'Wyszukiwanie aktualizacji...'
    try {
        $result = $searcher.Search($Criteria)
    } catch {
        $hr = Get-HResult $_.Exception
        if ($hr -ne '0x80240032' -and $hr -ne '0x80070057') { throw }
        Write-Log "Kryterium wyszukiwania odrzucone ($hr) - używam zapasowego."
        $result = $searcher.Search($FallbackCriteria)
    }
    Write-Log ('Znaleziono {0} aktualizacji.' -f $result.Updates.Count)

    $todo = New-Object -ComObject Microsoft.Update.UpdateColl
    $seen = @{}
    foreach ($u in $result.Updates) {
        $id = ([string]$u.Identity.UpdateID).ToLowerInvariant()
        $key = '{0}/{1}' -f $id, $u.Identity.RevisionNumber
        if ($seen.ContainsKey($key)) { continue }
        $seen[$key] = $true
        $title = Get-CleanText $u.Title
        if ($IdList.Count -gt 0 -and $IdList -notcontains $id) { Write-Log "Pomijam (nie było jej na liście w Winstallerze): $title"; continue }
        if ([int]$u.Type -eq 2) {
            if ($SkipDrivers) { Write-Log "Pomijam sterownik: $title"; continue }
            if ($SkipDisplayDrivers -and [string]$u.DriverClass -eq 'Display') { Write-Log "Pomijam sterownik grafiki: $title"; continue }
        }
        if ($SkipInteractive -and $u.InstallationBehavior.CanRequestUserInput) { Write-Log "Pomijam (może wymagać interakcji): $title"; continue }
        try {
            if (-not $u.EulaAccepted) { $u.AcceptEula() }
        } catch {
            Write-Log ('Pomijam (nie udało się zaakceptować licencji, {0}): {1}' -f (Get-HResult $_.Exception), $title)
            continue
        }
        [void]$todo.Add($u)
    }
    if ($todo.Count -eq 0) { Write-Log 'Brak aktualizacji do zainstalowania.'; return 0 }

    $installer = $session.CreateUpdateInstaller()
    $waited = 0
    while ($installer.IsBusy -and $waited -lt 600) {
        if ($waited -eq 0) { Write-Log 'Instalator Windows Update jest zajęty - czekam do 10 minut...' }
        Start-Sleep -Seconds 15
        $waited += 15
    }
    if ($installer.IsBusy) { Write-Log 'Instalator Windows Update nadal jest zajęty - kończę bez instalacji.'; $status.error = 'busy'; return 6 }
    if ($installer.RebootRequiredBeforeInstallation) {
        Write-Log 'Windows wymaga ponownego uruchomienia przed instalacją kolejnych aktualizacji - kończę bez instalacji.'
        $status.error = 'reboot_required_before'
        $status.rebootRequired = $true
        return 5
    }

    $toDownload = New-Object -ComObject Microsoft.Update.UpdateColl
    foreach ($u in $todo) { if (-not $u.IsDownloaded) { [void]$toDownload.Add($u) } }
    if ($toDownload.Count -gt 0) {
        Write-Log ('Pobieranie {0} aktualizacji...' -f $toDownload.Count)
        $downloader = $session.CreateUpdateDownloader()
        $downloader.Updates = $toDownload
        try {
            $dl = $downloader.Download()
            Write-Log ('Pobieranie zakończone: {0}.' -f (Get-ResultName ([int]$dl.ResultCode)))
            for ($i = 0; $i -lt $toDownload.Count; $i++) {
                $r = $dl.GetUpdateResult($i)
                if ([int]$r.ResultCode -ge 4) { Write-Log ('  Nie pobrano: {0} (0x{1:X8})' -f $toDownload.Item($i).Title, $r.HResult) }
            }
        } catch {
            Write-Log ('Błąd pobierania ({0}): {1}' -f (Get-HResult $_.Exception), $_.Exception.Message)
        }
    }

    # One by one: an exclusive update must be installed alone, and one failure must
    # not hide the results of the others.
    $results = New-Object System.Collections.ArrayList
    $anyFailed = $false
    foreach ($u in $todo) {
        $entry = [ordered]@{
            id = ([string]$u.Identity.UpdateID).ToLowerInvariant(); revision = [int]$u.Identity.RevisionNumber
            title = (Get-CleanText $u.Title); result = $null; hresult = $null; rebootRequired = $false
        }
        try {
            if (-not $u.IsDownloaded) {
                $entry.result = 'not_downloaded'
                $anyFailed = $true
                Write-Log "Nie pobrano, pomijam instalację: $($entry.title)"
            } else {
                $one = New-Object -ComObject Microsoft.Update.UpdateColl
                [void]$one.Add($u)
                $inst = $session.CreateUpdateInstaller()
                $inst.Updates = $one
                $inst.ForceQuiet = $true
                $inst.AllowSourcePrompts = $false
                Write-Log "Instalacja: $($entry.title)"
                $ir = $inst.Install()
                $ur = $ir.GetUpdateResult(0)
                $entry.result = switch ([int]$ur.ResultCode) { 2 { 'succeeded' } 3 { 'succeeded_with_errors' } 4 { 'failed' } 5 { 'aborted' } default { 'unknown' } }
                $entry.hresult = '0x{0:X8}' -f $ur.HResult
                $entry.rebootRequired = [bool]$ur.RebootRequired
                if ($ir.RebootRequired -or $ur.RebootRequired) { $status.rebootRequired = $true }
                if ([int]$ur.ResultCode -ge 4) { $anyFailed = $true }
                Write-Log ('  -> {0} ({1}){2}' -f (Get-ResultName ([int]$ur.ResultCode)), $entry.hresult, $(if ($entry.rebootRequired) { ', wymaga restartu' } else { '' }))
            }
        } catch {
            $entry.result = 'error'
            $entry.hresult = Get-HResult $_.Exception
            $anyFailed = $true
            Write-Log ('  -> błąd ({0}): {1}' -f $entry.hresult, $_.Exception.Message)
        }
        [void]$results.Add($entry)
        $status.updates = $results.ToArray()
        Save-Status
    }
    try { if ((New-Object -ComObject Microsoft.Update.SystemInfo).RebootRequired) { $status.rebootRequired = $true } } catch { }
    if ($status.rebootRequired) { Write-Log 'Aby dokończyć instalację, uruchom ponownie komputer (Winstaller nie robi tego automatycznie).' }
    if ($anyFailed) { return 2 }
    return 0
}

$exitCode = 1
$mutex = $null
$owned = $false
try {
    $createdNew = $false
    $mutex = New-Object System.Threading.Mutex($true, $MutexName, [ref]$createdNew)
    $owned = $createdNew
    if (-not $owned) {
        Write-Log 'Instalacja aktualizacji Windows już trwa w innym procesie - kończę.'
        $status.error = 'already_running'
        $exitCode = 3
    } else {
        # Only Administrators and SYSTEM may open our mutex (a standard user cannot
        # signal or hold it once we own it).
        try {
            $sec = New-Object System.Security.AccessControl.MutexSecurity
            $sec.SetAccessRuleProtection($true, $false)
            foreach ($sid in @('S-1-5-32-544', 'S-1-5-18')) {
                $identity = New-Object System.Security.Principal.SecurityIdentifier($sid)
                $sec.AddAccessRule((New-Object System.Security.AccessControl.MutexAccessRule($identity, [System.Security.AccessControl.MutexRights]::FullControl, [System.Security.AccessControl.AccessControlType]::Allow)))
            }
            $mutex.SetAccessControl($sec)
        } catch {
            Write-Log ('Nie udało się ograniczyć dostępu do blokady: {0}' -f $_.Exception.Message)
        }
        # Checked before the first status write: an early exit never shows 'running'.
        $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
        if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            Write-Log 'Brak uprawnień administratora - pobieranie i instalacja aktualizacji są niemożliwe.'
            $status.error = 'not_elevated'
            $exitCode = 4
        } else {
            Save-Status
            Write-Log ('Start instalacji aktualizacji w tle (PID {0}).' -f $PID)
            $exitCode = [int](Invoke-WindowsUpdate | Select-Object -Last 1)
        }
    }
} catch {
    $status.error = '{0} {1}' -f (Get-HResult $_.Exception), $_.Exception.Message
    Write-Log "Nieoczekiwany błąd: $($status.error)"
    $exitCode = 1
} finally {
    $status.state = 'finished'
    $status.finished = (Get-Date).ToString('o')
    $status.exitCode = $exitCode
    Save-Status
    Write-Log "Koniec (kod $exitCode)."
    if ($null -ne $mutex) {
        if ($owned) { try { $mutex.ReleaseMutex() } catch { } }
        $mutex.Dispose()
    }
}
exit $exitCode
"""
)

# ---------------------------------------------------------------- Microsoft Store

#: WINSTALLER_STORE_MODE: "update" = ask the Store install service to search for all
#: app updates and queue them for automatic download and install (returns right after
#: the search, InstallService carries on); anything else (default) = "list": read the
#: current install queue only, no search, no changes. WINSTALLER_STORE_TIMEOUT: seconds.
#: Exit: 0 ok, 1 error.
STORE_SCRIPT = (
    _PS_COMMON
    + r"""
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$Mode = $(if ([string]$env:WINSTALLER_STORE_MODE -eq 'update') { 'update' } else { 'list' })
$TimeoutSeconds = 180
$parsed = 0
if ([int]::TryParse([string]$env:WINSTALLER_STORE_TIMEOUT, [ref]$parsed) -and $parsed -gt 0) { $TimeoutSeconds = $parsed }
$out = [ordered]@{
    winstaller = 'store-update'; ok = $false; mode = $Mode; autoUpdateSetting = $null
    blockedByPolicy = $false; found = @(); queue = @(); error = $null
}

function ConvertTo-ItemInfo($Item) {
    $info = [ordered]@{
        productId = [string]$Item.ProductId; packageFamilyName = [string]$Item.PackageFamilyName
        installType = [string]$Item.InstallType; state = $null; percent = $null; errorCode = $null
    }
    try {
        $st = $Item.GetCurrentStatus()
        $info.state = [string]$st.InstallState
        $info.percent = [double]$st.PercentComplete
        if ($null -ne $st.ErrorCode) { $info.errorCode = '0x{0:X8}' -f $st.ErrorCode.HResult }
    } catch { }
    return $info
}

try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.ApplicationModel.Store.Preview.InstallControl.AppInstallManager, Windows.ApplicationModel.Store.Preview, ContentType = WindowsRuntime]
    $null = [Windows.ApplicationModel.Store.Preview.InstallControl.AppUpdateOptions, Windows.ApplicationModel.Store.Preview, ContentType = WindowsRuntime]
    $null = [Windows.ApplicationModel.Store.Preview.InstallControl.AppInstallItem, Windows.ApplicationModel.Store.Preview, ContentType = WindowsRuntime]

    # Windows PowerShell 5.1 cannot await IAsyncOperation<T>: bridge it to a .NET Task.
    $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    } | Select-Object -First 1
    function Wait-WinRt($Operation, [Type]$ResultType) {
        $task = $asTask.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
        if (-not $task.Wait([TimeSpan]::FromSeconds($TimeoutSeconds))) { throw (New-Object TimeoutException 'Microsoft Store nie odpowiedział w wyznaczonym czasie.') }
        return $task.Result
    }

    $mgr = New-Object Windows.ApplicationModel.Store.Preview.InstallControl.AppInstallManager
    $out.autoUpdateSetting = [string]$mgr.AutoUpdateSetting
    if ($Mode -eq 'update') {
        # Best effort: the argument values the Store passes itself are not documented.
        try {
            $out.blockedByPolicy = [bool](Wait-WinRt ($mgr.IsStoreBlockedByPolicyAsync('Microsoft.WindowsStore', 'CN=Microsoft Corporation, O=Microsoft Corporation, L=Redmond, S=Washington, C=US')) ([bool]))
        } catch { }
        if ($out.blockedByPolicy) {
            $out.error = [ordered]@{ hresult = $null; message = 'Microsoft Store jest zablokowany przez zasady.' }
        } else {
            $opts = New-Object Windows.ApplicationModel.Store.Preview.InstallControl.AppUpdateOptions
            $opts.AutomaticallyDownloadAndInstallUpdateIfFound = $true
            $opts.AllowForcedAppRestart = $false
            $listType = [System.Collections.Generic.IReadOnlyList[Windows.ApplicationModel.Store.Preview.InstallControl.AppInstallItem]]
            $found = Wait-WinRt ($mgr.SearchForAllUpdatesAsync('', 'Winstaller', $opts)) $listType
            $out.found = @($found | ForEach-Object { ConvertTo-ItemInfo $_ })
        }
    }
    $out.queue = @($mgr.AppInstallItems | ForEach-Object { ConvertTo-ItemInfo $_ })
    $out.ok = ($null -eq $out.error)
} catch {
    # A timeout means the request may already have been accepted: Winstaller must not
    # fall back to store.exe then.
    $isTimeout = $false
    $x = $_.Exception
    while ($null -ne $x) { if ($x -is [System.TimeoutException]) { $isTimeout = $true }; $x = $x.InnerException }
    $out.error = [ordered]@{ hresult = (Get-HResult $_.Exception); message = (Get-CleanText $_.Exception.Message); timeout = $isTimeout }
}
[Console]::Out.WriteLine(($out | ConvertTo-Json -Depth 6 -Compress))
if ($out.ok) { exit 0 } else { exit 1 }
"""
)

ALL_SCRIPTS: dict[str, str] = {
    "wu-scan": WU_SCAN_SCRIPT,
    "wu-install": WU_INSTALL_SCRIPT,
    "store": STORE_SCRIPT,
}
