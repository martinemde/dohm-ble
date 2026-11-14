# Dependency updates

The central Renovate runner reads the root [`renovate.json`](../renovate.json),
which extends the shared
[`martinemde/renovate-config` v1.0.0 preset](https://github.com/martinemde/renovate-config/tree/v1.0.0).
There is no repository-local Renovate runner. All updates require manual review;
the final package rule disables automerge even when inherited rules enable it.

Discovery is limited to the root `pyproject.toml` and `uv.lock`,
`.github/workflows/validate.yml`, and `renovate.json`. Local `.claude/worktrees`
are excluded. The `pep621` manager also updates `uv.lock`; the pinned engine has
no separate `uv` manager. The HACS manifest at
`custom_components/dohm/manifest.json` currently has no external requirements,
so no custom runtime-pin manager is needed. Add manifest tracking if pinned
requirements are introduced.

Review updates and run the checks in [CLAUDE.md](../CLAUDE.md) before merging.
Dependency checks and CI do not operate hardware or verify a live BLE bond.
Do not actuate a machine, clear bonds, or overwrite schedules while reviewing
updates without explicit permission.
