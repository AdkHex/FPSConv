<#
.SYNOPSIS
    Installs everything fpsaudio needs on Windows.

.DESCRIPTION
    Replaces the master prompt's install.sh, which assumed a Unix target. The
    runtime target here is Windows only.

    Uses winget where a package genuinely exists, and pinned direct downloads
    with SHA-256 verification for the rest. Nothing is fetched without its hash
    being checked first, and no checksum is ever "fixed" by recording whatever
    the download happened to produce -- an unverified download is reported and
    skipped.

    Python packages all ship Windows wheels for 3.11+, so nothing compiles.

.PARAMETER SkipOptional
    Install only what is strictly required (ffmpeg, MediaInfo, Python packages).

.PARAMETER ToolsDir
    Where to unpack tools that have no winget package. Default: <script dir>\tools

.PARAMETER PythonExe
    Full path to a python.exe to use. Only needed when Python is installed
    somewhere the search does not look; run without it first.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1 -PythonExe "D:\Python312\python.exe"
#>

[CmdletBinding()]
param(
    [switch]$SkipOptional,
    # Deliberately NOT defaulted to (Join-Path $PSScriptRoot 'tools').
    # $PSScriptRoot is not populated while param defaults are evaluated on older
    # Windows PowerShell hosts (it is empty in scripts entirely on 2.0), which
    # made Join-Path throw before the script body ever ran. Resolved below.
    [string]$ToolsDir,

    # Full path to a python.exe to use, for installs that are not on PATH and
    # not in a standard location.
    [string]$PythonExe
)

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

$script:Failures = @()
$script:Skipped  = @()

function Write-Step   { param($m) Write-Host "`n==> $m" -ForegroundColor Cyan }
function Write-Ok     { param($m) Write-Host "    OK   $m" -ForegroundColor Green }
function Write-Warn   { param($m) Write-Host "    WARN $m" -ForegroundColor Yellow }
function Write-Fail   { param($m) Write-Host "    FAIL $m" -ForegroundColor Red }

# --------------------------------------------------------------------------- #
# Where am I?  Resolved defensively, because $PSScriptRoot is not dependable
# on every host that can launch this script.
# --------------------------------------------------------------------------- #

# Resolved at SCRIPT scope on purpose. Inside a function,
# $MyInvocation.MyCommand.Path describes the function, not the script, so the
# usual fallback would silently return nothing — the same class of trap that
# broke the param default.
$ScriptRoot = $null

if ($PSScriptRoot) {
    $ScriptRoot = $PSScriptRoot
}
if (-not $ScriptRoot -and $MyInvocation.MyCommand.Path) {
    # The classic pre-3.0 equivalent; also correct on every later version.
    $ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
}
if (-not $ScriptRoot -and $PSCommandPath) {
    $ScriptRoot = Split-Path -Parent $PSCommandPath
}
if (-not $ScriptRoot) {
    # Install.bat cds to the script's folder before launching, so this is a
    # sound last resort rather than a guess.
    $ScriptRoot = (Get-Location).Path
}

if (-not $ToolsDir) { $ToolsDir = Join-Path $ScriptRoot 'tools' }

# --------------------------------------------------------------------------- #
# Host requirements.  Fail with an explanation rather than a stack trace when a
# needed cmdlet simply does not exist on this host.
# --------------------------------------------------------------------------- #

$psMajor = $PSVersionTable.PSVersion.Major
Write-Host "PowerShell   : $($PSVersionTable.PSVersion)"
Write-Host "Script folder: $ScriptRoot"

if ($psMajor -lt 5) {
    Write-Fail "PowerShell $($PSVersionTable.PSVersion) is too old."
    Write-Host @'
    This installer needs PowerShell 5.0 or newer: it uses Get-FileHash to verify
    downloads and Expand-Archive to unpack them, and neither exists before 5.0.

    Windows 10 and 11 ship PowerShell 5.1 as "Windows PowerShell". If you landed
    on an older engine, launch the newer one explicitly:

        %SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe -ExecutionPolicy Bypass -File install.ps1

    Check what you have with:

        $PSVersionTable
'@
    exit 1
}

