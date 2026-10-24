"""`./run.sh --isolated`: the egress allowlist enforced by the NETWORK.

By default the box runs `network_mode: host` — it shares this machine's network
stack, so ``net/egress.py`` is the only thing between the agent and the
internet, and it only governs DECLARED destinations and page fetches. A `curl`
typed into the agent's shell never passes through it.

`--isolated` puts the box on an internal docker network with no route out and
sends every outbound request through a proxy holding the operator's allowlist.
These pin the wiring: the right base file, a DEFAULT-DENY proxy config, the
allowlist written from the same env the app reads, and a refusal to combine two
base files.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RUN_SH = REPO / "run.sh"


_FAKE_DOCKER = r"""#!/bin/sh
echo "$*" >> "{log}"
# Answers for run.sh's post-start isolation check (_verify_isolated).
case "$*" in
  "inspect aiforge-egress-guard --format"*) echo "{guard}" ;;
  "inspect aiforge-egress-guard") exit 1 ;;
  *"exec aiforge sh -c python3"*) echo "{probe}" ;;
  "inspect aiforge --format"*) echo "proj_box " ;;
  *"network inspect proj_box"*Internal*) echo true ;;
  *"network inspect proj_box"*inhibit_ipv4*) echo "{inhibit}" ;;
  *"network inspect proj_box"*EnableIPv6*) echo false ;;
  *"network inspect proj_box"*Subnet*) echo "10.251.250.0/24" ;;
  *"exec aiforge sh -c echo box-up"*) echo box-up ;;
  *"exec aiforge sh -c getent"*) if [ "{getent}" = 0 ]; then echo DNS-LEAK; else echo DNS-NONE; fi ;;
  *"exec aiforge sh -c if [ -s /proc/net/if_inet6 ]"*) echo V6-NO ;;
esac
exit 0
"""


def _run(tmp_path: Path, args: list[str], extra_env: dict | None = None,
         inhibit: str = "true", getent: int = 2, box_up: bool = True,
         guard: str = "healthy", probe: str = "NET-BLOCKED"):
    """run.sh (from its own checkout folder) with a fake `docker` on PATH that
    records how it was called and answers the isolation check."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "docker.log"
    fake = bindir / "docker"
    text = (_FAKE_DOCKER.replace("{log}", str(log))
            .replace("{inhibit}", inhibit).replace("{getent}", str(getent))
            .replace("{guard}", guard).replace("{probe}", probe))
    if not box_up:
        text = text.replace('echo box-up ;;', 'exit 1 ;;')
    fake.write_text(text)
    fake.chmod(0o755)

    checkout = tmp_path / "checkout"
    checkout.mkdir(exist_ok=True)
    dst = checkout / "run.sh"
    shutil.copy(RUN_SH, dst)
    env = {**os.environ,
           "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
           "AIFORGE_CONFIG_DIR": str(tmp_path / "cfg"),
           "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
           # Force the sandbox branch: this suite runs INSIDE a container, where
           # detection would otherwise take the in-box path and never call docker.
           "AIFORGE_IN_SANDBOX": "0"}
    env.pop("COMPOSE_FILE", None)
    env.pop("AIFORGE_REPOS_DIR", None)
    env.update(extra_env or {})
    proc = subprocess.run(["bash", str(dst), *args], cwd=str(checkout),
                          capture_output=True, text=True, timeout=120, env=env)
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, calls


def _sandbox(tmp_path: Path) -> Path:
    return tmp_path / "cfg" / ".sandbox"


def test_isolated_selects_the_isolated_base_file(tmp_path: Path):
    """It REPLACES the base rather than overlaying: compose cannot take
    `network_mode: host` back off once the default file has set it."""
    _proc, calls = _run(tmp_path, ["--isolated"],
                        {"AIFORGE_EGRESS_ALLOW_HOSTS": "artifactory.internal"})
    up = [c for c in calls if " up " in f" {c} "]
    assert up, calls
    assert "-f docker-compose.isolated.yml" in up[0]
    assert "-f docker-compose.yml" not in up[0]


def test_guarded_is_the_default(tmp_path: Path):
    """Plain ./run.sh: the host network, with the internet limited to the
    allowlist (an overlay on docker-compose.yml)."""
    proc, calls = _run(tmp_path, [])
    up = [c for c in calls if " up " in f" {c} "]
    assert up, calls
    assert "-f docker-compose.yml -f docker-compose.guard.yml" in up[0]
    assert "docker-compose.isolated.yml" not in up[0]
    assert "guarded network verified" in proc.stdout


