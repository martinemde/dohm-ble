# dohm-ble

Reverse-engineered BLE control for the Marpac/Yogasleep Dohm, shipped as a HACS
custom integration. `protocol.py` and `client.py` are vendored inside
`custom_components/dohm/` and deliberately importable without Home Assistant.

This file is the repository's contributor/agent guide. Start with `README.md`
for supported features, `docs/debugging.md` for current troubleshooting, and
`docs/pairing.md` for backend-specific recovery. `docs/protocol.md` describes the
wire format; `docs/android-app.md` records the APK evidence.

Keep changes local unless publishing is requested. Never actuate a machine,
clear a bond, or overwrite a schedule as part of a diagnostic without the
user's permission. Do not treat a successful connection or unit test as proof
of a working hardware bond.

## Verifying a change

```sh
uv run pytest                        # must be green
uvx ruff check <files you touched>
uvx ruff format --check <files you touched>
```

**Only lint and format files you touched.** Ruff configuration can come from
the surrounding workspace, so check the actual output rather than assuming a
particular set of baseline warnings. Compare any unrelated findings with the
parent revision; report them instead of silently disabling rules. Avoid
repo-wide formatting that would hide the intended change. Documentation-only
changes still need a link/path check and `git diff --check` (read-only).

### hassfest passing does NOT mean the code imports

This bit us in 0.2.0: hassfest validates the manifest, strings, and metadata.
It never imports the platform modules. A release can be fully green in CI and
still fail at runtime with `ImportError`.

Anything touching Home Assistant APIs must be imported against a real Home
Assistant before release. The project's `.venv` deliberately has no HA.
Use a separate environment and match the HA/Python versions you plan to
support. This example reproduces the audited HA 2026.2.3 / Python 3.13 pair;
it does not validate every newer HA release:

```sh
uv venv --python 3.13 /tmp/dohm-ha-check
uv pip install --python /tmp/dohm-ha-check/bin/python \
  "homeassistant==2026.2.3" aiousbwatcher pyserial
/tmp/dohm-ha-check/bin/python -c "
import sys; sys.path.insert(0, '.')
import custom_components.dohm, custom_components.dohm.client
import custom_components.dohm.coordinator, custom_components.dohm.config_flow
import custom_components.dohm.media_player, custom_components.dohm.fan
print('IMPORTS OK')"
```

The base test run intentionally skips HA-only entity/setup tests when HA is
absent. Run those in the separate HA environment too:

```sh
uv pip install --python /tmp/dohm-ha-check/bin/python pytest pytest-asyncio
/tmp/dohm-ha-check/bin/python -m pytest
```

Import any new platform modules in this check too. Record the actual versions
and separate import/unit-test results from live HA or hardware tests.

`aiousbwatcher` and `pyserial` are not HA dependencies proper — they are pulled
in by the `bluetooth` component's import chain via `usb`, and the import fails
without them.

### What still has no coverage

No test exercises a live BLE stack. `protocol.py` and `client.py` are unit
tested against a fake, and the volume conversion helpers are pure. But the
connect path — timing, the racy first attempt, whether a bond is honored — can
only be verified on hardware. Say so in the release notes rather than implying
it was tested.

## Entity selection

Entity type is chosen at setup and stored in config-entry data. Entries without
it default to `media_player`. Options are for bond recovery only; changing type
requires deletion/re-addition. Load exactly one platform and unload the platform
actually loaded. Do not add automatic entity-registry migration or bond clearing
when changing presentation.

## Releasing

The version lives in **`custom_components/dohm/manifest.json` only**. The
`version` in `pyproject.toml` is vestigial (stuck at 0.1.0) and is not the
release version — do not bump it expecting anything to happen.

1. Fetch remote state with `jj git fetch`. Select the exact release commit;
   do not assume the working copy `@` is it.
2. Verify tests, touched-file lint/format, and HA imports. State what was not
   tested on hardware.
3. Bump `manifest.json` and update the README for user-visible changes.
4. Commit/describe the release as `Subject line (vX.Y.Z)`. Ordinary development
   commits do not need a release suffix. `jj commit` leaves a fresh working
   copy, so the completed release is normally `@-`.
5. Point `main` at that exact release commit:
   `jj bookmark set main -r <release-commit>`.
6. Inspect `jj git push --remote origin --bookmark main --dry-run`. **Warn the
   user before pushing main**, then push only the intended bookmark.
7. Create the tag on the same commit:
   `jj tag set vX.Y.Z -r <release-commit>`.
8. On jj 0.45.1, inspect and push the tag with
   `jj git push --remote origin --tag vX.Y.Z --dry-run`, then the same command
   without `--dry-run`. Check your installed `jj git push --help`; older
   versions without `--tag` need `git push origin vX.Y.Z` instead.
9. Create the GitHub release with `gh release create vX.Y.Z --title "..."
   --notes-file <file>`. Confirm release CI is green.

Never use `--all` for a routine release. Do not move an existing published tag
or force-push main to repair a mistake without discussing it first. After a
commit or push, inspect `jj status` before running `jj new`: a fresh empty
working copy may already exist.

## Release notes style

Match the existing releases — they are technical and long, not changelogs.
Sections used: **The bug**, **Changes**, **Still open**, **Known limitation**,
**Unverified**. Lead with the mechanism and name the actual symbol or error
string. Bold-led bullets. State plainly what was not verified; several past
releases turned out to hinge on exactly that.

Breaking changes lead with the upgrade action, before the reasoning.

## Research artifacts

Keep APKs, decompiled app code, captures, and temporary HA environments in the
ignored `artifacts/` or `captures/` directories. Commit protocol findings and
reproducible provenance, not third-party APK contents or embedded credentials.
Historical host names, MACs, handles, and SSH details are examples, not defaults
for another installation.
