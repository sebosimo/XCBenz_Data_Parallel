# Infomaniak storage retention

Infomaniak enforces an account quota independently of the underlying
filesystem reported by `df`. Forecast publication therefore performs two
preflights before it creates an upload candidate:

- a real, fsynced allocation probe, bounded by
  `DEPLOY_CAPACITY_PROBE_MAX_BYTES`;
- when `DEPLOY_ACCOUNT_QUOTA_BYTES` is nonzero, an account-usage check that
  reserves `DEPLOY_ACCOUNT_QUOTA_RESERVE_BYTES` beyond the incoming tree.

Set the quota from the Infomaniak control panel. Do not derive it from `df`.
The usage root defaults to the SSH account home and can be changed with
`DEPLOY_ACCOUNT_QUOTA_USAGE_ROOT` only when the account layout is known.

All forecast and hydration SSH connections require an independently verified
known-hosts file. First-use trust and `ssh-keyscan` are not publication
controls.

## Rollback tree decision

`_previous_web_exports` remains a complete tree for now. The forecast commit
uses directory renames and can restore that tree immediately if the second
rename fails. Removing live-owned subtrees from it would invalidate that
failure path and would also make an operator rollback incomplete.

Reference-aware catalog and radar cleanup may safely operate inside both the
current and previous trees while holding the shared publication lease, because
each tree's own indexes are revalidated before deletion. This removes retained
garbage without changing rollback semantics.

A smaller forecast-only rollback needs a different transaction: construct a
rollback candidate from the previous forecast-owned paths plus the current
live-owned paths, validate the merged candidate, reacquire and verify the v1
lease, then rename it into place. That redesign should be rolled out and
failure-injected separately; it is not safe to approximate by deleting live
subtrees from the existing rollback tree.
