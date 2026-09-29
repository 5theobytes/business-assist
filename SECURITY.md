# Security policy

## Reporting a vulnerability

Please do not report security vulnerabilities in public issues or discussions. Use **Security → Report a vulnerability** in this repository to submit a private report. Include the affected version, steps to reproduce, and impact. Do not include real user records, API keys, or other secrets in the report.

If that option is unavailable, open an issue asking the maintainer to enable private vulnerability reporting, without including vulnerability details.

## Deployment notes

- Keep provider keys and service-account credentials in environment variables or a secret manager. Never commit them.
- Use a dedicated Firestore project and database for evaluation and live tests.
- Configure authentication, HTTPS, request limits, and an intake challenge before exposing an instance publicly.
- Treat user-submitted text and external provider responses as untrusted data.
- Rotate any credential that may have been exposed; removing it from the current file is not sufficient.
