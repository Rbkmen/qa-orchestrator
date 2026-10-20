# Review Input Completeness and Impact Closure

**Status:** Draft for user review
**Goal:** Make missing review inputs and unresolved out-of-diff impact visible in the final report, so bounded discovery is never mistaken for complete coverage.

## Current behavior

ReviewContext pins repository, base/head revisions, diff and packet hashes. Criterion coverage is completed only for requirement and claim records already registered in its evidence packet. Neighbor discovery scans a bounded set of pinned files and can add CodeGraph caller/callee suggestions, but intentionally does not claim to be exhaustive. The final report therefore cannot tell whether a source was omitted before registration, or whether every discovered neighbor was assessed.

## Design

### 1. Input manifest

Add an immutable, host-authored manifest bound to the review context. It describes each required Jira issue, merge request, specification, or other declared source without copying its raw content. A source record contains a stable identifier and locator, source revision or snapshot digest when available, whether retrieval and extraction completed, whether pagination completed, and the criterion references extracted from that source.

Completeness is derived from those records:

- Complete means every declared required source was retrieved, all required pages were read, and extraction completed.
- Partial means at least one required source or page is missing, or extraction stopped early.
- Unknown means the host did not provide enough information to determine completeness.

An empty criteria list is valid when extraction completed and the source contains no review criteria. A source cannot be treated as complete merely because the model supplied no criteria. The manifest remains a host assertion; the orchestrator verifies its internal consistency and digest binding, not the truth of an external connector's claim.

Registered requirement and MR-claim evidence must agree with the manifest's extracted criterion references. Any declared criterion absent from the evidence registry is a coverage gap. If the manifest is absent on a legacy context, the report says input completeness is unknown and preserves the legacy data.

### 2. Impact-closure ledger

Add a host-owned ledger for candidates returned by bounded neighbor discovery. It binds to the exact head SHA, changed-path digest, discovery method/version, configured limits, and the discovery result digest. Candidate relations include callers, callees, imports/dependencies, tests, API or schema contracts, database changes, events, background jobs, configuration/feature flags, and authorization boundaries when discovered.

Each candidate receives one explicit disposition: reviewed, checked with no relevant relationship, unresolved, or not checked. A disposition includes a concise rationale and evidence references where applicable. The ledger reports candidate counts by relation and disposition, along with omitted candidates and scan limits.

“Closed” means every candidate in the recorded discovery result has a disposition and the discovery run reports no unaccounted omissions. It does not mean every possible repository relationship was found. Any omitted candidate, unresolved item, uninspected limit, or unavailable graph result keeps the ledger partial and is rendered as such. Existing discovery stays advisory; it does not silently expand the pinned evidence packet.

### 3. Reporting and compatibility

Render input completeness and impact closure as separate sections from changed-path and criterion coverage. Show unknown, partial, and complete states with their reasons and counts. Do not merge them into a single green “review complete” label.

New fields are additive. Existing serialized contexts and reports continue to load; missing fields produce unknown or legacy-unverified status rather than being upgraded to complete. All source references remain metadata-only; raw tickets, PR bodies, source contents, and credentials are not copied into the new manifest.

## Acceptance criteria

1. A required source with incomplete retrieval, pagination, or extraction cannot produce complete input status.
2. Every manifest criterion is either registered and assessed or shown as a gap.
3. Every discovered impact candidate has a validated disposition; unresolved, unchecked, omitted, and capped results remain visible.
4. The ledger is rejected when its head SHA, changed-path digest, or discovery-result digest does not match the bound review.
5. A closed ledger is explicitly scoped to the recorded bounded discovery and never described as exhaustive repository coverage.
6. Legacy contexts and reports still parse and render without a false completeness claim.
7. Tests cover complete, empty, partial, omitted, mismatched, and legacy inputs, including hostile text in locators and rationales.

## Boundaries

This work does not implement a universal static analyzer or guarantee that every possible dependency is discoverable. It does not fetch Jira or GitLab content itself. Provider pagination and extraction status come from the host adapter and remain labeled as host-supplied evidence.