def test_guarded_stops_the_box_when_not_guarded(tmp_path: Path):
    proc, calls = _run(tmp_path, [], probe="NET-OPEN")
    assert proc.returncode != 0 and "NOT guarded" in proc.stderr
    assert any(" stop aiforge" in f" {c}" for c in calls)
    proc, _ = _run(tmp_path, [], guard="starting")
    assert proc.returncode != 0 and "firewall keeper" in proc.stderr


def test_isolated_is_still_there(tmp_path: Path):
    _proc, calls = _run(tmp_path, ["--isolated"])
    up = [c for c in calls if " up " in f" {c} "]
    assert up and "-f docker-compose.isolated.yml" in up[0]


def test_there_is_no_unlimited_mode(tmp_path: Path):
    for args, env in ((["--open"], {}), ([], {"AIFORGE_NETWORK": "open"})):
        proc, calls = _run(tmp_path, args, env)
        assert proc.returncode != 0 and "always limits the internet" in proc.stderr, (args, env)
        assert not [c for c in calls if " up " in f" {c} "]


def test_a_site_compose_file_still_gets_the_guard(tmp_path: Path):
    proc, calls = _run(tmp_path, [], {"COMPOSE_FILE": "site.yml"})
    up = [c for c in calls if " up " in f" {c} "]
    assert up and "-f site.yml -f docker-compose.guard.yml" in up[0]


def test_first_start_seeds_the_package_registries(tmp_path: Path):
    """The box must be able to install itself; only the registries run.sh
    installs from are seeded (once — Settings owns the list after that)."""
    proc, _ = _run(tmp_path, [], {"AIFORGE_NPM_REGISTRY": "https://npm.corp.example/api/npm/",
                                  "UV_DEFAULT_INDEX": "https://u:p@pypi.corp.example:8443/simple",
                                  "AIFORGE_EGRESS_ALLOW_HOSTS": ""})   # conftest sets one
    out = proc.stdout + proc.stderr
    assert "first start: npm.corp.example,pypi.corp.example" in out
    proc, _ = _run(tmp_path, [], {"AIFORGE_EGRESS_ALLOW_HOSTS": "only.example"})
    assert "first start: only.example" in proc.stdout + proc.stderr


def test_nothing_about_the_boundary_is_written_where_the_box_can_write(tmp_path: Path):
    """~/.aiforge is mounted read-write into the box, and the agent has full
    rights there: a proxy config or allow file in it is one the agent edits.
    A stale one from an older run.sh is removed."""
    sb = _sandbox(tmp_path)
    sb.mkdir(parents=True)
    (sb / "allow.txt").write_text(".*\n")
    (sb / "tinyproxy.conf").write_text("FilterDefaultDeny No\n")
    _run(tmp_path, ["--isolated"], {"AIFORGE_EGRESS_ALLOW_HOSTS": "artifactory.internal"})
    assert not (sb / "allow.txt").exists() and not (sb / "tinyproxy.conf").exists()


def test_the_proxy_config_is_static_and_denies_by_default():
    """An allowlist that defaults to ALLOW is not an allowlist."""
    conf = (REPO / "docker" / "isolated" / "tinyproxy.conf").read_text()
    assert "FilterDefaultDeny Yes" in conf and "FilterExtended On" in conf
    assert 'Filter "/policy/allow.txt"' in conf
    assert "ConnectPort 443" in conf
    df = (REPO / "docker" / "isolated" / "egress.Dockerfile").read_text()
    assert "COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf" in df   # baked in
    assert "FROM alpine:3.21" in df and "USER 65534" in df            # 1.11.2, not root
    compose = (REPO / "docker-compose.isolated.yml").read_text()
    assert "egress-policy:/policy:ro" in compose          # the proxy only reads it