if (-not (Test-Path -LiteralPath (Join-Path $ScriptRoot 'requirements.txt'))) {
    Write-Fail "This does not look like the fpsaudio folder: no requirements.txt in $ScriptRoot"
    Write-Host '    Run Install.bat from the folder it shipped in.'
    exit 1
}

# --------------------------------------------------------------------------- #
# Python
# --------------------------------------------------------------------------- #

$script:PythonProbes = @()

function Test-PythonCandidate {
    <#
      Ask one candidate interpreter for its version.
      Returns a hashtable describing what happened, always - a candidate that
      does not work is data, not an error. Every outcome is recorded so the
      failure message can show what was actually tried.
    #>
    param([string]$Exe, [string[]]$Arguments = @(), [string]$Label)

    $result = @{
        Label = $Label; Exe = $Exe; Args = $Arguments
        Version = $null; Major = 0; Minor = 0; Status = 'not found'
    }

    # Resolve on PATH, or accept a full path that exists.
    $resolved = $null
    if ($Exe -match '[\\/]') {
        if (Test-Path -LiteralPath $Exe) { $resolved = $Exe }
    } else {
        $cmd = Get-Command $Exe -ErrorAction SilentlyContinue
        if ($cmd) { $resolved = $Exe }
    }
    if (-not $resolved) { return $result }

    $probe = $Arguments + @('-c', 'import sys; print("%d.%d" % sys.version_info[:2])')
    $raw = ''
    # ErrorActionPreference is 'Stop' for the script, but a native program
    # writing to stderr must not be fatal here: several of these candidates are
    # *expected* to fail. 2>&1 keeps the text for the diagnosis.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $raw = (& $resolved @probe 2>&1 | Out-String)
    } catch {
        $raw = "$_"
    } finally {
        $ErrorActionPreference = $previous
    }

    $result.Output = ($raw -replace '\s+', ' ').Trim()

    # The Windows "App Execution Alias" stub: python.exe exists on PATH but only
    # opens the Microsoft Store. It is the single most common reason a Windows
    # box appears to have Python and does not.
    if ($raw -match 'Microsoft Store|was not found; run without arguments') {
        $result.Status = 'Microsoft Store alias stub, not a real Python'
        return $result
    }

    foreach ($line in ($raw -split "`r?`n")) {
        $trimmed = $line.Trim()
        if ($trimmed -match '^(\d+)\.(\d+)$') {
            $result.Major = [int]$Matches[1]
            $result.Minor = [int]$Matches[2]
            $result.Version = $trimmed
            if ($result.Major -eq 3 -and $result.Minor -ge 11) {
                $result.Status = 'OK'
            } else {
                $result.Status = "Python $trimmed - too old, need 3.11+"
            }
            return $result
        }
    }

    if (-not $result.Output) { $result.Status = 'ran but printed nothing' }
    else { $result.Status = "unexpected output: $($result.Output)" }
    return $result
}

