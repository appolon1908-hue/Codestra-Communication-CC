# Communication runtime alert response

These procedures are read-only until an incident owner authorizes a governed
change. Never enable external delivery as part of alert recovery.

## Target down

Confirm the Prometheus target error, then inspect `/health/live`,
`/health/ready`, the exact release digest, restart count, and dependency status.
Escalate instead of weakening readiness. Use the protected rollback workflow if
the failure began with a release.

## Server errors

Use correlation identifiers to inspect redacted application logs and traces.
Group failures by route and release. Confirm database and key-projection
readiness. Do not replay a mutation unless its durable operation and
idempotency record have been reconciled.

## Latency

Compare route latency with database pool saturation, delivery/event queue age,
and Middleware dependency latency. Preserve request and tenant boundaries; do
not increase timeouts without a reviewed capacity or dependency finding.

## Delivery dead letter

Inspect the operation, audit trail, attempt count, and safe error code. Confirm
whether the outcome is known before reconciliation. Never redispatch an
unknown-outcome command with a new idempotency identity.

## Middleware circuit

Validate Middleware readiness and the private network/TLS path. Wait for the
bounded cooldown and observe the automatic probe. Do not bypass Middleware or
route directly to a provider.

## Event backlog

Inspect Redis TLS connectivity, consumer health, lease age, and dead-letter
counts. Confirm event identity before retrying. Event recovery must not trigger
provider delivery.
