# Edit ticket description + attachments (web UI)

**Date:** 2026-05-31
**Status:** approved

## Problem

Ticket body and attached files are only settable at create time (POST
`/api/tickets`). The detail view (`web/src/views/TicketDetail.tsx`)
renders both read-only. Operators cannot fix a description or add/remove
files after a ticket exists.

## Scope

In: edit `body` (description) + add/remove `attached_files` from the
detail view. Out: title edit (not requested), external_refs edit.

## Decisions (user-confirmed)

- Edit allowed on **any** status (even in_progress; will not affect an
  already-running agent — acceptable).
- File **add + remove** (remove deletes from disk + metadata).
- Adding files post-create **forces** `force_provider=claude_local`
  (matches create), since attachments are only readable via the Claude
  CLI's native FS tools.

## Backend — `aiforge_core/api/api.py`

`TicketPatch` gains:
- `attached_files: list[AttachedFile] = []` — new uploads (base64).
- `remove_files: list[str] = []` — names to delete.

New helper `_remove_ticket_attachments(identifier, names) -> list[str]`:
mirrors `_persist_ticket_attachments` path resolution; strips `../`
components; unlinks `{root}/.aiforge/ticket-files/{id}/<name>`; returns
names actually removed. Missing file = no-op.

`patch_ticket` file handling (runs before metadata merge so the
recomputed list lands in the jsonb merge):
1. `current = list(t.metadata.get("attached_files", []))`
2. If `remove_files`: unlink each from disk, drop matching entries from
   `current` (match on `name`).
3. If `attached_files`: `_persist_ticket_attachments(...)` → append meta
   to `current`.
4. If either op ran: set `merge_md["attached_files"] = current`; if
   `current` non-empty → `merge_md["force_provider"] = "claude_local"`,
   else `merge_md["force_provider"] = None` (cleared).

jsonb `||` shallow-merge replaces the whole `attached_files` key —
already the existing semantics — so passing the full recomputed list is
correct for both add and remove.

## Frontend — `web/src/views/TicketDetail.tsx`

- **Body card**: Edit button → textarea + Save/Cancel. Save →
  `api.patch(id, { body })` → invalidate `['ticket', id]`.
- **Attachments card**: per-file `×` remove toggle (staged, not applied
  until Save) + DropZone to add. Save →
  `api.patch(id, { attached_files: <new b64[]>, remove_files: <names[]> })`.
- Body and attachment edits are independent flows (separate Save).

## Shared extraction — `web/src/components/FileUpload.tsx`

Move `DropZone`, `readAsBase64`, `MAX_FILE_BYTES`, `formatBytes` out of
`Tickets.tsx` into this module; import in both `Tickets.tsx` and
`TicketDetail.tsx`. Removes duplication.

## Testing

- Backend: extend `aiforge_core` ticket API tests — PATCH adds a file
  (asserts disk write + metadata + force_provider), PATCH removes a file
  (asserts disk unlink + metadata drop), PATCH body unchanged-path still
  works.
- Frontend: typecheck/build (`yarn build` in `web/`).
