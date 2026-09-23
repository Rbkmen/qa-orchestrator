# Security policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately through a [GitHub Security
Advisory](https://github.com/Rbkmen/qa-orchestrator/security/advisories/new).
Do not publish credentials, private logs, tokens, or an exploit before the
maintainers have had a chance to investigate.

Include the affected version, a minimal reproduction, impact, and any safe
mitigation. Do not include task content, source code, or user data unless it is
strictly necessary to reproduce the issue.

## Data handling

QA Orchestrator is designed to keep evidence, prompts, model responses, and
final QA decisions in the host agent. Its local metrics file contains only
content-free aggregate fields. Review the [installation guide](docs/INSTALLATION.md)
before changing the metrics directory or sharing diagnostic output.
