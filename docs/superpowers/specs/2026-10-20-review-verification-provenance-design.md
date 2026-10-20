# Review Verification Provenance

**Status:** Draft for user review
**Goal:** Let reviewers distinguish a passing result tied to the reviewed commit from a stale, incomplete, or merely host-reported verification claim.

## Current behavior

ReviewVerificationRecord already records check type, status, head SHA, environment, source, source reference, optional run URL, evidence references, and timestamps. The report labels records as host-supplied and states that external results are not independently authenticated. The gap is that the report has no structured provenance grade or verification scope, and consumers can mistake a passed status for a verified result.

## Design

Keep the existing record as the compatibility base and add structured provenance and execution scope. A record states what was checked (scenario and command or check name), the selected test/CI/runtime scope, the run identity, the source artifact digest when one exists, and how the record was obtained. Provenance states distinguish host-reported, locally reproduced, and externally source-verified results. Missing provenance stays unknown; it is never inferred from a run URL or a passed status.

Validation against ReviewContext derives whether the record is bound to the exact head SHA. A matching SHA is necessary but not sufficient for source verification. A mismatch is reported as stale and cannot count as a passing result for this review. Missing scope, run identity, or artifact metadata lowers the provenance state instead of being filled with defaults.

The first implementation adds contracts, report rendering, and a verifier boundary. A read-only provider adapter may verify run identity, commit SHA, conclusion, scope, and timestamps from the provider response. Until such an adapter is configured and its result is retained, CI records remain host-reported. Local execution can be marked locally reproduced only when the host supplies a run record tied to the reviewed SHA and its bounded output digest; that is still not an external attestation.

The record stores references and hashes, not logs, secrets, tokens, or full CI payloads. Provider-specific credentials remain outside the orchestrator. No provider API is assumed by the generic contract.

## Acceptance criteria

1. A verification record for another head SHA is visibly stale and cannot be counted as a pass for the current review.
2. A passed status without provenance remains host-reported or unknown, never provider-verified.
3. Provider-verified status requires a verifier result bound to provider run identity, reviewed SHA, conclusion, scope, and time.
4. Local reproduction is distinct from provider verification and includes a bounded artifact digest when available.
5. Skipped, failed, cancelled, unknown, or not-run states retain their reason and cannot be rendered as passing.
6. The report keeps test, CI, and runtime checks distinct and shows source, scope, SHA binding, and provenance.
7. Old records continue to load and render as host-reported/unverified without being upgraded.
8. Tests cover matching and mismatching SHA, missing provenance, malformed run references, and all result states.

## Boundaries

This work does not claim cryptographic attestation from a host-supplied record. It does not persist credentials or raw logs. Provider integrations are read-only and must be separately configured; the generic verifier contract can ship before any provider adapter.
