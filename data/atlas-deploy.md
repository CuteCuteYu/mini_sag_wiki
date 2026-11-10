# Atlas Platform — Deployment and Release

The Atlas platform ships through a single release pipeline named `atlas-release`. Every commit merged into `main` becomes a release candidate automatically, and a human promotes the candidate to staging and then to production with `atlas promote --stage production`. No engineer deploys directly to production by hand; the pipeline is the only write path to the production cluster.

Production traffic is exposed to a new release through a canary stage. The canary receives 5 percent of live traffic for 10 minutes while the pipeline watches the error rate and the p99 latency. The promotion to 100 percent happens automatically only if the canary error rate stays below 0.5 percent and the p99 latency stays under 400 milliseconds for the full observation window.

The rollback window is 30 minutes. For 30 minutes after a release reaches 100 percent, any engineer on the release rotation can run `atlas rollback --to <release-id>` and the previous release is restored from an immutable image in under 90 seconds. After the 30 minute window closes the rollback command refuses to run and the change must instead go through a forward fix or a new release candidate.

Every user visible behaviour is guarded by a feature flag in the `atlas-flags` service. Flags are rolled out on a percentage schedule that is independent of the code deployment, so a release can be deployed with all new behaviour disabled, verified in production, and then enabled gradually. Each flag has a kill switch that the on-call engineer may flip without any approval.

Database changes follow the expand and contract pattern. A schema change is first deployed as an additive migration that both the old and the new code can read, the code is released, and the removal of the old column happens in a later release at least two weeks afterwards. Destructive migrations such as dropping a column or a table are blocked by the pipeline unless the migration is tagged `contract` and approved by the database owners.

Release freezes run from December 15 to January 5, and during any active SEV1 or SEV2 incident. During a freeze only security fixes and rollbacks are permitted, and each exception needs a written approval from the engineering director recorded on the change ticket.

Production changes need two approvals from the change advisory board for anything that touches the billing service, the authentication service or the database migration path. Other services need one approval. The pipeline enforces the approval count and refuses to promote when the approvals are missing or stale.

A release that exhausts its error budget is treated as a change freeze for that service until the budget recovers. The error budget rules are defined in the reliability handbook, and the deployment pipeline reads the current budget from the monitoring service before it allows a promotion.
