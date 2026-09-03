# Codestra Communication production channel activation

## Scope

This release introduces the admission control required before the existing
Communication API and delivery worker may open external email and SMS delivery.
It does **not** activate a runtime by merging source.

The Communication service owns only email and SMS in this release. PSTN,
callback execution, n8n workflow activation, and Odoo writes remain separate
runtime authorities and are forcibly held false by this image.

## Effective activation tuple

The first production activation is a bounded canary and requires all four
runtime requests together:

```text
BUSINESS_WRITES_ENABLED=true
EXTERNAL_DELIVERY_ENABLED=true
LIVE_EMAIL_DELIVERY=true
LIVE_SMS_DELIVERY=true
```

The current worker consumes one shared delivery outbox. Therefore the first
release admits email and SMS as one reviewed pair. A future per-channel worker
split must be independently reviewed before partial channel activation is
allowed.

These flags remain false:

```text
LIVE_PSTN_DIALING=false
CALLBACK_DISPATCH=false
N8N_ACTIVATION=false
ODOO_WRITE=false
LIVE_ODOO_WRITE=false
VICIDIAL_LIVE_CONTROL=false
LIVE_WHATSAPP_DELIVERY=false
LIVE_PUSH_DELIVERY=false
```

## Signed receipt

The runtime refuses the requested live state unless both files are mounted from
root-owned protected storage:

```text
COMMUNICATION_ACTIVATION_RECEIPT_FILE=/run/secrets/communication-activation-receipt.json
COMMUNICATION_ACTIVATION_HMAC_KEY_FILE=/run/secrets/communication-activation-hmac.key
```

The receipt must:

- bind the exact protected source SHA to `CODESTRA_GIT_SHA`;
- bind the exact immutable OCI digest to `CODESTRA_IMAGE_DIGEST`;
- specify `environment=production` and `mode=canary`;
- approve the exact `email,sms` pair;
- authorize a canary greater than zero and no more than one percent;
- expire no more than four hours after its start;
- identify different independent approver and deployment operator identities;
- cite a GitHub approval record;
- prove source and digest readback;
- prove staging no-effect certification;
- prove email and SMS provider readiness;
- prove paired backup/restore and rollback rehearsal;
- prove observability and canary stop conditions;
- prove zero email, SMS, callback, and PSTN counters at baseline;
- prove zero pending delivery rows and zero reconciliation-required rows;
- carry a valid HMAC-SHA256 signature.

Any missing, malformed, expired, mismatched, unsigned, self-approved, or
non-zero-baseline field forces every live-effect variable back to `false`.

## Runtime entry points

The production image starts `app.production:app`. Python also imports the
root-level `sitecustomize.py` before the API, worker, migration helper, or any
alternate module. This prevents bypassing activation admission by invoking
`app.delivery_worker` directly.

The production wrapper replaces the old hard-coded capability response with
truthful effective readback:

```text
GET /capabilities
GET /v1/communications/capabilities
GET /activation/status
```

Every response includes the activation verdict, mode, canary percentage, and
receipt identity. It never exposes the HMAC key or receipt signature.

## Controlled execution order

1. Merge the tested Communication source candidate through protected `main`.
2. Build and publish one immutable image from the resulting protected SHA.
3. Deploy that exact digest to isolated staging with every effect disabled.
4. Verify source SHA, image digest, readiness, capability readback, data
   protection, authentication, consent, suppression, idempotency, tenant
   isolation, provider health, metrics, and zero effects.
5. Complete paired database/configuration backup and clean restore.
6. Rehearse rollback to the previous exact source and image tuple.
7. Establish email and SMS provider credentials through protected secret
   storage; credentials never enter Git or workflow inputs.
8. Produce sanitized evidence hashes with empty delivery and reconciliation
   queues.
9. Obtain independent activation approval bound to the exact candidate.
10. Run **Build communication production activation receipt** from protected
    `main` in the `communication-production-activation` environment.
11. Mount the resulting receipt and the independently managed HMAC key into the
    canary deployment.
12. Set the four requested flags and a canary value no greater than `1`.
13. Verify `/capabilities` returns `APPROVED_CANARY`, the exact receipt ID,
    exact source SHA, exact image digest, and the expected channel pair.
14. Stop and roll back on any authentication failure, provider degradation,
    queue growth, reconciliation movement, unexpected effect, error/latency
    regression, source/digest mismatch, monitoring loss, or receipt expiry.

## Explicit non-authorization

A pull request, merge, green CI run, image build, or signed receipt by itself is
not proof that the live runtime was changed. Production is enabled only when
runtime readback and retained evidence demonstrate the exact approved tuple.
