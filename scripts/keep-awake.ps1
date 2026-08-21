<#
.SYNOPSIS
  Keep this machine awake until you stop the script (Ctrl-C).

.DESCRIPTION
  Screen LOCK is never the problem — Windows keeps running through it. SLEEP
  is: it suspends everything, and under WSL it freezes the entire distro, so a
  long run comes back to a dead model socket with all its finished work waiting
  to be re-done.

  Holds ES_CONTINUOUS | ES_SYSTEM_REQUIRED for as long as this script runs.
  Deliberately NOT ES_DISPLAY_REQUIRED: keeping a machine awake is reasonable,
  keeping it unlocked is not — the screen still locks normally.

  Nothing permanent is changed. No `powercfg` setting is written, so there is
  nothing left behind if this is killed, and no elevation is needed: the API is
  available to the ordinary user.

  From inside WSL use scripts/keep-awake.sh instead — it calls this same API
  through interop.

.EXAMPLE
  .\scripts\keep-awake.ps1

.EXAMPLE
  .\scripts\keep-awake.ps1 -Command "python -m pytest tests/python"
  Holds the machine awake for exactly as long as that command runs.
#>
param(
  [string]$Command = ""
)

$ErrorActionPreference = "Stop"

$sig = @'
[DllImport("kernel32.dll", SetLastError = true)]
public static extern uint SetThreadExecutionState(uint esFlags);
'@
$pwr = Add-Type -MemberDefinition $sig -Name Pwr -Namespace AIForge -PassThru

$ES_CONTINUOUS      = [uint32]0x80000000
$ES_SYSTEM_REQUIRED = [uint32]0x00000001

# A zero return means the request was refused — say so rather than sitting
# there implying the machine is being held awake when it is not.
$prev = $pwr::SetThreadExecutionState($ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED)
if ($prev -eq 0) {
  Write-Error "keep-awake: Windows refused the request; the machine may still sleep."
  exit 1
}
Write-Host "keep-awake: holding. The screen still locks normally."

try {
  if ($Command) {
    # The assertion belongs to THIS thread, so the command runs as a child and
    # the finally block releases as soon as it exits, however it exits.
    & cmd.exe /c $Command
    exit $LASTEXITCODE
  }
  Write-Host "keep-awake: press Ctrl-C to release."
  while ($true) { Start-Sleep -Seconds 60 }
}
finally {
  # Back to the machine's own policy. ES_CONTINUOUS alone clears the assertion.
  $pwr::SetThreadExecutionState($ES_CONTINUOUS) | Out-Null
  Write-Host "keep-awake: released."
}
