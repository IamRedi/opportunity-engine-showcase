# Security and privacy

This is an evaluation snapshot, not an invitation to use a personal pilot.
The included launcher uses a fresh SQLite workspace, fictional people and
fictional listings. It does not read an existing CV, database, `.env` file,
browser session, provider credential or cloud account.

## Demo boundary

- The launcher binds to loopback by default. Docker publishes only to loopback.
- Provider credentials are removed from the child environment. AI is disabled.
- A process-level network guard rejects non-loopback connections, including DNS.
- The scheduler and queue worker are not started. Email and search are disabled.
- Drafts are editable local documents. Nothing is sent to an employer.
- The database is generated at runtime and excluded from Git and the image.

The local demo is intentionally usable without login. Do not expose this local
mode to the internet or upload real personal documents into a shared instance.
The cloud authentication, tenant and usage modules are included for inspection
and regression tests; the showcase does not provision or deploy them.

## Design controls in the application

Production mode requires authentication and server-derived actor/tenant
context. Passwords use scrypt, private files use scoped routes, and drafts have
explicit review states. The test suite exercises cross-user denial, usage
accounting and queue behavior. These tests are not a security certification.

Known limits and verification scope are recorded in [EVIDENCE.md](EVIDENCE.md).
Report concerns privately through the author's GitHub profile. Do not put
credentials or personal CV content in a public issue.
