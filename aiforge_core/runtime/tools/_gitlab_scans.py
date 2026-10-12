"""GitLab pipelines: recognising the Sonar and Black Duck jobs, and reading
what they found.

A pipeline's job list says a scan job passed or failed; what the scan found
lives on the scan server. This reads the job logs for the marks the scanners
leave, and asks the configured server for the result behind them.

A scan job is recognised by its LOG, not by its name. Teams call the job
anything (``quality``, ``verify``, ``sec-check``), but the scanner prints the
same lines whatever the job is called. The name only decides which logs are
read first.

A job log is untrusted text of up to a few megabytes. Every pattern here runs
over a handful of short lines picked out by a plain substring test, never
over the whole log — an unbounded match over one long line is quadratic.

Not covered: a scan job inside a triggered CHILD pipeline (read that pipeline
itself), and a scanner that prints nothing recognisable (Gradle's Sonar task
without ``--info``).
"""
from __future__ import annotations

import os
import re
import threading
import urllib.parse

# Finished jobs whose log is read to look for a scanner, on top of the failed
# jobs (whose logs are in hand anyway). Each is one HTTP round trip.
_PROBE_JOBS = 6
# Rows listed per scan inside a pipeline answer. The scan tools give more.
_ROWS = 10
# Lines kept per mark, and how much of each. The scanners' own lines are short.
_KEEP_LINES = 40
_LINE_WIDTH = 1000

# Job or stage names, for ORDER only. A name that names the scanner goes
# first; a name that only sounds like a check goes next. GitLab's own
# templates (secret_detection, container_scanning) land in the second group.
_NAME_STRONG = re.compile(
    r"sonar|black[-_ ]?duck|synopsys|quality[-_ ]?gate|detect(?!ion)|"
    r"(?<![a-z])(?:sca|bd|hub)(?![a-z])", re.I)
_NAME_WEAK = re.compile(r"quality|scan|secur|analy[sz]|compliance|licen[cs]e",
                        re.I)

_URL = r"https?://[^\s\"'<>\x1b]{1,400}?"
_TAIL = r"[^\s\"'<>\x1b]{1,400}"
_SONAR_TASK = re.compile(_URL + r"/api/ce/task\?id=[\w-]{1,80}")
_SONAR_DASH = re.compile(_URL + r"/dashboard\?id=" + _TAIL)
# The query of a dashboard link, readable even when GitLab masked the host.
_SONAR_QUERY = re.compile(r"/dashboard\?(id=" + _TAIL + ")")
_SONAR_MARKS = ("SonarScanner", "sonar-scanner", "SonarQube Scanner",
                "sonar-maven-plugin", "sonar:sonar", "Task :sonar",
                "QUALITY GATE STATUS", "ANALYSIS SUCCESSFUL",
                "sonar.projectKey")
_SONAR_KEY = re.compile(
    r"(?:Project key|sonar\.projectKey)[ \t]*[:=][ \t]*([\w.:\-${}]{1,200})")
_SONAR_PR = re.compile(r"Pull request (\S{1,80}) for merge into", re.I)
_SONAR_BRANCH = re.compile(r"Branch name:[ \t]*([^\s,]{1,200})")
_SONAR_GATE = re.compile(r"QUALITY GATE STATUS:[ \t]*(\w{1,40})")

_BD_ID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
_BD_LINK = re.compile(
    _URL + rf"/projects/{_BD_ID}/versions/{_BD_ID}(?:/components)?")
_BD_MARKS = ("Black Duck", "Synopsys Detect", "synopsys-detect",
             "blackduck/detect", "Detect Version", "FAILURE_POLICY_VIOLATION",
             "--blackduck.url", "detect.synopsys.com")
_BD_SCRIPT = re.compile(r"(?<![A-Za-z])detect\d{0,3}\.(?:sh|ps1)")
_BD_PROJECT = re.compile(r"Project name:[ \t]*(\S[^\n]*?)[ \t]*$", re.M)
_BD_VERSION = re.compile(r"Project version:[ \t]*(\S[^\n]*?)[ \t]*$", re.M)
_BD_STATUS = re.compile(r"Overall Status:[ \t]*([A-Z_]{1,60})")


def _lines(text: str, *needles: str) -> str:
    """The last few lines of ``text`` that contain one of ``needles``, each
    cut to a sane width. What the patterns are allowed to see."""
    hits = [line[:_LINE_WIDTH] for line in text.splitlines()
            if any(n in line for n in needles)]
    return "\n".join(hits[-_KEEP_LINES:])


def _last(pattern: re.Pattern, text: str) -> str:
    """The LAST match — a retried scanner prints its lines again, and the last
    set is the one the job ended on."""
    found = pattern.findall(text)
    return str(found[-1]).strip() if found else ""


def _clean_link(link: str) -> str:
    return link.rstrip(".,;:)]}")


def _named(pattern: re.Pattern, text: str) -> str:
    """A name the scanner printed. A value with ``$`` in it is the command as
    GitLab echoed it, before the shell filled the variable in."""
    value = _last(pattern, text).strip("'\"")
    return "" if "$" in value else value


