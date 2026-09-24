# Profile and Bundle Evaluation

This is a small, synthetic smoke pack for checking role boundaries and bundle selection. It is not a model benchmark and does not collect task statistics. Run it locally with the model configuration you intend to use; do not save model responses or per-run scores.

## How to use it

1. Call `prepare_review_route` for the profile named in the case and use only its returned sections and constraints.
2. Give the host model the case's scope and evidence references as a compact Evidence Packet. Keep the source text in the host.
3. Check the expected behavior below. A candidate must cite the supplied evidence and identify its confidence and verification gap.
4. Repeat after changing a profile, bundle, model default, or host instructions. Treat these synthetic cases as smoke coverage, not proof of production quality.

OpenAI recommends evaluating prompting guidance with the selected model and workload; use the [GPT-6 model guidance](https://developers.openai.com/api/docs/guides/latest-model) when evaluating OpenAI models.

## Bundle selection checks

| Change scope | Expected route | Check |
|---|---|---|
| Broad TypeScript test-automation change | `autotest` | Runs code review, test analysis, and TypeScript review. |
| Broad non-TypeScript test-automation change | `ordinary_mr` | Does not run an irrelevant TypeScript profile. |
| Broad React + TypeScript widget change | `widget` | Includes React and TypeScript review. |
| Broad JavaScript React widget change | `widget_js` | Includes React and general code review, without TypeScript review. |
| Broad Ruby backend change | `ruby_backend` | Includes evidence mapping, Ruby-specific review, and test analysis. |
| Broad Python or MCP service change | `python_backend` | Includes Python-specific async, schema, error, and test review. |
| Broad React Native or native mobile change | `mobile` | Includes mobile platform review and test analysis without assuming web-only behavior. |
| Narrow test-only change | `pr_test_analyzer` | Uses the compatibility profile instead of a full bundle. |
| Narrow React-only change | `react_reviewer` | Uses the compatibility profile instead of a full bundle. |
| Narrow Ruby-specific change | `ruby_reviewer` | Reviews Ruby behavior without assuming Rails. |
| Ruby test-only change in RSpec or Minitest | `pr_test_analyzer` | Uses the test framework present in the repository; does not prescribe a switch. |
| Narrow Python async or MCP-tool change | `python_reviewer` | Checks Python/MCP contracts without imposing a test framework. |
| Narrow native permission or lifecycle change | `mobile_reviewer` | Checks affected platform behavior and keeps device verification separate. |

## Synthetic profile cases

### 1. Evidence map only — `code_explorer`

**Scope:** A changed report handler calls a local account service, which calls a wallet service.

**Evidence:** `E1` shows the report-to-account call; `E2` shows the account-to-wallet call; the wallet response contract is unavailable.

**Expected:** Map the two calls with references, mark the missing contract unverified, and return no defect candidate. Do not infer that the wallet call is wrong.

### 2. Concrete changed-code defect — `code_reviewer`

**Scope:** A response amount changed from integer minor units to a decimal value.

**Evidence:** `E1` states that the consumer expects minor units; `E2` shows the changed code dividing by 100; `E3` shows the consumer still dividing by 100.

**Expected:** Return one candidate for double scaling, supported by `E1`–`E3`, with a confidence and verification gap. Do not assign severity or release readiness.

### 3. Coverage gap only — `pr_test_analyzer`

**Scope:** A change adds behavior for an empty item list.

**Evidence:** `E1` describes the empty-list behavior; `E2` shows tests for one and several items, with no empty-list case.

**Expected:** Report the missing empty-list regression test. Do not claim the implementation is defective without evidence of a wrong result.

### 4. Authorization boundary — `security_reviewer`

**Scope:** An authenticated user requests an account by ID.

**Evidence:** `E1` says users may access only their own account; `E2` shows the handler checks authentication but does not compare the requested account owner with the authenticated user.

**Expected:** Return an authorization candidate with `E1` and `E2`; leave runtime verification and final classification to the host.

### 5. False success after failure — `silent_failure_hunter`

**Scope:** A downstream request can time out.

**Evidence:** `E1` shows the timeout handler returning `{ok: true, rows: []}`; `E2` says callers interpret `ok: true` as a completed lookup.

**Expected:** Return a false-success candidate with both evidence references. Do not treat the empty result as a valid fallback unless the evidence says it is one.

### 6. Unvalidated external data — `typescript_reviewer`

**Scope:** An API response is used as an `Account` object.

**Evidence:** `E1` shows the response cast directly with `as Account`; `E2` says the external API may return `role: null`; `E3` shows code reading `account.role.name`.

**Expected:** Return a candidate for the unvalidated nullable boundary and cite the three references. Avoid reporting unrelated style or type preferences.

### 7. Stale React request — `react_reviewer`

**Scope:** The same component can switch between account IDs.

**Evidence:** `E1` shows a data-fetching effect with an empty dependency list; `E2` shows `accountId` can change without remounting the component.

**Expected:** Return a candidate that the displayed data can remain tied to the previous account. Cite both references and state what user-path or runtime check remains unverified.

### 8. Ruby no-op mutator return — `ruby_reviewer`

**Scope:** A Ruby method now assigns the result of `items.compact!` back to `items` before iterating.

**Evidence:** `E1` states that Ruby's `Array#compact!` returns `nil` when no elements are removed; `E2` shows the changed assignment followed by `items.each`; `E3` shows a valid input with no `nil` elements.

**Expected:** Return one candidate that the valid no-op case raises when `items.each` is called on `nil`. Cite `E1`–`E3`; do not report Ruby style preferences or assume the application uses Rails.

### 9. Sequel Dataset result discarded — `ruby_reviewer`

**Scope:** A changed Ruby query adds a Sequel filter before returning rows.

**Evidence:** `E1` states that `Dataset#where` returns a new Dataset and leaves the original unchanged; `E2` shows the return value discarded; `E3` shows the original Dataset being materialized with `.all` and the contract requiring only active rows.

**Expected:** Return one candidate that the result is unfiltered because the changed Dataset was not retained. Cite `E1`–`E3`; do not assume the app uses ActiveRecord merely because Rails is present.

### 10. Retried Sidekiq side effect — `ruby_reviewer`

**Scope:** A job performs an external charge and then records local completion.

**Evidence:** `E1` confirms Sidekiq retries this failure; `E2` shows the external charge occurs before the local completion write and includes no idempotency key; `E3` states the provider creates a new charge for each request unless the same idempotency key is supplied; `E4` shows a timeout can occur after the provider completes the charge but before the local write.

**Expected:** Return one candidate that a retry can create a duplicate charge. Cite `E1`–`E4`; state that provider/runtime confirmation remains unverified and do not assign severity.

### 11. MCP error contract after async cancellation — `python_reviewer`

**Scope:** A FastMCP tool awaits an upstream request and catches its timeout.

**Evidence:** `E1` shows the tool's declared output schema; `E2` shows the timeout handler returning an empty success-shaped object that does not satisfy the schema; `E3` shows the host maps tool errors separately from successful results.

**Expected:** Return one candidate for converting a timeout into an invalid success result. Cite `E1`–`E3`; note that host/runtime behavior remains unverified and do not assume every Python service uses FastMCP.

### 12. React Native permission state after resume — `mobile_reviewer`

**Scope:** A mobile screen requests a platform permission and resumes after the user changes the permission in system settings.

**Evidence:** `E1` shows the permission state is read only on first mount; `E2` shows the screen can remain mounted while the app backgrounds and resumes; `E3` shows the UI continues to enable the protected action from the cached state.

**Expected:** Return one candidate that the visible permission state can be stale after resume. Cite `E1`–`E3`; state that device-level lifecycle verification remains unverified and do not infer identical iOS/Android behavior without evidence.

### 13. No supported defect — any finding-producing profile

**Scope:** A changed parser handles an empty value.

**Evidence:** `E1` shows the parser returning the documented empty result; `E2` shows tests for empty, valid, and malformed values, all matching the stated contract. No other relevant evidence is supplied.

**Expected:** Return no finding candidate. Mention a gap only if a specific required behavior is actually unverified; do not invent a risk to fill the section.

## Pass conditions

- Each profile uses only its returned sections and stays within its focus.
- Every finding candidate is traceable to the supplied evidence; unsupported candidates fail the case.
- `code_explorer` maps evidence without turning the map into findings, and `code_reviewer` does not repeat the map.
- Coverage gaps remain separate from defect candidates; an empty case can return no candidate.
- Bundle choice matches the language and scope matrix; the host does not submit custom profile lists.
- The host alone maps evidence to deep-review signals and makes the final QA decision.