def test_the_allowlist_owner_is_off_the_box_network():
    """The controller is on its own network, shared only with the UI front
    door; the front door refuses the box's subnet AND the proxy's address
    (else the agent loops back through the proxy) on the allowlist route."""
    compose = (REPO / "docker-compose.isolated.yml").read_text()
    ctl = compose[compose.index("  egress-ctl:"):compose.index("  model:")]
    assert "networks: [admin]" in ctl and "ports:" not in ctl
    ui = compose[compose.index("  ui:"):compose.index("networks:\n  box:")]
    assert "networks: [box, out, admin]" in ui
    egress = compose[compose.index("  egress:"):compose.index("  egress-ctl:")]
    assert "ipv4_address: ${AIFORGE_EGRESS_IP:-" in egress
    box = compose[compose.index("    container_name: aiforge\n"):compose.index("  egress:")]
    assert "egress-policy:/var/lib/aiforge-egress:ro" in box        # the box only reads it
    assert "cap_drop: [NET_RAW]" in box
    conf = (REPO / "docker" / "isolated" / "ui-proxy.conf.template").read_text()
    loc = conf[conf.index("location /egress-admin/"):conf.index("proxy_pass $egress_ctl")]
    assert "deny ${AIFORGE_BOX_SUBNET};" in loc and "deny ${AIFORGE_EGRESS_IP};" in loc


def test_the_box_network_gives_this_machine_no_address():
    """Without it the box reached every host service (ssh, the AIForge API)
    through the internal bridge's gateway."""
    compose = (REPO / "docker-compose.isolated.yml").read_text()
    assert 'com.docker.network.bridge.inhibit_ipv4: "true"' in compose


def test_the_mount_overlay_is_not_written_where_the_box_can_write(tmp_path: Path):
    """The box could swap it before `up` (a privileged container, the host's
    / mounted) or plant its .tmp as a symlink to a host file."""
    xdg = tmp_path / "xdg"
    shared = tmp_path / "shared"
    shared.mkdir()
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "mounts.list").write_text(str(shared) + "\n")
    (xdg / "aiforge").mkdir(parents=True)
    (xdg / "aiforge" / "approved-mounts").write_text(str(shared) + "\n")
    _proc, calls = _run(tmp_path, [], {"XDG_CONFIG_HOME": str(xdg)})
    up = [c for c in calls if " up " in f" {c} "][0]
    assert str(xdg / "aiforge" / "mounts.compose.yml") in up
    assert not (cfg / ".sandbox" / "mounts.compose.yml").exists()


def test_the_model_url_is_parsed_for_the_relay(tmp_path: Path):
    cases = {
        "https://user:pw@api.example.com/v1": "api.example.com:443",
        "http://[::1]:1234/v1": "host.docker.internal:1234",
        "http://[fd00::5]:8080/v1": "[fd00::5]:8080",
        "http://studio.lan/v1": "studio.lan:80",
    }
    for url, want in cases.items():
        proc, _ = _run(tmp_path, ["--isolated"], {"AIFORGE_LM_BASE_URL": url})
        assert f"model via relay -> {want}" in proc.stdout + proc.stderr, url


def test_the_proxy_waits_for_its_list_and_follows_changes():
    sh = (REPO / "docker" / "isolated" / "egress-proxy.sh").read_text()
    assert 'while [ ! -f "$ALLOW" ]' in sh and "kill -USR1" in sh
    compose = (REPO / "docker-compose.isolated.yml").read_text()
    assert 'pid: "service:egress"' not in compose     # no shared namespace (start-order deadlock)


def test_the_model_goes_through_its_own_relay(tmp_path: Path):
    """The model client does not use the proxy; it reaches a relay on the box
    network that forwards to the real endpoint only. A loopback endpoint is
    this machine's: the relay reaches it as host.docker.internal."""
    proc, calls = _run(tmp_path, ["--isolated"],
                       {"AIFORGE_LM_BASE_URL": "http://127.0.0.1:1234/v1"})
    out = proc.stdout + proc.stderr
    assert "model via relay -> host.docker.internal:1234" in out
    proc, _ = _run(tmp_path, ["--isolated"],
                   {"AIFORGE_LM_BASE_URL": "http://192.168.70.185:8081/v1"})
    assert "model via relay -> 192.168.70.185:8081" in proc.stdout + proc.stderr
    compose = (REPO / "docker-compose.isolated.yml").read_text()
    assert "AIFORGE_LM_BASE_URL=${AIFORGE_BOX_LM_URL" in compose
    assert "host.docker.internal:host-gateway" not in compose.split("  egress:")[0]


