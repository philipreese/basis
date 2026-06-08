# 🔍 Rule 06: Independent Validation & Verification (IV&V)

## 1. Goal
To ensure that all code modifications are objectively verified against the original user requirements by a separate validation loop, eliminating developer agent confirmation bias.

## 2. The Verification Role
- The validation agent (IV&V role) must operate under the assumption that the implemented code is broken until proven otherwise.
- It must test code by executing tests, checking edge cases, inspecting output structures, and analyzing runtime logs.
- It must not rely on the developer agent's assertions, but rather on objective script execution outputs.

## 3. Traceability Matrix
- Map user requirements to specific tests, verification scripts, or files.
- Verify that every user requirement is covered by at least one integration test, manual check script, or unit test assertion.

## 4. Verification Execution
- Run `verify-project.ps1` to ensure all tests pass and static checks succeed.
- Perform sanity/smoke checks on endpoints or UI components.
- Prepare a Verification Report summarizing:
  - Requirement Traceability status (Passed/Failed).
  - Test outcomes.
  - Security and lint status.
  - Remaining issues or warnings.
