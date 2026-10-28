# Evidence registry and local verification

`ReviewContext.evidence_registry` optionally maps every registered evidence ID to metadata: kind, SHA-256, revision, source path and line range, or a stable external locator. If supplied, the registry must be complete and unique. The bound diff is mandatory; changed-source excerpts are optional and can be selected per profile or budget. Any source evidence for a changed path must use the pinned head (or the base for a deleted file). Diff entries match the bound head and diff hash. `prior_review` entries identify a same-MR review reference and the exact head it covered; their raw content remains in the host's Evidence Packet. A ref attached to `prior_review_decisions` is included in every profile's selected packet, even if its fragment was otherwise optional.

Source evidence can represent an entire file or an inclusive line range. Its digest hashes the exact Git blob bytes or exact selected LF-delimited lines, preserving CRLF and the final newline state. Registration marks whether the pinned bytes are valid text; binary or non-UTF-8 content stays hash-bound but is represented by an omission marker and cannot support source coordinates. Source paths are repository-relative. A finding's coordinates must be covered by a cited head-source entry for that path; an explicit `source_text_available=false` cannot support coordinates, and a base-source or unrelated diff reference cannot support head coordinates. Legacy records without this flag retain their historical citation behavior until re-registered or rebuilt; all new registrations write the flag explicitly. Local verification re-reads the pinned Git object and checks the actual text status even for legacy records.

Neighbor discovery is bounded and records its omissions. Test-path detection includes qualified directories such as `tests-e2e` and `test-integration`, so added lines from those test files do not become production search anchors. Changed tests can also pull in their nearest matching runner config (`playwright.config.*`, `jest.config.*`, and the other supported runner names) plus CI workflows whose pinned source contains an explicit test command. Workflow scanning is capped at 64 paths and 8 MB, with skipped paths recorded; deploy-only workflows are excluded. Direct added-diff type/domain anchors can produce exact hash-bound ranges in large related sources. When a directly matched controller uses a JavaScript or TypeScript path, discovery may widen that range to its enclosing block handler if the declaration and same-indent closing brace can be identified and the full handler stays within 200 lines and 48 KB. Conventional same-name route files and one-hop static imports from those controllers/routes are also eligible. If the handler boundary cannot be identified or included within those bounds, discovery records the omission instead of implying that the full handler was reviewed. For static TypeScript imports, discovery records which local bindings are referenced or called in the matched handler and ranks calls tied to a changed-diff anchor above general imports. Validators, paired routes, and middleware retain higher priority; this ranking stays within the existing import and packet limits.

## Build and verify

Prepare a JSON list of records, for example:

```json
[
  {"evidence_ref":"E1","kind":"source","revision":"<full head SHA>","path":"src/api.py","start_line":10,"end_line":20},
  {"evidence_ref":"E2","kind":"diff","revision":"<full head SHA>"},
  {"evidence_ref":"E3","kind":"requirement","locator":"TASK-123","sha256":"<content SHA-256>"}
]
```

```sh
qa-orch evidence build --context context.json --records records.json \
  --repository /path/to/repo --output bound-context.json
qa-orch evidence verify --context bound-context.json --repository /path/to/repo \
  --packet evidence-packet.md --report profile-report.json --output verification.json
```

The builder computes source/diff hashes from immutable Git objects. External content hashes are supplied by the host. Verification checks commit identities, the exact diff hash, source hashes and ranges, and optionally the packet hash and finding coordinates. It does not execute target code or modify the repository. External requirements/runtime records remain explicitly unverified by this local checker. Final `verification_records` preserve test, CI and runtime outcomes with the exact reviewed head SHA, scenario, environment, source/run reference, and reason for non-passing states. These records are host-supplied; a run URL or runtime evidence hash does not make the orchestrator an independent CI verifier. A missing packet check is reported as unknown; mismatches fail instead of silently rebinding the input.

## On-demand link reachability

`verify` stays offline. To check whether URL locators respond, run the separate network command explicitly and allowlist each exact hostname:

```sh
qa-orch evidence check-links --context bound-context.json \
  --allow-host github.com --timeout 10 --output link-audit.json
```

Repeat `--allow-host` for each exact hostname required by the evidence. Wildcards, IP addresses, ports, and paths are rejected. Only HTTPS URLs without embedded credentials, query strings, or fragments are requested; redirects are followed only when their destination hostname is also allowlisted. Identical URLs are checked once and mapped back to their evidence refs. The JSON distinguishes reachable, redirected, access-denied, missing, and failure statuses; the command exits nonzero for failed URL checks. Non-URL identifiers are reported without a network request.

A reachable response proves only that the URL responded at check time. It does not prove that the page content matches an external evidence hash, that the page still represents the pinned PR/task state, or that the cited claim is true. External records therefore remain `unverified_external_refs` in ordinary local verification until a separate content-verification mechanism is available.

The MCP server validates registry structure and citation/location membership. In schema-v2 reviews, an `introduced` finding must cite the bound diff, a `pre_existing` finding must cite base-source evidence covering the same location, and an `exacerbated_by_pr` finding must cite both the bound diff and base-source evidence covering the same location. The latter means the defect predates the PR but the PR makes it worse. It cannot read the host's raw packet or prove evidence truth. The host may pass `evidence_verifier=lambda context: verify_evidence(context, repository, packet)` to `HostReviewAdapter` to verify before any workers launch. Legacy contexts without a registry remain accepted; local verification requires a registry.

When a final report JSON is available, export a readable Markdown summary with its evidence refs resolved through the same bound context:

```sh
qa-orch report render --input final-report.json --context bound-context.json --output review.md
```

The renderer sorts findings by Blocker, High, Medium, Low, then unspecified severity; shows source locations, change origin, confidence, impact, verification gaps, and optional next steps; and keeps Coverage gaps and Unverified separate. It also separates adapter-observed callback outcomes and CLI input handoff from executor-attested reviewed paths, and renders verification records or explicitly says none were supplied. The report model preserves legacy reports without `change_origin`, `next_step`, or severity by supplying an unknown/default display value.