def _sonar_query(text: str) -> dict:
    """Project, branch and pull request from the last dashboard link."""
    raw = _clean_link(_last(_SONAR_QUERY, text))
    if "$" in raw:
        # The command as GitLab echoed it, not a link the scanner printed.
        return {}
    try:
        query = urllib.parse.parse_qs(raw)
    except ValueError:
        return {}
    return {name: (query.get(key) or [""])[0]
            for name, key in (("project", "id"), ("branch", "branch"),
                              ("pull_request", "pullRequest"))}


def find_sonar(text: str) -> dict:
    """What a job log says about a Sonar scan, or ``{}`` when it ran none.

    ``ran`` is true when the scanner got as far as printing where its result
    is — as opposed to a log that only mentions Sonar."""
    links = _lines(text, "/api/ce/task?id=", "/dashboard?id=")
    task, dash = _last(_SONAR_TASK, links), _last(_SONAR_DASH, links)
    marked = any(mark in text for mark in _SONAR_MARKS)
    if not (links or marked):
        return {}
    query = _sonar_query(links)
    names = _lines(text, "Project key", "sonar.projectKey", "Branch name:",
                   "Pull request ", "QUALITY GATE STATUS")
    return {"tool": "sonar",
            "ran": bool(task or dash or query.get("project")),
            "link": _clean_link(task or dash),
            "project": query.get("project") or _named(_SONAR_KEY, names),
            "branch": query.get("branch") or _last(_SONAR_BRANCH, names),
            "pull_request": (query.get("pull_request")
                             or _last(_SONAR_PR, names)),
            "log_verdict": _last(_SONAR_GATE, names)}


def find_blackduck(text: str) -> dict:
    """What a job log says about a Black Duck scan, or ``{}``."""
    link = _last(_BD_LINK, _lines(text, "/versions/"))
    marked = (any(mark in text for mark in _BD_MARKS)
              or bool(_BD_SCRIPT.search(_lines(text, "detect"))))
    if not (link or marked):
        return {}
    names = _lines(text, "Project name:", "Project version:",
                   "Overall Status:")
    status = _last(_BD_STATUS, names)
    return {"tool": "blackduck", "ran": bool(link or status),
            "link": _clean_link(link),
            "project": _named(_BD_PROJECT, names),
            "version": _named(_BD_VERSION, names),
            "log_verdict": status}


def find_scans(text: str) -> list:
    """Every scanner a job log shows."""
    return [f for f in (find_sonar(text or ""), find_blackduck(text or "")) if f]


# ─────────────────────────── reading the result ───────────────────────────

_BY_NAME = ("looked up by the project name in the job log, not by a link to "
            "this run's result — it is the server's LATEST result for that "
            "name (its default branch, unless a branch or pull request is "
            "shown) and may be older than this pipeline")


def _sonar_result(found: dict) -> dict:
    from . import sonar
    if not sonar._configured():
        return {"note": "Sonar is not set up on this install (Settings → "
                        "Integrations → Sonar), so the result was not read"}
    by_name = {"limit": _ROWS, "project": found["project"],
               "branch": found["branch"],
               "pull_request": found["pull_request"]}
    if found["link"] and sonar.on_our_server(found["link"]):
        report = sonar.sonar_report({"limit": _ROWS, "url": found["link"]})
        denied = str(report.get("error") or "").startswith(("http 403",
                                                            "http 404"))
        if not (denied and found["project"]):
            return _result(report)
        # The scanner's task link needs more than "Browse" on some servers.
        # The project and branch the log named are enough to read the result.
        return _result(sonar.sonar_report(by_name), _BY_NAME)
    if found["project"]:
        note = _BY_NAME
        if found["link"]:
            note = (f"the link in the log is on another host than the "
                    f"configured Sonar ({sonar._base()}) and was not opened; "
                    + note)
        return _result(sonar.sonar_report(by_name), note)
    if found["link"]:
        return {"note": "the link in the log is not on the configured Sonar "
                        f"server ({sonar._base()}), so it was not opened"}
    return {"note": "the log shows a Sonar scan but no link or project "
                    "key to look the result up by"}


def _blackduck_result(found: dict) -> dict:
    from . import blackduck
    if not blackduck._configured():
        return {"note": "Black Duck is not set up on this install (Settings "
                        "→ Integrations → Black Duck), so the result was not "
                        "read"}
    if found["link"] and blackduck.on_our_server(found["link"]):
        return _result(blackduck.blackduck_report(
            {"limit": _ROWS, "url": found["link"]}))
    if found["project"]:
        note = _BY_NAME
        if found["link"]:
            note = (f"the link in the log is on another host than the "
                    f"configured Black Duck ({blackduck._base()}) and was not "
                    "opened; " + note)
        return _result(blackduck.blackduck_report(
            {"limit": _ROWS, "project": found["project"],
             "version": found["version"]}), note)
    if found["link"]:
        return {"note": "the link in the log is not on the configured Black "
                        f"Duck server ({blackduck._base()}), so it was not "
                        "opened"}
    return {"note": "the log shows a Black Duck scan but no link or "
                    "project name to look the result up by"}


