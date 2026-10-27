# Verification record

Reviewed on **27 October 2026**. This is an evaluation snapshot of a developed
prototype, with a separate fictional offline demo. It is not a production launch.

| Check | Result and scope |
|---|---|
| Full regression suite | **671 passed, 0 failed, 0 skipped** on Linux, CPython 3.14.10, Node 22.23.4, constrained CI dependencies. Includes SQLite and the local libSQL adapter; no remote database. |
| Offline launcher | Passed on Windows, CPython 3.14.4. Health, English/German workspace, profile and CV preview return 200; a live search is rejected with 403. |
| Real browser review | Workspace, fictional profile, matching factors, editable draft, manual approval, CV preview and freelance view checked against the running local application. Approved draft version 3; submission timestamp remains empty. |
| External services | Demo AI disabled; inherited provider credentials removed; non-loopback DNS and connections blocked. Tests also install the network guard. No paid AI, real job ingestion, email delivery or application submission used for this review. |
| Media | Eight actual application screenshots, WebP copies, two 34-second H.264 videos, animated preview and social card. Both MP4 files fully decoded without errors; English playback checked in a browser. |
| Secret scan | Gitleaks 8.30.1 directory and fresh Git-history scans: **0 findings**. One narrowly scoped allowlist covers an explicit synthetic session-secret literal in a unit test. |
| Publication surface | **200 tracked files; 0 issues.** Scan checks runtime data, environment files, personal pilot metadata and README links. |
| GitHub CI | **Blocked before any job started** by GitHub account billing/spending restrictions. [First run and annotations](https://github.com/IamRedi/opportunity-engine-showcase/actions/runs/40530889018). The workflow is configured for full tests, dependency checks, offline smoke, publication-surface checks, full-history Gitleaks and Docker smoke; no remote pass is claimed. |

The Linux local regression run used a temporary SQLite workspace on tmpfs to
avoid host filesystem latency. SQLite locking and transactions were retained;
this is not evidence of persistent-disk performance. GitHub CI uses its normal
runner filesystem. Docker Desktop failed during its local service startup, so
the container build/smoke is **not verified**. Its dedicated CI job is configured
but also blocked by the account billing restriction. No settings or billing
changes were made to bypass either limitation.

The test run emitted 13 dependency/deprecation warnings, including Starlette's
test-client transition, an XML element truthiness warning and a pytest fixture
deprecation. These are recorded rather than described as a warning-free run.

## Media provenance

The videos are silent, captioned screenshot montages, not continuous interaction
recordings. No generated UI, stock footage or private pilot recording is used.
See [MEDIA.md](docs/MEDIA.md) and the file hashes in
[media-manifest.json](docs/assets/media-manifest.json). All people, employers,
listings, contact data and application text in the fixture are fictional.

## What this does not verify

- No hosted public demo, cloud deployment, provider credentials or live AI output
  was tested or provisioned for this snapshot.
- Cross-platform installation is checked on Windows and Linux; macOS is not
  independently verified.
- The demonstration uses an authored example draft. Language handling of live
  generated drafts remains separate work; some stored rule explanations can
  still contain Albanian source text.
- Cloud authentication, actor isolation, durable work and usage accounting have
  local regressions, not a third-party security certification or an operational
  readiness claim.

The original project and personal pilot are separate private assets. Their
databases, CVs, access links, credentials and Git history are not distributed.
