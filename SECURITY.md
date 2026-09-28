# Security policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately through a [GitHub Security
Advisory](https://github.com/Rbkmen/qa-orchestrator/security/advisories/new).
Do not publish credentials, private logs, or an exploit before the
maintainers have had a chance to investigate.

Include the affected version, a minimal reproduction, impact, and any safe
mitigation. Do not include task content, source code, or user data unless it is
strictly necessary to reproduce the issue.

## Data handling

QA Orchestrator keeps evidence, prompts, model responses, and final QA decisions
in the host agent. By default, session state stays in process memory. If
`QA_ORCHESTRATOR_SESSION_STORE_PATH` is explicitly set, a local SQLite file
stores only unfinished structured session state for restart recovery; the row
is deleted at finalization. Final outcomes, task history, and statistics are not
persisted. Review the [installation guide](docs/INSTALLATION.md) before sharing
diagnostic output.
