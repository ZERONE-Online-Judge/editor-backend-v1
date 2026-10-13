# Problem authoring workflow

Source files retain the existing `files` / `solutions` package format.
Optional `roles` metadata identifies checker, validator, generator and
interactor programs. Empty role metadata is omitted so legacy document
hashes remain unchanged. Existing selected programs and generator commands
remain usable without rewriting older projects.

The UI can promote an existing unassigned source into a solution without
uploading its contents again. Active tool and grader sources must be
disconnected first. Published material references move with the source.

Successful `prepare` jobs remain separate from working copies until applied.
`job_applications` records application time, resulting document hash and
copy version per user. Applying a result never modifies the shared revision.
Repeating an applied request returns its receipt without overwriting edits.
Working-copy version and source hash protect against concurrent changes.

`auto_apply` is opt-in and only valid for `prepare` jobs. The worker checks
the author's current write access, version and source hash after success.
Failed or cancelled jobs never apply. Changed or revoked copies retain a
completed result with an explanation that automatic application was blocked.

`POST /api/problems/{id}/commit-preview` accepts an unsaved document and
copy version without saving it. It performs the same three-way merge used
by commit, returning the current shared revision, proposed document and
conflicts. The UI compares against the shared revision so independent peer
changes are not listed as the author's changes. `expected_revision` on the
commit prevents publishing a preview based on an outdated shared revision.

Before deployment, run the Alembic migration to create `job_applications`.
The existing backend deployment script runs migrations before starting the
new API and worker. Deploy the backend before its new frontend.
