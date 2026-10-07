<!-- CODESTRA-GOVERNANCE-V3:BEGIN -->
# Codestra Governed Development Contract v3

Standalone family: Contact Center
Component: Codestra-Communication-CC

Mandatory hierarchy:
Product -> Section -> Subsection -> Atomic Task

Only authorized promotion:
subsection -> section -> development -> testing -> staging -> production

Agent rules:
- one active lease per subsection;
- work only in the assigned subsection branch/worktree;
- implementation + tests + evidence + commit + push are required;
- review-only output is not completion;
- no force push and no direct protected-environment writes;
- every promotion requires codestra-control-plane plus repository CI;
- dirty, stale, divergent, dependency-incomplete, or uncertified work fails closed.

Production safety:
- PRODUCTION_GO=NO
- LIVE_CAPABILITIES_ENABLED=NO
- EXTERNAL_EFFECTS=false

Live calls, SMS, email, WhatsApp, payments, publishing, credential issuance,
production database writes, and production infrastructure mutation remain disabled
until separately certified and explicitly approved.
<!-- CODESTRA-GOVERNANCE-V3:END -->