function Find-Python {
    # fpsaudio needs 3.11+: tomllib for config, and dataclass slots.
    $candidates = @(
        @{ Exe = 'py';      Args = @('-3.14') },
        @{ Exe = 'py';      Args = @('-3.13') },
        @{ Exe = 'py';      Args = @('-3.12') },
        @{ Exe = 'py';      Args = @('-3.11') },
        @{ Exe = 'py';      Args = @('-3')    },
        @{ Exe = 'python';  Args = @()        },
        @{ Exe = 'python3'; Args = @()        }
    )

    # Python being installed but absent from PATH is extremely common on
    # Windows - the installer's "Add to PATH" box is off by default. Look where
    # it actually lands before giving up.
    # Each base is checked BEFORE Join-Path is called: Join-Path throws on a
    # null or empty first argument, and these variables are simply absent on a
    # non-Windows host or in a stripped environment.
    $searchRoots = @(
        @{ Base = $env:LOCALAPPDATA;            Leaf = 'Programs\Python\Python3*\python.exe' },
        @{ Base = $env:ProgramFiles;            Leaf = 'Python3*\python.exe' },
        @{ Base = ${env:ProgramFiles(x86)};     Leaf = 'Python3*\python.exe' },
        @{ Base = $env:SystemDrive;             Leaf = 'Python3*\python.exe' }
    )
    foreach ($root in $searchRoots) {
        if ([string]::IsNullOrWhiteSpace($root.Base)) { continue }
        $glob = Join-Path $root.Base $root.Leaf
        $found = @(Get-ChildItem -Path $glob -ErrorAction SilentlyContinue |
                   Sort-Object FullName -Descending)
        foreach ($item in $found) {
            $candidates += @{ Exe = $item.FullName; Args = @() }
        }
    }

    foreach ($candidate in $candidates) {
        $label = ($candidate.Exe + ' ' + ($candidate.Args -join ' ')).Trim()
        $probe = Test-PythonCandidate -Exe $candidate.Exe -Arguments $candidate.Args -Label $label
        $script:PythonProbes += $probe
        if ($probe.Status -eq 'OK') {
            return @{ Exe = $candidate.Exe; Args = $candidate.Args; Version = $probe.Version }
        }
    }
    return $null
}

Write-Step 'Checking Python (3.11 or newer required)'

$python = $null
if ($PythonExe) {
    # An explicit path is an instruction, not a hint: if it is wrong, say so and
    # stop rather than quietly searching elsewhere and using something else.
    if (-not (Test-Path -LiteralPath $PythonExe)) {
        Write-Fail "-PythonExe does not exist: $PythonExe"
        exit 1
    }
    $probe = Test-PythonCandidate -Exe $PythonExe -Label $PythonExe
    if ($probe.Status -ne 'OK') {
        Write-Fail "-PythonExe is not usable: $($probe.Status)"
        Write-Host "    $PythonExe"
        exit 1
    }
    $python = @{ Exe = $PythonExe; Args = @(); Version = $probe.Version }
} else {
    $python = Find-Python
}

if (-not $python) {
    Write-Fail 'No Python 3.11 or newer was found.'
    Write-Host ''
    Write-Host '    This is what was tried, and what each one said:'
    Write-Host ''
    foreach ($probe in $script:PythonProbes) {
        $label = $probe.Label
        if ($label.Length -gt 46) { $label = '...' + $label.Substring($label.Length - 43) }
        Write-Host ("      {0,-46}  {1}" -f $label, $probe.Status)
    }
    Write-Host ''

    # The launcher's own inventory is the most useful Windows diagnostic there
    # is, so show it when the launcher exists at all.
    if (Get-Command py -ErrorAction SilentlyContinue) {
        Write-Host '    Installed versions the "py" launcher can see (py -0p):'
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $inventory = (& py -0p 2>&1 | Out-String).Trim()
            if ($inventory) {
                foreach ($line in ($inventory -split "`r?`n")) { Write-Host "      $line" }
            } else {
                Write-Host '      (the launcher reported nothing)'
            }
        } catch {
            Write-Host "      (py -0p failed: $_)"
        } finally {
            $ErrorActionPreference = $previous
        }
        Write-Host ''
    }

    Write-Host @'
    fpsaudio needs Python 3.11 or newer: it reads its config with tomllib,
    which was added in 3.11.

    To install it:

        winget install --id Python.Python.3.12 -e

    IMPORTANT: after installing, CLOSE this window and open a new one before
    re-running Install.bat. A running console keeps the PATH it started with,
    so a freshly installed Python will not be visible until you do.

    If you install from python.org instead, tick "Add python.exe to PATH" on
    the first page of the installer.

    Already installed somewhere unusual? Point at it directly:

        powershell -ExecutionPolicy Bypass -File install.ps1 -PythonExe "D:\Python312\python.exe"
