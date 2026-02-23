# CTO Operations Runbook (v1)

## 1) Deployment Path (single lane)

1. All operational changes are merged to `main`.
2. Runtime execution is triggered only by:
   - `schedule`, or
   - `workflow_dispatch` with optional `backlog_id`.
3. Self-evolution code deployment is restricted to a **specific backlog id**.
   - No `backlog_id` -> code deployment step is skipped.
   - Allowed statuses for deployment target: `CONFIRMED`, `DEVELOPING`.

## 2) Pre-deploy Gate (must pass)

Before bot execution, CI runs E2E smoke gate:

- index CTA opens auth overlay.
- unauthenticated app user is redirected to index.
- authenticated new user sees onboarding + keyword modal and can add keyword.

If gate fails, workflow fails and release is blocked.

## 3) Rollback Policy

### Code rollback
- Generated code must pass:
  - Python compile
  - Runtime structure guard (critical signatures must exist)
  - Size sanity check for core runtime file
- On validation failure:
  - auto restore previous code
  - mark backlog status as failure class
  - block git push

### Emergency rollback (workflow_dispatch)
- Use `rollback_to_sha` input to run rollback mode.
- Rollback mode behavior:
  1. restore product files (`app.html`, `index.html`, `master.html`, `news_bot.py`, `requirements.txt`, `data.json`) from target SHA
  2. run E2E smoke gate on rollback snapshot
  3. commit rollback snapshot and push to `main`
  4. record result to `release_ledger`
- `master.html` release ledger provides "↩️ 이 버전으로 롤백" action for successful `main` releases.

### Data rollback
- Prefer forward-fix for DB/content changes.
- Avoid destructive rollback unless critical impact confirmed.

## 4) Release Traceability

Each execution records release metadata in `release_ledger` (or action-log fallback):

- `run_id`, `status`, `branch`, `commit_sha`, `backlog_id`
- `release_type`, `released_at`, `completed_at`, `note`

Dashboard (`master.html` > Dev tab) provides:

- status counts
- status filters
- paginated list
- summary + expandable full details

## 5) Incident Response SLO

- MTTD < 5 minutes
- MTTR < 15 minutes
- Failed release should be visible in dashboard within 1 minute

## 6) Operational Checklist

Before production run:

1. Verify `main` commit and branch alignment.
2. Verify E2E smoke gate pass.
3. Verify release ledger start record created.

After run:

1. Verify success/failure final status in release ledger.
2. Verify core user flows in production:
   - email link -> app landing
   - in-app browser guidance
   - onboarding modal for new users
3. Verify no unexpected rollback markers in backlog status.

