# Codex QA review reminder

This opt-in hook provides one continuation prompt when a single user message
contains both a Jira URL and one or more GitLab MR URLs and the hook
has not observed matching finished QA Orchestrator sessions. It never proves
the content of a review or the truth of a reported partial outcome. It permits
the next stop to prevent endless loops, including when an input or tool is
unavailable. It does not cover references supplied in separate messages, later
user turns, disabled hooks, events missed before activation,
hook failures, or untrusted hook definitions. Another Stop hook can consume the
single continuation before this one. Existing conversations may need
a restart. Users can interrupt a continuation normally.

It supports macOS and Linux; its file locking uses `fcntl`, which is not
available in standard Python on Windows. Matching uses Jira-style `/browse/`
and GitLab-style `/-/merge_requests/` HTTPS paths on any host.

The script receives Codex hook events and stores only issue and MR URLs, run
IDs, and statuses in a per-user mode-0700 temporary directory. Temporary state
is session-specific and reset at the next non-continuation prompt. It does not
store the prompt, tool outputs, or report contents. Hook trust requires manual
review in Codex; modifying the script may require renewed trust.

Run tests: `cd scripts/qa-review-gate && python3 -m unittest -v test_gate`.
