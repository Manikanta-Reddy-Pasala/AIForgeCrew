# First-run bootstrap for the Windows package — the .ps1 half of
# installer/common/first-run.sh. Same contract, same layout, same idempotence:
# after the first launch it is three checks and a start.
#
#   $env:AIFORGE_APP_HOME   where the MSI put the wheel + uv.exe (Program Files)
#   $env:AIFORGE_DATA_HOME  this user's venv (%LOCALAPPDATA%\AIForge)
#
# Native Windows, no WSL and no Docker. The runtime is built on the machine's
# own Python 3.12 (winget install Python.Python.3.12): uv never downloads an
# interpreter, because its managed CPython comes from GitHub, not an index.
#
# One honest limitation, and it is not a bug in this script: the agent's `bash`
# tool keeps its per-run state in a tmux session, and there is no tmux here.
# It degrades to a stateless subprocess per command (BashFallback,
# reason=tmux_missing) — `cd` and `export` do not carry between calls on Windows.
$ErrorActionPreference = 'Stop'

$appHome  = if ($env:AIFORGE_APP_HOME)  { $env:AIFORGE_APP_HOME }
            else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$dataHome = if ($env:AIFORGE_DATA_HOME) { $env:AIFORGE_DATA_HOME }
            else { Join-Path $env:LOCALAPPDATA 'AIForge' }
$venv       = Join-Path $dataHome 'venv'
$pyVersion  = if ($env:AIFORGE_PYTHON_VERSION) { $env:AIFORGE_PYTHON_VERSION } else { '3.12' }

$env:UV_PYTHON_DOWNLOADS = 'never'
$env:UV_PYTHON_PREFERENCE = 'only-system'
if (-not $env:UV_SYSTEM_CERTS) { $env:UV_SYSTEM_CERTS = 'true' }   # the internal CA is in the OS store

$uv = Join-Path $appHome 'uv\uv.exe'
if (-not (Test-Path $uv)) {
    $onPath = Get-Command uv -ErrorAction SilentlyContinue
    if ($onPath) { $uv = $onPath.Source }
    else { throw 'AIForge: no uv.exe in the package and none on PATH - cannot build the runtime.' }
}

# The APP wheel specifically - the directory also holds the vendored
# aiforge-memory wheel, and installing that one gives a venv with a library in
# it and no application.
$wheel = Get-ChildItem -Path $appHome -Filter 'aiforgecrew-*.whl' -ErrorAction SilentlyContinue |
         Select-Object -First 1
if (-not $wheel) { throw "AIForge: no wheel in $appHome - the package is incomplete." }

# Keyed on the wheel's NAME, so installing a new version rebuilds the runtime
# without the user having to know that is what happened.
$marker    = Join-Path $dataHome (".installed-" + $wheel.Name)
$venvPy    = Join-Path $venv 'Scripts\python.exe'
$venvStart = Join-Path $venv 'Scripts\aiforge.exe'

if (-not (Test-Path $marker) -or -not (Test-Path $venvStart)) {
    Write-Host 'AIForge: preparing the runtime (first run after install - this needs the network once)...'
    New-Item -ItemType Directory -Force -Path $dataHome | Out-Null
    # uv reads pyproject.toml/uv.toml from the current directory: launched from
    # inside a project, that project's index became the app's. Install from here.
    Push-Location $dataHome
    # uv downloads a managed CPython when the machine has none of the right
    # version, which on Windows is the normal case.
    #
    # An UPGRADE lands here too: the new wheel's name does not match the marker,
    # so this block runs again with a venv already in place. `uv venv` refuses
    # to touch an existing one, which turned every upgrade into a dead
    # launcher. Create when absent, rebuild when broken, otherwise install the
    # new wheel into the runtime that already works.
    & $uv python find $pyVersion *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "AIForge: needs Python $pyVersion on this machine, and found none. Install it: winget install Python.Python.$pyVersion"
    }
    if (-not (Test-Path $venvPy)) {
        if (Test-Path $venv) {
            Write-Host 'AIForge: the runtime is incomplete - rebuilding it.'
            & $uv venv --clear --python $pyVersion $venv
        } else {
            & $uv venv --python $pyVersion $venv
        }
        if ($LASTEXITCODE -ne 0) { throw "AIForge: could not create the runtime (uv venv exit $LASTEXITCODE)" }
    } else {
        Write-Host 'AIForge: updating the existing runtime.'
    }
    # --find-links: aiforge-memory is vendored, ships beside the app wheel and
    # exists on no index. Everything else still resolves from PyPI.
    #
    # WITH THE EXTRAS: a bare wheel install pulls base dependencies only, and
    # the extras are semantic recall, chunking, structured output and crawl.
    # Without them the app starts, serves every route, and then degrades
    # feature by feature - which reads as "some pages do not work".
    # --override: exactly uv.lock's versions, so the installed app runs what CI
    # tested. Not -c: the lock overrides google-adk's starlette cap.
    # The index the package was built against (the internal Artifactory);
    # uv's own default would be pypi.org. $env:UV_DEFAULT_INDEX wins.
    $indexFile = Join-Path $appHome 'index-url.txt'
    if (-not $env:UV_DEFAULT_INDEX -and (Test-Path $indexFile)) {
        $env:UV_DEFAULT_INDEX = (Get-Content $indexFile -TotalCount 1).Trim()
    }
    $pin = @()
    $lockPins = Join-Path $appHome 'lock-pins.txt'
    if (Test-Path $lockPins) { $pin = @('--override', $lockPins) }
    # --no-build: wheels only, nothing built from a downloaded source archive.
    & $uv pip install --python $venvPy --no-build @pin --find-links $appHome ($wheel.FullName + '[xlsx,structured,crawl,chunking,embed-static]')
    if ($LASTEXITCODE -ne 0) { throw "AIForge: could not install the app (uv pip exit $LASTEXITCODE)" }
    # Written last: a half-built venv must not look finished next launch.
    New-Item -ItemType File -Force -Path $marker | Out-Null
    Get-ChildItem -Path $dataHome -Filter '.installed-*' -Force |
        Where-Object { $_.Name -ne (Split-Path $marker -Leaf) } |
        Remove-Item -Force -ErrorAction SilentlyContinue
    Pop-Location
    Write-Host 'AIForge: runtime ready.'
}

& $venvStart @args
exit $LASTEXITCODE