'@
    exit 1
}
Write-Ok "Python $($python.Version) via '$(($python.Exe + ' ' + ($python.Args -join ' ')).Trim())'"

# --------------------------------------------------------------------------- #
# Virtual environment and Python packages
# --------------------------------------------------------------------------- #

$venvDir = Join-Path $ScriptRoot '.venv'

function Get-VenvPython {
    param([string]$Dir)
    # Windows puts it in Scripts\python.exe; every other layout uses bin/python.
    # Checking both costs nothing and makes this script testable off-Windows.
    foreach ($relative in @('Scripts\python.exe', 'bin/python')) {
        $candidate = Join-Path $Dir $relative
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    return $null
}

Write-Step 'Creating the virtual environment'
$venvPy = Get-VenvPython -Dir $venvDir
if ($venvPy) {
    Write-Ok '.venv already exists'
} else {
    & $python.Exe @($python.Args + @('-m', 'venv', $venvDir))
    $venvPy = Get-VenvPython -Dir $venvDir
    if (-not $venvPy) {
        Write-Fail 'Could not create .venv.'
        Write-Host '    Your Python launcher may point at a removed install. Try: py -0p'
        exit 1
    }
    Write-Ok 'created .venv'
}

Write-Step 'Installing Python packages (all ship Windows wheels; nothing compiles)'
& $venvPy -m pip install --upgrade pip --quiet
$requirements = Join-Path $ScriptRoot 'requirements.txt'
if (Test-Path $requirements) {
    & $venvPy -m pip install -r $requirements
    if ($LASTEXITCODE -ne 0) {
        Write-Fail 'pip install failed. See the output above.'
        exit 1
    }
    Write-Ok 'numpy, soxr, soundfile, pyloudnorm, typer, textual'
} else {
    Write-Fail "requirements.txt is missing from $ScriptRoot"
    exit 1
}

# --------------------------------------------------------------------------- #
# winget packages
# --------------------------------------------------------------------------- #

function Install-Winget {
    param([string]$Id, [string]$Name, [string]$Probe, [switch]$Required)

    if (Get-Command $Probe -ErrorAction SilentlyContinue) {
        Write-Ok "$Name already on PATH"
        return
    }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        $msg = "$Name is missing and winget is not available to install it"
        if ($Required) { $script:Failures += $msg; Write-Fail $msg }
        else { $script:Skipped += $msg; Write-Warn $msg }
        return
    }

    Write-Host "    installing $Name ($Id) ..."
    winget install --id $Id -e --accept-package-agreements --accept-source-agreements --silent
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "$Name installed (a new terminal may be needed for PATH)"
    } else {
        $msg = "$Name failed to install via winget (exit $LASTEXITCODE)"
        if ($Required) { $script:Failures += $msg; Write-Fail $msg }
        else { $script:Skipped += $msg; Write-Warn $msg }
    }
}

Write-Step 'Installing required external tools'
Install-Winget -Id 'Gyan.FFmpeg'            -Name 'FFmpeg'    -Probe 'ffmpeg'    -Required
Install-Winget -Id 'MediaArea.MediaInfo.CLI' -Name 'MediaInfo' -Probe 'mediainfo' -Required

if (-not $SkipOptional) {
    Write-Step 'Installing optional external tools'
    Install-Winget -Id 'MoritzBunkus.MKVToolNix' -Name 'MKVToolNix'  -Probe 'mkvmerge'
    Install-Winget -Id 'Xiph.Flac'               -Name 'FLAC'        -Probe 'flac'
    Install-Winget -Id 'Xiph.Opus-tools'         -Name 'opus-tools'  -Probe 'opusenc'
}

# --------------------------------------------------------------------------- #
# Pinned downloads with checksum verification
# --------------------------------------------------------------------------- #

