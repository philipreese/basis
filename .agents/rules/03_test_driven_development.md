# 🧪 Rule 03: Test-Driven Development (TDD)

## 1. Goal
Ensure all modifications are fully covered by robust automated tests, preventing regressions, and validating edge cases through a test-first methodology.

## 2. Red-Green-Refactor Loop
When implementing a new feature or bug fix:
1.  **Red**: Write unit/integration tests that describe the behavior and verify they fail.
2.  **Green**: Write the minimum amount of code to make the tests pass.
3.  **Refactor**: Clean up the code, optimize performance, and remove duplication while keeping the tests passing.

## 3. Test Coverage Requirements
- **Branch Coverage**: Code changes MUST achieve a minimum of **80% branch coverage**.
- **Edge Cases**: Explicitly test boundary values, nulls, empty collections, and error handling paths.
- **Verification**: Run the coverage runner skill or project verification script to calculate metrics.

## 4. Test Design & Mocking Rules
- **Mocking boundaries**: Mock external services, databases, and filesystem access. Do not perform network requests or write to system paths outside the test sandbox during unit tests.
- **Readability**: Follow a clear structure in tests: `Arrange / Act / Assert` or `Given / When / Then`.
- **Naming**: Use descriptive names indicating the scenario under test and the expected outcome (e.g., `GetUser_WhenUserDoesNotExist_ShouldReturnNotFoundError`).
