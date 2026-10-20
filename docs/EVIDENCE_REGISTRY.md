# Evidence registry and local verification

`ReviewContext.evidence_registry` optionally maps every registered evidence ID to metadata: kind, SHA-256, revision, source path and line range, or a stable external locator. If supplied, the registry must be complete and unique. The bound diff is mandatory; changed-source excerpts are optional and can be selected per profile or budget. Any source evidence for a changed path must use the pinned head (or the base for a deleted file). Diff entries match the bound head and diff hash. Raw content remains in the host's Evidence Packet.

Source evidence can represent an entire file or an inclusive line range. Its digest hashes the exact Git blob bytes or exact selected LF-delimited lines, preserving CRLF and the final newline state. Registration marks whether the pinned bytes are valid text; binary or non-UTF-8 content stays hash-bound but is represented by an omission marker and cannot support source coordinates. Source paths are repository-relative. A finding's coordinates must be covered by a cited head-source entry for that path; an explicit `source_text_available=false` cannot support coordinates, and a base-source or unrelated diff reference cannot support head coordinates. Legacy records without this flag retain their historical citation behavior until re-registered or rebuilt; all new registrations write the flag explicitly. Local verification re-reads the pinned Git object and checks the actual text status even for legacy records.

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

The MCP server validates registry structure and citation/location membership. It cannot read the host's raw packet or prove evidence truth. The host may pass `evidence_verifier=lambda context: verify_evidence(context, repository, packet)` to `HostReviewAdapter` to verify before any workers launch. Legacy contexts without a registry remain accepted; local verification requires a registry.

When a final report JSON is available, export a readable Markdown summary with its evidence refs resolved through the same bound context:

```sh
qa-orch report render --input final-report.json --context bound-context.json --output review.md
```

The renderer sorts findings by Blocker, High, Medium, Low, then unspecified severity; shows source locations, change origin, confidence, impact, verification gaps, and optional next steps; and keeps Coverage gaps and Unverified separate. It also separates adapter-observed callback outcomes and CLI input handoff from executor-attested reviewed paths, and renders verification records or explicitly says none were supplied. The report model preserves legacy reports without `change_origin`, `next_step`, or severity by supplying an unknown/default display value.
