# AIForge — Demo Guide

Copy-paste prompts. Same text every run. 6 scenarios.

**Env:** NUC `http://<host>:8799` (UI). Model: `qwen/qwen3-coder-next` (single, local).
**Legend:** ✅ tested & working · ⚙️ needs one-time integration setup first (see §0).

---

## §0 — Pre-flight (do ONCE before the demo)

| Capability | Needed for scenarios | Where to configure |
|---|---|---|
| Jira | 2, 4, 5, 6 | UI → **Settings → Integrations → Jira** (Base URL + Personal Access Token) → **Test** |
| Confluence | 3, 4, 6 | UI → **Settings → Integrations → Confluence** (Base URL + token) → **Test** |
| Email (SMTP) | 4 | UI → **Settings → Integrations → Email** (SMTP host/port/user/pass/from) → **Test** |
| Git remote / GitLab | 5, 6 | UI → **Settings → Integrations → GitLab** (Base URL + token) — optional; local git works without it |

> On this NUC **none are configured yet** (all blank). Scenarios **1 and 5-rules are fully working now**; **2, 3, 4, 6** light up the moment their integration test passes. Each tool degrades cleanly — an unconfigured tool returns `*_not_configured` with a hint, never a crash.

**Model ready (check first):** the chat model picker should show **qwen/qwen3-coder-next · active** (or the Home page shows the endpoint green). The `lms-ensure` timer keeps it loaded and auto-reloads within ~10 min if it drops — but confirm it's active before you start, or the first prompt returns *"the model didn't respond."*

**Demo hygiene:** start each scenario in a **fresh chat** (New chat) so history is clean. Pick the mode named in each step (Simple / Plan / Team).

---

## §1 — Chat memory ✅ (tested)

**Point:** AIForge remembers project rules across sessions — teach once, it applies forever, even in a brand-new chat.

**Step 1 — teach (New chat, Simple mode). Type:**
```
Remember this project rule: we always mock outbound HTTP in tests with the responses library, never real network calls.
```
Expect: `Got it — saved as rule (project).`

**Step 2 — open a NEW chat (fresh session), Simple mode. Type:**
```
How should I mock an outbound HTTP call in a new test for this project?
```
Expect: it answers using the **`responses`** library (imports `responses`, `RequestsMock()`), citing the remembered rule — **without being told again**. That cross-session recall is the wow moment.

