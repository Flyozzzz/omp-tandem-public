# Security policy

## Supported versions

Security fixes target the latest 3.x release. Older snapshots are retained for history, but do not assume they receive backports. Update to the latest release before checking whether a report is still reproducible.

## Reporting a vulnerability

Do not open a public issue containing exploit details, credentials, private conversations, user paths, or database contents.

Use GitHub's private vulnerability reporting through the repository's **Security** tab when that feature is available. If it is not available, open a minimal issue asking the maintainer for a private reporting channel. Include no sensitive details until a private channel is agreed. No response-time guarantee is implied.

A useful private report includes:

- affected version and client/platform;
- the boundary that is bypassed;
- a minimal reproduction using synthetic data;
- expected and observed behavior;
- impact and any proposed fix or mitigation.

If an actual credential was exposed, revoke/rotate it immediately. Deleting a file, rewriting Git history, or making a repository private does not remove copies already held elsewhere.

## Scope and intended boundaries

Relevant reports include unauthorized cross-project MCP access, incorrect trusted-workspace binding, token leakage, unsafe bootstrap execution, unintended task takeover, and corruption or disclosure during migration/transfer.

OMP Tandem is **not an OS sandbox**. A process running with your OS permissions can access files that those permissions allow. A host agent's shell sandbox does not automatically sandbox an external OMP process. Provider access, model behavior and third-party client policies have their own boundaries.

The project does not ship company-specific product rules. User-provided instructions, artifacts, snapshots and webhook content are data, not authority to bypass permissions. An imported rule or a successful agent report is not independent security approval.

## Handling reports

Maintainers should reproduce with isolated data, avoid public disclosure of sensitive details while a fix is prepared, add a meaningful regression, and document the affected versions and mitigation when appropriate. Keep tests and CI free of real provider credentials and customer data.

Repository visibility and release publication are explicit maintainer actions. Installation, updates and migration must not change visibility, rewrite remote history, or copy provider credentials.
