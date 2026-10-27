# Verification record

Reviewed on **27 October 2026**. This is an evaluation snapshot of a developed
prototype, with a separate fictional offline demo. The owner approved publishing
only this showcase repository on the review date. The original repository and
personal pilot remain private and unchanged. This is not a production launch.

| Check | Result and scope |
|---|---|
| Full regression suite | **671 passed, 0 failed, 0 skipped** on Linux, CPython 3.14.10, Node 22.23.4, constrained CI dependencies. Includes SQLite and the local libSQL adapter; no remote database. |
| Offline launcher | Passed on Windows, CPython 3.14.4. Health, English/German workspace, profile and CV preview return 200; a live search is rejected with 403. |
| Real browser review | Workspace, fictional profile, matching factors, editable draft, manual approval, CV preview and freelance view checked against the running local application. Approved draft version 3; submission timestamp remains empty. |
| External services | Demo AI disabled; inherited provider credentials removed; non-loopback DNS and connections blocked. Tests also install the network guard. No paid AI, real job ingestion, email delivery or application submission used for this review. |
| Media | Eight actual application screenshots, WebP copies, two 34-second H.264 videos, animated preview and social card. Both MP4 files fully decoded without errors; English playback checked in a browser. |
| Secret scan | Gitleaks 8.30.1 directory and fresh Git-history scans: **0 findings**. One narrowly scoped allowlist covers an explicit synthetic session-secret literal in a unit test. |
| Publication surface | **200 tracked files; 0 issues.** Scan checks runtime data, environment files, personal pilot metadata and README links. |
| GitHub CI | **All three jobs passed** on tested revision `a7499d6`: [verified run](https://github.com/IamRedi/opportunity-engine-showcase/actions/runs/40532705940). CPython 3.14.8: **671 passed, 0 failed, 0 skipped**; dependency check, offline smoke and publication scan passed. Container image build and network-isolated smoke passed. Gitleaks scanned **4 commits / 1.77 MB with 0 findings**. |

The Linux local regression run used a temporary SQLite workspace on tmpfs to
avoid host filesystem latency. SQLite locking and transactions were retained;
this is not evidence of persistent-disk performance. GitHub CI uses its normal
runner filesystem. Docker Desktop failed during its local service startup;
the container was subsequently built and smoke-tested successfully in GitHub CI.
Private-repository runs were initially blocked before execution by account
billing/spending restrictions. After the approved visibility change, public CI
ran without changing account billing settings.

Inspection of an earlier container-based Gitleaks step revealed a Git ownership
error that produced an empty scan despite a successful job. That result was not
accepted. The corrected step uses the runner UID/GID, mounts the repository read
only and rejects Git errors, zero-byte scans and zero-commit scans. The verified
run above confirms that the actual history was scanned.

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