# Tools with no winget package. Each entry MUST carry a known SHA-256.
#
# The hashes below are deliberately left empty: publishing a checksum that was
# never verified against the real artefact would be worse than having none, and
# these binaries could not be downloaded from the build machine. Fill one in and
# the installer will fetch and verify it; leave it empty and the installer says
# so and moves on. It will never download something it cannot check.
$PinnedDownloads = @(
    @{
        Name   = 'fdkaac'
        Probe  = 'fdkaac'
        Url    = ''
        Sha256 = ''
        Note   = 'AAC encoder. See docs/TOOLS.md for a source and its checksum.'
    },
    @{
        Name   = 'wavpack'
        Probe  = 'wavpack'
        Url    = ''
        Sha256 = ''
        Note   = 'WavPack encoder: https://www.wavpack.com/downloads.html'
    },
    @{
        Name   = 'rubberband'
        Probe  = 'rubberband'
        Url    = ''
        Sha256 = ''
        Note   = 'Only needed for --method stretch (opt-in, not the default).'
    },
    @{
        Name   = 'truehdd'
        Probe  = 'truehdd'
        Url    = ''
        Sha256 = ''
        Note   = 'Required for Dolby Atmos. https://github.com/truehdd/truehdd'
    }
)

function Install-Pinned {
    param($Spec)

    if (Get-Command $Spec.Probe -ErrorAction SilentlyContinue) {
        Write-Ok "$($Spec.Name) already on PATH"
        return
    }
    if ([string]::IsNullOrWhiteSpace($Spec.Url) -or [string]::IsNullOrWhiteSpace($Spec.Sha256)) {
        $msg = "$($Spec.Name) not installed - no verified download is pinned. $($Spec.Note)"
        $script:Skipped += $msg
        Write-Warn $msg
        return
    }

    $null = New-Item -ItemType Directory -Force -Path $ToolsDir
    $archive = Join-Path $ToolsDir ([System.IO.Path]::GetFileName($Spec.Url))
    Write-Host "    downloading $($Spec.Name) ..."
    try {
        Invoke-WebRequest -Uri $Spec.Url -OutFile $archive -UseBasicParsing
    } catch {
        $msg = "$($Spec.Name) download failed: $_"
        $script:Skipped += $msg; Write-Warn $msg
        return
    }

    $actual = (Get-FileHash -Path $archive -Algorithm SHA256).Hash
    if ($actual -ne $Spec.Sha256.ToUpper()) {
        Remove-Item $archive -Force
        $msg = "$($Spec.Name) CHECKSUM MISMATCH - expected $($Spec.Sha256), got $actual. Discarded."
        $script:Failures += $msg
        Write-Fail $msg
        return
    }

    $target = Join-Path $ToolsDir $Spec.Name
    Expand-Archive -Path $archive -DestinationPath $target -Force
    Remove-Item $archive -Force
    Write-Ok "$($Spec.Name) unpacked to $target (add it to PATH)"
}

if (-not $SkipOptional) {
    Write-Step 'Tools with no winget package'
    foreach ($spec in $PinnedDownloads) { Install-Pinned -Spec $spec }
}

# --------------------------------------------------------------------------- #
# Verify the install by asking fpsaudio itself
# --------------------------------------------------------------------------- #

Write-Step 'Running fpsaudio doctor'
& $venvPy -m fpsaudio doctor
$doctorExit = $LASTEXITCODE

Write-Host "`n============================================================"
if ($script:Failures.Count -gt 0) {
    Write-Host 'Install finished with failures:' -ForegroundColor Red
    $script:Failures | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
}
if ($script:Skipped.Count -gt 0) {
    Write-Host 'Not installed (features needing these will refuse, not degrade):' -ForegroundColor Yellow
    $script:Skipped | ForEach-Object { Write-Host "  - $_" -ForegroundColor Yellow }
}
if ($doctorExit -eq 0 -and $script:Failures.Count -eq 0) {
    Write-Host 'Ready. Launch with "Run FPS Audio.bat".' -ForegroundColor Green
} else {
    Write-Host 'Some essential tools are missing - see the doctor output above.' -ForegroundColor Red
}
Write-Host '============================================================'

exit $doctorExit