def test_the_ui_is_published_through_its_own_proxy():
    """A container on an internal network cannot publish a port itself, so the
    UI is served by an nginx that sits on both networks.

    Its config varies with nothing, so it is a file in the repo rather than
    something run.sh echoes out at start-up — compose mounts it directly."""
    conf = (REPO / "docker" / "isolated" / "ui-proxy.conf.template").read_text()
    assert "set $box http://aiforge:8799;" in conf and "proxy_pass $box;" in conf
    # SSE: the chat streams, so buffering would hold every token to the end.
    assert "proxy_buffering off;" in conf


def test_the_ui_image_carries_that_config():
    """The file is only useful if the ui container actually gets it."""
    df = (REPO / "docker" / "isolated" / "ui.Dockerfile").read_text()
    assert "COPY ui-proxy.conf.template /etc/nginx/templates/default.conf.template" in df
    assert "nginx-unprivileged" in df


def test_isolated_refuses_to_fight_a_site_compose_file(tmp_path: Path):
    proc, _calls = _run(tmp_path, ["--isolated"],
                        {"COMPOSE_FILE": "/tmp/site.yml"})
    assert proc.returncode != 0
    assert "cannot be combined" in (proc.stderr + proc.stdout)


def test_boundary_containers_are_unprivileged_and_hardened():
    import re
    compose = (REPO / "docker-compose.isolated.yml").read_text()
    for svc in ("egress", "egress-ctl", "ssh-egress", "model"):
        block = compose[compose.index(f"  {svc}:\n"):]
        block = block[:block.index("\n\n")]
        for want in ('user: "65534:65534"', "cap_drop: [ALL]", "no-new-privileges:true",
                     "read_only: true"):
            assert want in block, (svc, want)
    assert compose.count("enable_ipv6: false") == 3
    assert "net.ipv6.conf.all.disable_ipv6: 1" in compose


def test_isolated_refuses_a_checkout_the_box_can_see(tmp_path: Path):
    """--repos pointing at a folder that contains this checkout would let the
    agent edit the isolation's own files."""
    proc, calls = _run(tmp_path, ["--isolated"], {"AIFORGE_REPOS_DIR": str(tmp_path)})
    assert proc.returncode != 0
    assert "would see its own boundary" in proc.stderr
    assert not [c for c in calls if " up " in f" {c} "]
    proc, calls = _run(tmp_path, [], {"AIFORGE_REPOS_DIR": str(tmp_path)})
    assert "the box can edit this checkout" in proc.stderr          # guarded mode: warns
    assert [c for c in calls if " up " in f" {c} "]


def test_isolated_stops_when_the_network_is_not_what_it_assumes(tmp_path: Path):
    proc, calls = _run(tmp_path, ["--isolated"], inhibit="false")
    assert proc.returncode != 0 and "NOT isolated" in proc.stderr
    assert "gives this machine an address" in proc.stderr
    assert any(c.startswith("compose") and " down" in f" {c}" for c in calls) or \
        any(" down" in f" {c} " for c in calls)
    proc, _ = _run(tmp_path, ["--isolated"], getent=0)
    assert proc.returncode != 0 and "resolves internet names" in proc.stderr
    proc, _ = _run(tmp_path, ["--isolated"])
    assert proc.returncode == 0 and "isolated network verified" in proc.stdout


def test_the_relay_listens_unprivileged(tmp_path: Path):
    proc, _ = _run(tmp_path, ["--isolated"], {"AIFORGE_LM_BASE_URL": "HTTPS://api.example.com/v1?x=1"})
    out = proc.stdout + proc.stderr
    assert "model via relay -> api.example.com:443" in out


def test_ssh_goes_out_through_its_own_exit():
    compose = (REPO / "docker-compose.isolated.yml").read_text()
    ssh = compose[compose.index("  ssh-egress:"):compose.index("  model:")]
    assert "networks: [box, out]" in ssh and "AIFORGE_SSH_EGRESS=${AIFORGE_SSH_EGRESS:-allowlist}" in ssh
    entry = (REPO / "docker" / "entrypoint.sh").read_text()
    assert "ProxyCommand $_py /opt/aiforge-src/docker/ssh-connect.py %h %p" in entry
    assert '_py="$(command -v python3' in entry


def test_the_check_fails_closed_when_the_box_is_not_running(tmp_path: Path):
    """No answer from the box is NOT 'isolated'."""
    proc, _ = _run(tmp_path, ["--isolated"], getent=2, box_up=False)
    assert proc.returncode != 0 and "isolation could not be checked" in proc.stderr


