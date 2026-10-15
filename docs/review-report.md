# Evidence-based review report

`GET /api/problems/{id}/review-report` authorizes the caller before reading
any report or cache. Writers see their saved working copy; read-only users
see the committed revision. Reports never run code or call a paid AI API.

The overview groups errors, warnings, recommendations, and scope notes.
Static checks cover required materials and duplicate input hashes.
Content hashes identify stale translations. Complete scoped verification
records identify mismatched expected verdicts; partial runs never prove
that an intended wrong solution passes all tests. Evidence links open the
specific Invocation. Component self-tests use dedicated recorded runs.

Execution evidence must match the document hash or the hash recorded when
a preparation was applied. Only terminal jobs are read, newest first,
with a bounded 24-job evidence window. A process-local 128-entry cache is
keyed by problem, database, content hash, and terminal job IDs. It expires
after 30 seconds. It is derived data, not a persisted AI correction report.

Constraint comparison recognizes explicit `lower <= name <= upper` and
named testlib `readInt/readLong` calls with literal integer endpoints.
Conditional expressions, macros, general mathematics, grammar, and
semantic equivalence require human review. A first-token maximum check
is available only when the first read follows validation registration
without intervening control flow, and every registered input exists.
Only 256 bytes per input are read. Unknown coverage is never reported as
missing maximum coverage. Warnings include the parsed evidence and scope.

Unsaved edits are explicitly marked as newer than the report. This report
supplements, and does not relax, execution and package blocking checks.
