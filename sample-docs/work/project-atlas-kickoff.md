# Project Atlas — Kickoff Notes

Attendees: Priya (engineering lead), Jonas (product), Mei (SRE), Sam (security).

## Goal

Migrate the order service from the legacy VM cluster to Kubernetes, with zero downtime during the cutover and p95 latency below 200 ms.

## Decisions

- PostgreSQL stays the system of record; we add a read replica for reporting queries.
- Redis replaces the in-process session cache so API pods become stateless.
- Rollout uses a canary: 5% of traffic for 48 hours, then 50%, then 100%.

## Risks

The legacy service writes files to local disk for invoice exports. Those need to move to object storage before the migration, otherwise pods lose exports on restart.

Sam flagged that service-to-service traffic is unencrypted today. mTLS is required before the canary starts.

## Action Items

- Priya: architecture design doc, design review by 20 November 2026.
- Mei: staging cluster with autoscaling ready by 27 November 2026.
- Sam: mTLS rollout plan, due 4 December 2026.
- Jonas: communicate the maintenance window to customers two weeks in advance.
