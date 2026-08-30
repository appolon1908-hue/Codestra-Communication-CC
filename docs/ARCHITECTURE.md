# Codestra Communication CC Architecture

## Role
Codestra Communication CC is the customer-channel communication control plane for approved email, SMS, WhatsApp, push, and future supported channels.

## Owns
- channel-neutral message intents
- templates and versioning
- recipient preferences
- consent and suppression state
- send approval state
- provider-neutral delivery status
- conversation/message history
- channel policy and fallback rules

## Does not own
- CRM truth: Odoo
- authentication: Keycloak
- gateway policy: Kong
- provider transport reliability: Middleware
- workflow orchestration: n8n
- campaign spend: Codestra Marketing
- AI provider/model routing: Codestra AI

## Initial APIs
- POST /v1/communications/messages
- GET /v1/communications/messages/{id}
- POST /v1/communications/templates
- GET /v1/communications/templates
- POST /v1/communications/consents
- POST /v1/communications/suppressions
- GET /v1/communications/recipients/{id}/preferences
- POST /v1/communications/conversations

## Delivery safety
- outbound live delivery disabled by default until separately approved
- consent checked before regulated/marketing delivery
- suppression lists fail closed
- idempotency required for send commands
- provider callbacks verified and replay-safe through Middleware
- immutable audit trail for consent, suppression, send approval, and delivery result

## Events
communication.message.requested
communication.message.approved
communication.message.dispatched
communication.message.delivered
communication.message.failed
communication.message.bounced
communication.message.replied
communication.consent.changed
communication.suppression.changed
