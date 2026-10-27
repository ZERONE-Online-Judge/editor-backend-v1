# Team folder revision history

The team folder's **리비전** tab collects existing shared commits from the
currently selected folder and its descendants. Each entry shows the problem,
revision, author, commit message, time, and added/modified/deleted files or
settings. Records are paginated ten at a time. Opening an entry shows a compact
diff; **전체 변경 보기** opens the existing full document comparison.

The update notice above a problem workspace shows commits since the working
copy's base revision and a cumulative diff to the announced revision. Reading
the preview never saves, commits, merges, or replaces a working copy. The
existing Update action retains its save, merge, and conflict-resolution flow.

## Endpoints and access

- `GET /api/folders/{id}/revisions?page=1&page_size=10` requires current team
  folder membership. It includes only non-deleted problems currently located
  in that subtree. Moving a problem out of the team removes it from that
  team's history, even if some members retain a separate direct problem grant.
- `GET /api/problems/{id}/revision-preview?from_revision=1&to_revision=2`
  requires current problem access. Both snapshots come from committed
  revisions. Use `from_revision=-1&to_revision=0` for an initial revision.
  Intermediate commits are listed newest first; the diff compares the two
  endpoints, so reverted changes may have no final diff.

The preview includes at most five commit messages and three changed items.
Each blob read is bounded to 32 KiB, each comparison to 400 source lines, and
each rendered diff to nine lines of at most 240 characters. Binary files,
missing files, and truncated content are explicitly indicated. Blob references
must belong to the authorized problem. The full comparison and existing
revision downloads remain available for complete review. All API responses
use the application's existing no-store policy. No new snapshots or schema
migration are needed; historical commits are immediately included.
