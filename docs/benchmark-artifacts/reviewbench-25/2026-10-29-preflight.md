# ReviewBench public evaluation preflight — 2026-10-29

This is a preparation record, not a new quality measurement or an approved
quality-gate policy. The ReviewBench public test set has been used in earlier
experiments, so it is not a clean holdout for prompt or discovery changes.

- Source revision: `12e9d1aadbe7ddf4a480c6bd4ffb6ae37c7bcb67`.
- `corpus/test/test.json` SHA-256: `0f277447878a0b02f587c0b40a76a74985d9d1db3cb212d9dfbc97b1d1270f60`.
- `corpus/test/findings.json` SHA-256: `94743c8f143c86a3f7a3d262fea2f5786ea7a312173f6dd2e22ec1200d333440`.
- Source-verified imported dataset SHA-256: `5428d96846e97be55e0c3ae8dd2ba317ac3808b45b53b552a7f4b4794552ee25`.
- All 25 public repositories supplied both pinned commits to the importer.
- A one-million-byte raw-diff check reported no over-budget or unknown cases.
  It does not verify the full packet or the complete model prompt.

The input and Git mirrors were restored in a temporary workspace. Recreate them
from the pinned revision and commits using the procedure in
[Benchmark](../../BENCHMARK.md); the digest verifies the imported dataset.
Keep reference findings out of reviewer inputs. A quality result still requires
fresh, traceable reviewer execution, explicit independent adjudication and
complete accounting for cases that fail or exceed the full prompt budget. A
single integration smoke test cannot establish recall or precision.

A fresh integration smoke on `AA-Factory/aafactory-prototype#17` used head
`a1978cb75950a879df8805e4a5f373eb6678f6f9`, model `gpt-6.1-sol` with
`medium` reasoning, and the current `code_reviewer` through the host adapter.
After binding the discovery and hunk inventory, the executor recorded prompt
handoff and returned a validated profile report; the session reached synthesis.
Two candidates were returned, but have not been adjudicated. This is not a
completed end-to-end review, a recall measurement, or a claim about the other
24 cases. Earlier smoke attempts omitted the bound hunk inventory and returned
invalid hunk attestations; they ended `partial` and are not successful runs.
