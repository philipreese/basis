# 🔒 Rule 05: Security, Safety & Privacy

## 1. Goal
To prevent security vulnerabilities, credential leaks, and privacy issues from entering the codebase, ensuring compliant and secure software at all stages.

## 2. Secrets Management
- **Zero Secrets in Code**: Never commit passwords, tokens, API keys, certificates, or CSRF tokens to version control.
- **Environment Configuration**: Store all secrets in environmental variables or a local `.env` (which must be added to `.gitignore`).
- **Secret Scanning**: Use tools (like git-secrets, gitleaks, or custom scripts) to verify no secrets are contained in any staged files.

## 3. Secure Code Design
- **Input Sanitization**: Always sanitize and validate all external inputs (from HTTP request bodies, query params, CLI args) before processing to prevent SQL injection, XSS, and path traversal.
- **Minimize Dependencies**: Be cautious when adding new third-party packages. Check their download metrics, maintainer status, and open security alerts (e.g. `npm audit`, `pip-audit`).
- **OWASP Compliance**: Mitigate common vulnerabilities including injection, broken authentication, data exposure, XML external entities, and broken access controls.
