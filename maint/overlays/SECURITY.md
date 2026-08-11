# Security Policy

## Reporting a vulnerability in Gork Build

Please **do not** open a public GitHub issue for security reports that include
secrets, credentials, or exploit details.

Preferred options (in order):

1. **GitHub private vulnerability reporting** on
   [thedavidweng/gork-build](https://github.com/thedavidweng/gork-build/security)
   (if enabled for the repository)
2. Contact the maintainers via a private channel listed on the repository

Include:

- Affected version / commit
- Reproduction steps (minimal)
- Impact assessment
- Whether the issue is in Gork Build-specific deltas (privacy/branding) or
  inherited from upstream Grok Build

We aim to acknowledge reports promptly and coordinate disclosure.

## Upstream (SpaceXAI / xAI) issues

Bugs or vulnerabilities that exist in **upstream** Grok Build and are not
introduced by this fork should also be reported to SpaceXAI through their
program when appropriate:

https://hackerone.com/x

Mentioning both channels helps everyone; please still avoid posting secrets
publicly.

## Scope notes

- **In scope:** Gork Build client code in this repository, packaging, and
  privacy hard-off regressions
- **Out of scope (for this repo alone):** the remote Grok model API, account
  billing, or SpaceXAI server infrastructure — report those upstream
