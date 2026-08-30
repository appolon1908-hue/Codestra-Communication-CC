# Codestra Communication — Role and Integration Contract

## Mission
Codestra Communication is the customer-communications control plane for email, SMS, WhatsApp, push and other approved outbound/inbound messaging channels.

## Owns
- Message requests and conversation records
- Templates, template versions and localization
- Channel selection policy
- Consent, opt-out and suppression enforcement
- Delivery state, retries, receipts and message history
- Communication preferences and quiet-hour policy
- Provider-neutral messaging contracts

## Does Not Own
- Marketing campaign budgets or targeting
- CRM master data
- Identity
- General workflow orchestration
- Social post publishing
- Provider-specific durable transport primitives owned by Middleware where applicable

## Mandatory Flow
Business service -> Codestra Communication -> Middleware/provider adapter -> delivery provider -> webhook -> Middleware -> Codestra Communication -> Odoo/event consumers.

## Required Controls
No message may bypass consent/suppression policy. Every outbound command requires tenant/campaign context, idempotency, audit metadata and template/content provenance. Provider callbacks must be authenticated and deduplicated.

## Core Domains
Channel, Message, Conversation, Recipient, Template, TemplateVersion, Consent, Preference, Suppression, DeliveryAttempt, DeliveryReceipt, ProviderRoute.

## Required APIs
- /v1/communications/messages
- /v1/communications/conversations
- /v1/communications/templates
- /v1/communications/preferences
- /v1/communications/consents
- /v1/communications/suppressions
- /v1/communications/providers

## Required Events
communication.message.requested, communication.message.accepted, communication.message.sent, communication.message.delivered, communication.message.failed, communication.message.replied, communication.consent.changed.

## Implementation Order
1. Canonical message model
2. Consent/suppression policy
3. Template registry
4. Idempotent send API
5. Provider adapters through Middleware
6. Delivery receipts and inbound replies
7. Odoo synchronization
8. AI-assisted drafting via Codestra AI
9. Analytics and observability