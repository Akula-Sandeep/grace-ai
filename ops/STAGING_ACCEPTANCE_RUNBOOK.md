# Grace — Staging Acceptance Runbook

This runbook is the release gate for the production-hardening audit. It assumes a disposable staging environment with PostgreSQL 16/pgvector, Redis 7, Docker Compose, the real provider configuration used for staging, and k6 for concurrency testing.

## Preconditions

Use staging-only credentials and synthetic test accounts. Never reuse production secrets. The release artifact must contain no `.env`, `node_modules`, `dist`, test-result, cache, or credential files.

Record the release ID, Git commit, image digests, database backup identifier, and UTC start/end times for the drill.

## 1. Deploy and migration sequencing

```bash
docker compose up -d db redis
docker compose run --rm db-bootstrap
docker compose run --rm db-migrate
docker compose up -d backend feedback-worker frontend
```

Verify the migrator job completes exactly once and runtime workers use the `grace_app` role rather than the migrator role.

```bash
docker compose ps
docker compose logs --no-log-prefix db-migrate
```

For a multi-replica drill, start the intended backend replica count and confirm no runtime replica attempts DDL.

## 2. Readiness/liveness

```bash
curl -fsS http://staging-host/api/v1/live
curl -fsS http://staging-host/api/v1/ready
```

`/live` must stay dependency-light and represent process health. `/ready` must fail when a required dependency is unavailable and recover after the dependency returns.

## 3. P1-01 — refresh-token rotation race

Run the dedicated PostgreSQL test against a disposable staging database:

```bash
export POSTGRES_TEST_DATABASE_URL='postgresql+psycopg://...'
cd Backend
pytest -q tests/test_refresh_rotation_concurrency.py
```

Acceptance: two simultaneous uses of the same refresh token produce exactly one success; the losing reuse attempt is rejected; the refresh family state is consistent with the documented reuse policy.

## 4. 100+ concurrent SSE users

Create at least 100 synthetic verified accounts with isolated refresh/access cookies and CSRF tokens. Supply one credential object per VU.

```bash
export CHAT_STREAM_URL='https://staging-host/api/v1/chat/stream'
export VUS=100
export DURATION='2m'
k6 run ops/loadtest/chat.js
```

Acceptance is the thresholds defined by `ops/loadtest/chat.js` plus explicit observation of event-loop latency, worker CPU/memory, DB pool saturation, Redis latency, and provider quotas.

## 5. Redis failure / rate-limit safety

While representative authentication and chat traffic is running, stop Redis:

```bash
docker compose stop redis
```

Exercise login, OTP, and chat limits from multiple worker replicas. Security-critical rate limiting must remain globally enforced or return the documented controlled degraded-service response; it must not silently become process-local.

Restore Redis:

```bash
docker compose start redis
```

Confirm readiness recovers and distributed state resumes normally.

## 6. Provider timeout/outage

Use staging-only provider endpoint controls or a staging proxy to force deterministic timeout/error responses for TMDB, web search, and LLM/vision providers one at a time.

Acceptance:

- bounded request deadlines are respected;
- retries stay within the request budget;
- cached safe data remains available when appropriate;
- partial useful answers survive transient stream failures;
- provider failures are surfaced with the correct error domain rather than a generic chat failure;
- no retry storm or unbounded fan-out occurs.

## 7. Large-account export

Seed a disposable synthetic account large enough to stress the configured row/byte limits. Request an export through the API and watch the job lifecycle.

Acceptance:

`pending → running → ready`

with bounded DB batches, controlled worker memory, progress metadata, final size metadata, one-use download authorization, expiry/cleanup, and deterministic failure at configured limits.

## 8. Delete → restore → purge lifecycle

Using a disposable account:

1. Schedule deletion.
2. Confirm access/session behavior changes as documented.
3. Restore within the recovery window using the signed restore credential.
4. Verify the account and permitted data are restored.
5. Schedule deletion again and advance the clock/cutoff in a disposable environment.
6. Run the purge worker.
7. Verify account children are removed and retained audit records are anonymized according to policy.
8. Verify expired restore credentials cannot be reused.

## 9. Multi-worker metrics

Run multiple backend workers and generate known request counts. Query the protected metrics endpoint and compare the aggregated counters/histograms with the expected total across workers.

Confirm Prometheus multiprocess files are created in the configured directory and are cleaned/reinitialized correctly on worker startup.

## 10. PostgreSQL connection budget

Record:

`replicas × workers × (pool_size + max_overflow)`

and compare it with the configured `DB_CONNECTION_BUDGET` and actual database capacity. Exercise representative concurrent load and confirm the application stays below the configured budget.

## 11. Backup/restore drill

Create and encrypt a staging PostgreSQL backup:

```bash
BACKUP_AGE_RECIPIENT='...' ./ops/backup/backup-postgres.sh
```

Restore it to a new disposable database:

```bash
BACKUP_FILE='...' \
RESTORE_DATABASE='grace_restore_drill' \
RESTORE_AGE_IDENTITY='...' \
./ops/backup/restore-drill.sh
```

Acceptance: restore completes, migration version is correct, and critical sampled account/movie/state records are readable.

## 12. Evidence capture

For each drill record:

- release ID and Git commit;
- exact command/config used;
- start/end UTC timestamps;
- pass/fail result;
- observed p50/p95/p99 latency where applicable;
- peak worker memory/CPU;
- DB connection usage;
- Redis/provider failure timing;
- screenshots/log excerpts only when they are needed to explain a failure;
- any deviation, rollback, or follow-up action.

A finding may move from `IMPLEMENTED/STAGING-VERIFICATION` to `DONE/VERIFIED` only when its required staging evidence is attached to the release record.

## Final release gate

Do not label Grace production-ready until:

- P1-01 PostgreSQL refresh concurrency is demonstrated;
- the 100+ SSE drill passes;
- Redis failure demonstrates fail-safe security controls;
- provider outage behavior is bounded and recoverable;
- large-account export completes within worker resource limits;
- delete → restore → purge is observed end-to-end;
- multi-worker metrics aggregate correctly;
- readiness/liveness and migration sequencing behave correctly;
- backup/restore is demonstrated;
- the final release artifact is clean and secret-free.
