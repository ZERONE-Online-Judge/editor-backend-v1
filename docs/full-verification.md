# Full Editor solution verification

Editor verification runs every selected testcase, including after WA, TLE,
MLE, RE, or output-limit verdicts. Each solution/testset uses one immutable
judge request. It never resumes shrinking subsets after individual failures.
Contest submissions retain their existing first-failure policy.

## Protocol and compatibility

- Snapshot: `collect_all_results: true`.
- Node capability: `editor-judge-full-v1`, implemented by agent 0.2.23.
- Only nodes advertising this capability can claim these requests. The
  administrator's Editor routing permission and normal slot limits still apply.
- The first non-AC result in testcase order remains the aggregate verdict.
  Individual verdicts and measurements are retained for every completed case.
- Existing serial confirmation of a TLE found during parallel judging remains
  active. This confirms a testcase; it does not rerun previously accepted tests.
- Compile failures or infrastructure failures can leave cases unexecuted.
  Missing cases are never represented as AC or counted as completed tests.

Case progress uses existing authenticated node leases. Updates are buffered
for up to 0.5 seconds or 16 cases, with immediate first/last-case updates.
The final report resends all case evidence so a lost progress update does not
lose the result. The server rejects duplicate or foreign testcase IDs,
contradictory aggregates, changed evidence, and incomplete success reports.
Full reports retry transient transmission failures up to three times. The
private judge gateway permits 32 MiB requests and a 150-second response wait
to accommodate bounded base64 output without relaxing its source IP rules.

## Progress and shared results

`verification_progress` contains phase, combined current/total work units,
integer percentage, and solution completion counts. Each selected test has
input, validator, answer, checker, and ready steps, plus a sample step for
enabled custom display-sample checks. Ready is confirmed after duplicate checking and blob
storage. Successful stored or cached preparation counts too; failed steps do
not. Each confirmed solution/testcase outcome is one further work unit.

Nested `preparation` and `judging` counters expose each part independently.
Preparation also includes per-stage counters. Each `testsets` entry has the
same combined counters and nested preparation/judging breakdown. NR scopes
are excluded only from judging; preparation still runs for selected tests.
Percentages measure completed work, not estimated elapsed time. They do not
reset when preparation hands over to judging, and compile errors cannot
complete unexecuted testcase units. Old records without nested counters keep
their original judging-only meaning.

All requested solutions appear immediately. Their queued/running/completed
state is independent, so a slow earlier solution does not hide later results.
An interrupted verification retains its confirmed count and partial results.
The worker saves these snapshots with the existing heartbeat; normal result
polling also restores them after a page reload.

## Actual output files

The agent captures stdout bytes before cleaning its sandbox, including on
WA, TLE, MLE, and RE when stdout exists. Text decoding is only for previews;
downloaded files preserve the captured bytes.

- Capture limit: 256 KiB per testcase and 16 MiB per judge request.
- Capture limits do not change the submitted program's judge output limit.
- Every capture carries original byte count, retained byte count, SHA-256,
  and an explicit truncation flag. An empty stdout remains an empty file.
- The central server stores output in object storage, not in the job JSON.
  Signed output reads are restricted to the account owning that judge request.
- Editor checks output size/hash and copies the file into the problem's blob
  storage once. Collaborators can view/download it under existing problem
  read access. Editor project quota still applies.
- Central temporary outputs follow the existing 48-hour judge-run cleanup;
  Editor's saved result blobs remain available with the problem.
- Truncated output is identified in the UI. A failed output fetch leaves the
  judge verdict intact and shows an explicit retrieval error.

## Deployment order

1. Deploy the ZOJ backend protocol and output storage changes.
2. Roll out agent 0.2.23 to the Editor-enabled judge nodes and verify their
   `editor-judge-full-v1` registration.
3. Deploy the Editor backend and frontend changes, including prior downloads.

No new ports, credentials, or environment variables are required. In-flight
jobs and historical reports are not rewritten. Start a new verification to
use complete-suite judging, live progress, and actual-output capture.