*(Optional follow-up to show it's durable: ask the same in Plan or Team mode — same rule surfaces.)*

---

## §2 — Jira ⚙️ (needs Jira configured)

**Point:** chat reads & acts on Jira without leaving AIForge.

**New chat, Simple mode. Type (swap PROJ for your project key):**
```
Show me the open Jira tickets in project PROJ assigned to me, and summarize the top 3 by priority.
```
Expect: a table/summary of live Jira issues (key, summary, priority, status).

**Follow-up (write action):**
```
Add a comment to PROJ-42 saying "Picked up — starting today, ETA end of week."
```
Expect: confirmation with the comment link. *(Unconfigured → `jira_not_configured: set JIRA_BASE_URL + JIRA_TOKEN`.)*

---

## §3 — Confluence ⚙️ (needs Confluence configured)

**Point:** read/search Confluence and generate pages with an approval preview.

**New chat, Simple mode. Type:**
```
Search Confluence for our "deployment runbook" and give me the 5 key steps.
```
Expect: page content summarized.

**Create-with-preview:**
```
Draft a Confluence page in space DOCS titled "AIForge Demo Notes" summarizing what AIForge can do, and show me a markdown preview before publishing.
```
Expect: a **markdown preview + approval gate** (Approve / Reject). On Approve → page created with link.

---

## §4 — Job: email alert on Jira/Confluence ⚙️ (needs Jira/Confluence + Email)

**Point:** a scheduled job watches Jira/Confluence and emails you on a match.

**New chat — click the "Job" builder. Type:**
```
Create a job that runs every 30 minutes: check Jira project PROJ for new tickets labeled "urgent" OR any Confluence page in space DOCS that mentions "@me", and email a summary to me@company.com. Test it once before saving.
```
Expect: the job builder **gathers requirements, runs a dry test, shows the sample email**, then asks to save. On save it appears in **Jobs** and runs on schedule.

*(Talking point: job = trigger + condition + action, all from one sentence; the dry-run-before-save is the safety story.)*

---

## §5 — Skills / Rules / Workflow for Git MR ✅ (rule tested)

**Point:** teach AIForge your git conventions once; it enforces branch names + commit messages tied to the Jira id, end-to-end.

**Step 1 — create the rule (New chat → "Rule" builder). Type:**
```
Create a GLOBAL rule for all repos: git branch names must follow feature/<JIRA-ID>-<short-desc>, and every commit message must start with [<JIRA-ID>]. Name the rule git-branch-commit-convention.
```
Then confirm:
```
Yes, create it globally exactly as described.
```
Expect: `✅ Global rule created: git-branch-commit-convention`. It now appears in **Library → Rules**.

**Step 2 — use it (New chat, Simple mode). Type:**
```
I am starting work on Jira ticket PROJ-42, a login page bug. Per our team rules, what git branch name and first commit message should I use?
```
Expect: `feature/PROJ-42-login-page-bug` + a commit starting `[PROJ-42]` — the rule applied automatically.

**Step 3 (optional, full pipeline — Team mode, in a real repo):**
```
On this repo, fix the off-by-one in pagination for PROJ-42, following our git conventions, and open a merge request.
```
Expect: Team pipeline creates branch `feature/PROJ-42-...`, commits `[PROJ-42] ...`, and (with GitLab configured) opens the MR. *Workflow builder analog: build a reusable "git-mr" workflow the same way via the "Workflow" builder.*

---

## §6 — Create a repo + Confluence page ⚙️ (needs Confluence, + Git/GitLab for remote)

**Point:** scaffold a new service and document it in Confluence in one shot.

**New chat, Team mode. Type:**
```
Scaffold a new Python FastAPI service called "billing-api" with a health endpoint and a test, initialize a git repo, and create a Confluence page in space DOCS titled "billing-api — Service Overview" describing its purpose, endpoints, and how to run it.
```
Expect: files scaffolded + committed, tests pass, and a Confluence page created (approval preview first). With GitLab configured it also pushes the remote.

---

## Quick reference — exact prompts (copy block)

```
[§1a] Remember this project rule: we always mock outbound HTTP in tests with the responses library, never real network calls.
[§1b] How should I mock an outbound HTTP call in a new test for this project?
[§2 ] Show me the open Jira tickets in project PROJ assigned to me, and summarize the top 3 by priority.
[§3 ] Draft a Confluence page in space DOCS titled "AIForge Demo Notes" summarizing what AIForge can do, and show me a markdown preview before publishing.
[§4 ] Create a job that runs every 30 minutes: check Jira project PROJ for new tickets labeled "urgent" OR any Confluence page in space DOCS that mentions "@me", and email a summary to me@company.com. Test it once before saving.
[§5a] Create a GLOBAL rule for all repos: git branch names must follow feature/<JIRA-ID>-<short-desc>, and every commit message must start with [<JIRA-ID>]. Name the rule git-branch-commit-convention.
[§5b] I am starting work on Jira ticket PROJ-42, a login page bug. Per our team rules, what git branch name and first commit message should I use?
[§6 ] Scaffold a new Python FastAPI service called "billing-api" with a health endpoint and a test, initialize a git repo, and create a Confluence page in space DOCS titled "billing-api — Service Overview" describing its purpose, endpoints, and how to run it.
```

---

## Test status (as of prep)

| # | Scenario | Status |
|---|---|---|
| 1 | Chat memory | ✅ verified end-to-end (teach → fresh-session recall using `responses`) |
| 2 | Jira | ⚙️ code path present; needs Jira creds to run live |
| 3 | Confluence | ⚙️ code path present (approval preview built); needs creds |
| 4 | Job + email alert | ⚙️ job builder works; needs Jira/Confluence + SMTP |
| 5 | Skills/Rules/Workflow (git MR) | ✅ rule create → **Library → Rules** verified. Tip: ask §5b in a **fresh chat with a non-code working dir** so it answers the naming question instead of treating it as a coding task. |
| 6 | Repo + Confluence page | ⚙️ scaffold+git works; Confluence step needs creds |
