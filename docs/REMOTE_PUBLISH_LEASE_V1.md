# Remote publish lease protocol v1

This protocol coordinates every production publisher that writes beneath the
shared Infomaniak `web_exports` tree.

## Lease record

The active lease is the directory
`.xcbenz_web_exports_publish.lock`. Acquisition is an atomic `mkdir`. A v1
owner writes these files before doing protected work:

- `protocol_version`: `1`
- `owner`: globally unique owner token
- `publisher`: publisher lane
- `host`: originating host
- `pid`: originating process ID
- `acquired_at`: UTC ISO-8601 acquisition time
- `acquired_at_epoch`: acquisition epoch seconds
- `lease_seconds`: declared lease duration
- `heartbeat_at`: last heartbeat in epoch seconds

Writers update `heartbeat_at` through a temporary file and atomic rename, then
`touch` the lock directory. Updating the directory mtime keeps older
age-checking publishers from mistaking a live v1 lease for an abandoned legacy
lock during rollout.

## Ownership and fencing

A writer owns the lease only while all of these are true:

1. the lock directory exists;
2. `protocol_version` is `1`;
3. `owner` exactly matches its unique token; and
4. the heartbeat is within `lease_seconds + recovery_grace_seconds`.

The writer must verify these conditions immediately before every public commit
point. Long promotion scripts renew the heartbeat between commit-point groups.
A writer that loses ownership must stop promotion. It must not roll back over a
successor's publication.

## Recovery

Automatic recovery applies only to a well-formed v1 lease. Legacy, incomplete,
unknown-version, or malformed locks always require manual inspection.

A waiter may recover an expired v1 lease only by:

1. observing that its heartbeat is beyond the lease and grace period;
2. exclusively locking the sibling `.lock.guard` with kernel-managed `flock`;
3. re-reading protocol, owner, heartbeat, and duration;
4. confirming the same lease is still expired; and
5. atomically renaming the lock directory into
   `.xcbenz_web_exports_publish.quarantine/`.

The waiter then releases the mutation guard and competes normally for `mkdir`.
It never deletes the abandoned record in the acquisition path.

Every acquire, recover, and release mutation uses the same guard. Kernel-managed
locking means a killed process cannot orphan the guard. Heartbeats first change
directory into the owned lock generation and then use relative paths, so a late
heartbeat remains attached to the quarantined inode and cannot write into a
successor's newly created lock directory.

## Release and uncertain outcomes

Release reads `owner` and removes the active directory only on an exact match.
It is idempotent and retried with a short dedicated timeout. If the first remote
removal succeeds but its SSH acknowledgement is lost, a retry sees no owned lock
and succeeds. A retry must never remove a successor's lease.

## Defaults

- Lease: 900 seconds
- Heartbeat: 30 seconds
- Recovery grace: 60 seconds
- Release/heartbeat command timeout: 30 seconds

The lease must exceed both two heartbeat intervals and the maximum protected
remote-command duration. Changes to these defaults must stay compatible in every
publisher.

## Quarantine and candidate retention

Quarantined leases are audit evidence. Remove them only with a separate,
retention-based garbage-collection job. Candidate cleanup may remove only
expired candidates that are not referenced by the active lease or public tree.
