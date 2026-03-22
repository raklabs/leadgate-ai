# Contributing to LeadGate AI

Thank you for your interest in **LeadGate AI**, built and maintained by **IntelligentStack AI (`@raklabs`)**.

To protect repository integrity, security, and deterministic quality, all contributions are governed by strict guardrails.

---

## 1. Spec-Driven Development Workflow

We do not accept unrequested code dumps or ad-hoc changes. Every change must follow our **Spec-Driven Development** lifecycle:

1. **Specification Alignment**: All changes must trace to [`CONSTITUTION.md`](CONSTITUTION.md) and a formal specification in [`specs/`](specs/).
2. **Fork & Feature Branch**: Create a dedicated branch (`feat/<spec-id>-<short-name>` or `fix/<issue-id>-<short-name>`). Direct pushes to `main` are blocked.
3. **Code Owner Review**: All files are protected by [`.github/CODEOWNERS`](.github/CODEOWNERS). Every Pull Request requires explicit review and approval from `@raklabs` before merging.
4. **Automated CI & Security Gate**: All Pull Requests must pass:
   - Secret & `.env` hygiene checks
   - Bytecode & syntax verification (`python -m compileall -q app scripts tests`)
   - Full unit, integration, and security test suite (`pytest`)
   - 20-enquiry empirical benchmark (`python scripts/evaluate_20_enquiries.py --provider deterministic_local`)

---

## 2. Security & Anti-Abuse Policy

- **Zero Secrets**: Never commit API keys, `.env` files, credentials, or personal/client data.
- **Workflow Protection**: External fork Pull Requests require explicit maintainer approval before GitHub Actions workflows execute, and all workflows run with read-only `GITHUB_TOKEN` permissions (`contents: read`) and a strict 10-minute execution timeout.
- **Spam & Abuse**: Unsolicited promotional PRs, automated typo-farming PRs, or issues that bypass the structured issue templates will be closed and locked.
- **Vulnerability Disclosure**: Do **not** report security vulnerabilities in public issues or PRs. Follow [`SECURITY.md`](SECURITY.md) to submit a private report.

---

## 3. Local Verification Before Opening a PR

Run the full verification suite locally before submitting a Pull Request:

```bash
.venv/bin/pytest -v
.venv/bin/python scripts/evaluate_20_enquiries.py --provider deterministic_local
```
