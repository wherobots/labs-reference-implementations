# Contributing

Wherobots Labs projects welcome contributions from customers, partners, and
the community via standard GitHub pull requests.

## How it works

- Fork the repo (or branch, for collaborators) and open a PR against `main`.
- Every PR goes through the same release gates as internal changes: human
  review of the design, automated checks passing in CI, and a security review
  for anything touching credentials, dependencies, or new external surfaces.
- Contributing grants no access to core Wherobots repositories; each Labs
  repo manages its own collaborators.

## Ground rules for this repo

- Each reference implementation is self-contained in its own directory with
  its own README, deploy scripts, and a symmetric teardown.
- Secrets live only in a local, gitignored `.env`; every implementation ships
  a documented `.env.example`. Never commit secrets, credentials, or internal
  Wherobots infrastructure references.
- Every AWS resource an implementation creates must be tagged (`ManagedBy`,
  `Project`, `CreatedAt`, `TeardownBy`) and removed by its teardown script.
- Numbers quoted in a README must come from a verified live run.

## Support expectations

Functional issues are handled on a best-effort basis through
[GitHub Issues](../../issues) with no SLA. Security vulnerabilities are the
exception and are handled under Wherobots' standard security policy — report
them to security@wherobots.com rather than in a public issue.
