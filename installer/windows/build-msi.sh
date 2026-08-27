#!/usr/bin/env bash
# Build AIForge-<version>.msi — Windows, natively, no WSL and no Docker Desktop.
#
# Built with wixl (msitools), which produces a real MSI on macOS/Linux — so this
# comes out of the same CI job as the other two instead of needing a Windows
# runner nobody has. `brew install msitools` / `apt-get install wixl`.
#
# What the MSI installs (per-machine, needs admin once):
#   %ProgramFiles%\AIForge\aiforge-<ver>.whl   the app
#   %ProgramFiles%\AIForge\uv\uv.exe           provisions CPython 3.12 on first run
#   %ProgramFiles%\AIForge\first-run.ps1       the bootstrap
#   %ProgramFiles%\AIForge\AIForge.cmd         what the Start Menu shortcut runs
#
# The runtime itself lands in %LOCALAPPDATA%\AIForge per user, for the same
# reason it does on the other two: the agent runs as that user.
#
# UNTESTED ON WINDOWS from this build host — the MSI's structure is verified
# (msiinfo), its behaviour is not. Run it on a Windows box before shipping.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PAYLOAD="$REPO_ROOT/dist/installer"
VERSION="$(grep -m1 '^version' "$REPO_ROOT/pyproject.toml" | cut -d'"' -f2)"
MSI="$PAYLOAD/AIForge-${VERSION}.msi"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

command -v wixl >/dev/null 2>&1 || {
  echo "wixl not found — 'brew install msitools' or 'apt-get install -y wixl'" >&2; exit 1; }

WHEEL="$(ls -1 "$PAYLOAD"/aiforgecrew-*.whl 2>/dev/null | head -1 || true)"
MEM_WHEEL="$(ls -1 "$PAYLOAD"/aiforge_memory-*.whl "$PAYLOAD"/aiforge-memory-*.whl 2>/dev/null | head -1 || true)"
[[ -n "$WHEEL" ]] || { echo "no wheel — run installer/build_payload.sh first" >&2; exit 1; }
UV_EXE="$PAYLOAD/uv/windows/uv.exe"
[[ -f "$UV_EXE" ]] || { echo "no windows uv — run installer/build_payload.sh --target windows" >&2; exit 1; }

mkdir -p "$STAGE/files/uv"
cp "$WHEEL" "$STAGE/files/$(basename "$WHEEL")"
# The vendored aiforge-memory wheel — no index carries it, --find-links finds it.
[[ -n "$MEM_WHEEL" ]] && cp "$MEM_WHEEL" "$STAGE/files/$(basename "$MEM_WHEEL")"
cp "$UV_EXE" "$STAGE/files/uv/uv.exe"
cp "$REPO_ROOT/installer/windows/first-run.ps1" "$STAGE/files/first-run.ps1"

cat > "$STAGE/files/AIForge.cmd" <<'CMD'
@echo off
REM What the Start Menu shortcut runs. -ExecutionPolicy Bypass is scoped to this
REM process only: the machine's policy is not touched, and a default Windows
REM (RemoteSigned) would otherwise refuse an unsigned .ps1 shipped in an MSI.
setlocal
set "AIFORGE_APP_HOME=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0first-run.ps1" --open %*
endlocal
CMD

# wixl wants \ paths and a GUID per component; both are generated here so the
# only hand-written thing is the layout above.
guid() { python3 -c "import uuid,sys;print(str(uuid.uuid5(uuid.NAMESPACE_URL,'aiforge-msi-'+sys.argv[1])).upper())" "$1"; }

cat > "$STAGE/aiforge.wxs" <<WXS
<?xml version="1.0" encoding="utf-8"?>
<Wix xmlns="http://schemas.microsoft.com/wix/2006/wi">
  <Product Name="AIForge" Id="*" UpgradeCode="$(guid upgrade)"
           Language="1033" Codepage="1252" Version="$VERSION"
           Manufacturer="AIForge">
    <Package Id="*" Keywords="Installer" Description="AIForge $VERSION"
             InstallerVersion="200" Languages="1033" Compressed="yes"
             SummaryCodepage="1252" InstallScope="perMachine"/>
    <!-- No AllowSameVersionUpgrades: wixl does not implement that attribute and
         warns about it. Version bumps upgrade cleanly; a same-version reinstall
         needs an uninstall first, which is the stock MSI behaviour anyway. -->
    <MajorUpgrade DowngradeErrorMessage="A newer AIForge is already installed."/>
    <Media Id="1" Cabinet="aiforge.cab" EmbedCab="yes"/>

    <Directory Id="TARGETDIR" Name="SourceDir">
      <Directory Id="ProgramFiles64Folder">
        <Directory Id="INSTALLDIR" Name="AIForge">
          <Component Id="AppFiles" Guid="$(guid appfiles)" Win64="yes">
            <File Id="Wheel"   Source="files/$(basename "$WHEEL")" KeyPath="yes"/>
            $( [[ -n "$MEM_WHEEL" ]] && echo "<File Id=\"MemWheel\" Source=\"files/$(basename "$MEM_WHEEL")\"/>" )
            <File Id="FirstRun" Source="files/first-run.ps1"/>
            <File Id="LaunchCmd" Source="files/AIForge.cmd"/>
          </Component>
          <Directory Id="UVDIR" Name="uv">
            <Component Id="UvFiles" Guid="$(guid uvfiles)" Win64="yes">
              <File Id="UvExe" Source="files/uv/uv.exe" KeyPath="yes"/>
            </Component>
          </Directory>
        </Directory>
      </Directory>
      <Directory Id="ProgramMenuFolder">
        <Directory Id="AppMenuDir" Name="AIForge">
          <Component Id="Shortcuts" Guid="$(guid shortcuts)" Win64="yes">
            <Shortcut Id="StartMenuShortcut" Name="AIForge"
                      Description="Local AI engineering crew"
                      Target="[INSTALLDIR]AIForge.cmd"
                      WorkingDirectory="INSTALLDIR"/>
            <RemoveFolder Id="AppMenuDir" On="uninstall"/>
            <!-- Per-user keypath: an MSI component whose keypath is a file in
                 Program Files cannot own a per-user shortcut. -->
            <RegistryValue Root="HKCU" Key="Software\AIForge"
                           Name="installed" Type="integer" Value="1" KeyPath="yes"/>
          </Component>
        </Directory>
      </Directory>
    </Directory>

    <Feature Id="Complete" Level="1" Title="AIForge">
      <ComponentRef Id="AppFiles"/>
      <ComponentRef Id="UvFiles"/>
      <ComponentRef Id="Shortcuts"/>
    </Feature>
  </Product>
</Wix>
WXS

( cd "$STAGE" && wixl -v -o "$MSI" aiforge.wxs >/dev/null )
echo "==> $MSI"
if command -v msiinfo >/dev/null 2>&1; then
  msiinfo tables "$MSI" | tr '\n' ' ' | sed 's/^/    tables: /'; echo
fi
echo "    NOTE: built off-Windows. Structure verified, behaviour is not —"
echo "          run it on a Windows box before shipping."
