# Security Policy

## Supported versions

Invariant Receipt has not published a stable release. Security fixes currently
target the latest commit on `main`; older commits are not supported separately.

## Reporting a vulnerability

Do not open a public issue or discussion containing vulnerability details,
credentials, private repository content, local paths, or unredacted receipts.

Use GitHub's private vulnerability reporting form:

<https://github.com/tom2025b/invariant-receipt/security/advisories/new>

If the private form is unavailable, contact the repository owner through
<https://github.com/tom2025b> and request a private reporting channel without
including the vulnerability details in the initial public message.

Please include, when safe to do so:

- the affected commit and platform;
- the smallest reproduction;
- the security boundary that was crossed;
- whether original-tree integrity, disposable-copy isolation, named-test
  attribution, receipt contents, or command execution is affected; and
- any suggested mitigation.

There is no guaranteed response-time service level before the first stable
release. Reports will be assessed privately, and confirmed fixes will be
coordinated before public disclosure.

## Sensitive output

Receipts can contain test output, file paths, source mutations, and command
details. Review and redact generated artifacts before sharing them outside the
repository's intended audience. Never put access tokens, private keys, or
credentials in an invariant declaration or failure oracle.