def _result(report: dict, note: str = "") -> dict:
    """A scan report as one entry of a pipeline answer."""
    if report.get("ok"):
        out = {"result": {k: v for k, v in report.items() if k != "ok"}}
        if note:
            out["note"] = note
        return out
    out = {"error": report.get("error")}
    for key in ("hint", "versions", "similar"):
        if report.get(key):
            out[key] = report[key]
    return out


_READERS = {"sonar": _sonar_result, "blackduck": _blackduck_result}


def _read(found: dict) -> dict:
    """One scanner's result. Its failure is that entry's, not the pipeline's
    and not the other scanner's."""
    try:
        return _READERS[found["tool"]](found)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not read the result: {str(exc)[:200]}"}


def _configured_tools() -> set:
    have = set()
    from . import blackduck, sonar
    if sonar._configured():
        have.add("sonar")
    if blackduck._configured():
        have.add("blackduck")
    return have


def _probe_budget() -> int:
    try:
        return max(0, int(os.environ.get("GITLAB_SCAN_PROBE_JOBS", _PROBE_JOBS)))
    except (TypeError, ValueError):
        return _PROBE_JOBS


def _job_id_key(job: dict):
    job_id = job.get("id")
    return job_id if isinstance(job_id, int) else 0


def jobs_to_probe(jobs: list, already: set) -> list:
    """Finished jobs worth reading for a scanner, most likely first: a name
    or stage that names the scanner, then one that sounds like a check, then
    the rest — within each group the latest job first (a scan runs late)."""
    done = [j for j in jobs
            if j.get("id") is not None and j.get("id") not in already
            and str(j.get("status")).lower() in ("success", "failed")]
    done.sort(key=_job_id_key, reverse=True)

    def rank(job: dict) -> int:
        label = f"{job.get('name')} {job.get('stage')}"
        if _NAME_STRONG.search(label):
            return 0
        return 1 if _NAME_WEAK.search(label) else 2

    return sorted(done, key=rank)[:_probe_budget()]


# What a FINISHED job's log showed, kept only when it showed a scanner that
# ran. That log no longer changes, so a second read of the same pipeline (the
# watch's last look, a follow-up question) does not fetch it again. A log
# with nothing in it is NOT kept: read a moment after the job ends it can be
# half flushed, and remembering that would hide the scan for good.
# Keyed by project and job id; bounded.
_seen: dict = {}
_seen_lock = threading.Lock()
_SEEN_MAX = 512


def _found_in(proj, job_id, read_log) -> list:
    key = (str(proj), job_id)
    with _seen_lock:
        if key in _seen:
            return _seen[key]
    text = read_log(job_id)
    found = find_scans(text)
    if found and all(_strength(f) == 2 for f in found):
        with _seen_lock:
            if len(_seen) >= _SEEN_MAX:
                _seen.clear()
            _seen[key] = found
    return found


def _strength(found: dict) -> int:
    """How well a log pins the scan down: 2 the scanner ran and said where
    its result is, 1 a project name to look it up by, 0 only a mention."""
    if found.get("ran"):
        return 2
    return 1 if found.get("project") else 0


def collect(proj, jobs: list, texts: dict, read_log) -> list:
    """The scans of one pipeline, one entry per scanner.

    ``texts`` maps job id → cleaned log for the logs already fetched (the
    failed jobs). ``read_log(job_id)`` fetches one more. Other finished jobs
    are read only while a scan server is configured and no log has shown its
    scanner running yet — an install with neither pays for nothing extra.

    Several jobs can mention one scanner (a build job that writes
    ``sonar.projectKey`` into a properties file, then the scan job itself).
    The log that pins the result down best is the one that counts; between
    equals, the first read.
    """
    best: dict = {}
    ran_in: dict = {}

    def look(job_id, founds: list) -> None:
        for found in founds:
            if _strength(found) == 2:
                ran_in.setdefault(found["tool"], []).append(job_id)
            held = best.get(found["tool"])
            if held is None or _strength(found) > _strength(held[1]):
                best[found["tool"]] = (job_id, found)

    for job_id, text in texts.items():
        look(job_id, find_scans(text))
    wanted = _configured_tools()
    for job in jobs_to_probe(jobs, set(texts)):
        if all(t in best and _strength(best[t][1]) == 2 for t in wanted):
            break
        look(job.get("id"), _found_in(proj, job.get("id"), read_log))
    by_id = {j.get("id"): j for j in jobs}
    scans = []
    for tool in sorted(best):
        job_id, found = best[tool]
        job = by_id.get(job_id) or {}
        entry = {"tool": tool, "job": job.get("name"),
                 "job_status": job.get("status")}
        for key in ("link", "log_verdict"):
            if found.get(key):
                entry[key] = found[key]
        entry.update(_read(found))
        others = [by_id.get(i, {}).get("name") for i in ran_in.get(tool, [])
                  if i != job_id]
        if others:
            # One result per scanner is read. A second scan is named, not
            # dropped: its gate may be the red one.
            entry["also_ran_in_jobs"] = others
        scans.append(entry)
    return scans