def test_lan_names_are_added_when_the_ui_is_open_to_the_lan(tmp_path: Path):
    proc, _ = _run(tmp_path, ["--isolated", "--host", "0.0.0.0"],
                   {"AIFORGE_ALLOW_UNAUTH_NONLOOPBACK": "1"})
    assert "allowlist can be edited from the UI opened as localhost,127.0.0.1,::1," in proc.stdout


def test_the_model_set_in_settings_feeds_the_relay_and_the_seed(tmp_path: Path):
    """After an upgrade the box must keep the model chosen in Settings: the
    relay follows the stored chat model (its port is the box's loopback
    forward), and model hosts not on this machine are allowlisted."""
    sec = tmp_path / "cfg" / "security"
    sec.mkdir(parents=True)
    (sec / "agent_config.json").write_text(json.dumps({
        "chat": {"base_url": "http://127.0.0.1:11434/v1"},
        "planner": {"base_url": "https://api.remote-llm.example/v1"},
        "doer": {"base_url": "http://127.0.0.1:9999/v1"}}))
    proc, _ = _run(tmp_path, ["--isolated"], {"AIFORGE_EGRESS_ALLOW_HOSTS": "",
                                  "AIFORGE_ADMIN_URL": "http://admin.corp.example:8799"})
    out = proc.stdout + proc.stderr
    assert "model via relay -> host.docker.internal:11434" in out
    assert "api.remote-llm.example" in out and "admin.corp.example" in out
    assert "model http://127.0.0.1:9999/v1 is on this machine on another port" in out


def test_one_offs_get_their_way_out(tmp_path: Path):
    _proc, calls = _run(tmp_path, ["--test"])
    runs = [c for c in calls if c.startswith("compose") and " run " in f" {c} "]
    assert runs and "--no-deps" not in runs[-1]


def test_stop_is_quiet(tmp_path: Path):
    proc, _ = _run(tmp_path, ["--stop"])
    assert "model via relay" not in proc.stdout + proc.stderr


def test_an_unknown_network_value_is_reported(tmp_path: Path):
    proc, calls = _run(tmp_path, [], {"AIFORGE_NETWORK": "isolate"})
    assert "is not 'guarded' or 'isolated'" in proc.stderr
    up = [c for c in calls if " up " in f" {c} "]
    assert up and "docker-compose.guard.yml" in up[0]


def test_a_stale_ISOLATED_in_the_environment_is_ignored(tmp_path: Path):
    _proc, calls = _run(tmp_path, [], {"ISOLATED": "true"})
    up = [c for c in calls if " up " in f" {c} "]
    assert up and "docker-compose.guard.yml" in up[0]


def test_the_guard_overlay_keeps_the_host_network_and_gates_the_internet():
    g = (REPO / "docker-compose.guard.yml").read_text()
    assert "network_mode: host" in g and "cap_add: [NET_ADMIN, NET_RAW]" in g
    assert "/var/run/docker.sock:/var/run/docker.sock:ro" in g
    assert "no_proxy=localhost,127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16" in g
    assert "AIFORGE_SSH_DIRECT_PRIVATE=1" in g
    sh = (REPO / "docker" / "isolated" / "egress-guard.sh").read_text()
    assert "-m cgroup --path" in sh and "ip6tables" in sh
    for net in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"):
        assert net in sh


def test_guarded_hardening_is_in_place():
    g = (REPO / "docker-compose.guard.yml").read_text()
    box = g[g.index("\n  aiforge:\n"):g.index("\n  egress-guard:\n")]
    assert "cap_drop: [NET_RAW]" in box                        # no packet sockets
    assert "cgroup: host" in g and "/etc/resolv.conf:/host/resolv.conf:ro" in g
    sh = (REPO / "docker" / "isolated" / "egress-guard.sh").read_text()
    assert "--ctdir REPLY -j RETURN" in sh                     # replies to outside clients
    assert "169.254.169.254/32" in sh                          # cloud metadata refused
    assert "stat -c %i" in sh                                  # re-installed after a restart
    assert "chain_ok" in sh and "keeping its rules" in sh
    entry = (REPO / "docker" / "entrypoint.sh").read_text()
    assert 'AIFORGE_GUARDED:-0}" == 1' in entry and "not starting" in entry


def test_the_allowlist_route_needs_the_api_token():
    from aiforge_core.api import _bind_security as B
    assert B._auth_exempt("/egress-admin/api/hosts") is False
