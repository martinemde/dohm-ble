# dohm-ble

Reverse-engineered BLE control for the Marpac/Yogasleep Dohm, shipped as a HACS
custom integration. `protocol.py` and `client.py` are vendored inside
`custom_components/dohm/` and deliberately importable without Home Assistant.

General jj workflow and safety rules live in the global CLAUDE.md; this file
covers only what is specific to this repo.

## Verifying a change

```sh
uv run pytest                        # must be green
uvx ruff check <files you touched>
uvx ruff format --check <files you touched>
```

**Only lint and format the files you touched.** The repo is not ruff-clean
under ruff's unconfigured defaults — `protocol.py`, the older tests, and the
section-comment style throughout all trip `I001`, and there is no `[tool.ruff]`
config to say otherwise. A repo-wide `ruff format` would bury a real change
under hundreds of unrelated lines.

### hassfest passing does NOT mean the code imports

This bit us in 0.2.0: hassfest validates the manifest, strings, and metadata.
It never imports the platform modules. A release can be fully green in CI and
still fail at runtime with `ImportError`.

Anything touching Home Assistant APIs must be imported against a real Home
Assistant before release. Nothing in `.venv` can do this — it has bleak only,
by design.

```sh
uv venv --python 3.13 /tmp/ha-check
VIRTUAL_ENV=/tmp/ha-check uv pip install homeassistant aiousbwatcher pyserial
/tmp/ha-check/bin/python -c "
import sys; sys.path.insert(0, '.')
import custom_components.dohm, custom_components.dohm.coordinator
import custom_components.dohm.media_player
print('IMPORTS OK')"
```

`aiousbwatcher` and `pyserial` are not HA dependencies proper — they are pulled
in by the `bluetooth` component's import chain via `usb`, and the import fails
without them.

### What still has no coverage

No test exercises a live BLE stack. `protocol.py` and `client.py` are unit
tested against a fake, and the volume/health policy modules are pure. But the
connect path — timing, the racy first attempt, whether a bond is honored — can
only be verified on hardware. Say so in the release notes rather than implying
it was tested.

## Releasing

The version lives in **`custom_components/dohm/manifest.json` only**. The
`version` in `pyproject.toml` is vestigial (stuck at 0.1.0) and is not the
release version — do not bump it expecting anything to happen.

1. Tests green, lint clean on touched files, imports verified against real HA.
2. Bump `manifest.json`.
3. Update `README.md` if user-visible behavior changed, and re-check it when
   code is *removed*, not just added. 0.2.0's README promised the old fan
   entity was "removed for you"; the code that did so sat behind a call that
   failed first, so the promise was never kept — and dropping that code in
   0.2.1 meant the README had to be corrected again.
4. `jj desc -m "Subject line (vX.Y.Z)"` — every release commit ends with its
   version in parentheses; match the existing log.
5. `jj bookmark set main -r <change>`
6. `jj git push --bookmark main --dry-run`, then push for real. **Warn before
   pushing main**, per the global rules, even on a clean fast-forward.
7. `jj tag set vX.Y.Z -r <change>`
8. `git push origin vX.Y.Z`
9. `gh release create vX.Y.Z --title "vX.Y.Z — lowercase summary" --notes-file <file>`
10. Confirm CI is green on the release commit.

### jj cannot push tags

Step 8 is the one place git is required. `jj tag set` creates the tag locally,
but `jj git push` has no tag support and will not carry it — `jj git push`
alone reports "Nothing changed" while the tag sits unpushed. Use `git push
origin <tag>`. This is the documented exception to "never git".

Existing tags are lightweight (only v0.1.7 is annotated), which is what
`jj tag set` produces. `jj tag list` can also lag origin — v0.1.8 was on the
remote but missing locally — so `jj git fetch` before assuming a tag is absent.

### Don't run `jj new` after pushing

Pushing makes the working-copy commit immutable, so jj creates a fresh one for
you automatically. Running `jj new` on top leaves two stacked empty commits.

## Release notes style

Match the existing releases — they are technical and long, not changelogs.
Sections used: **The bug**, **Changes**, **Still open**, **Known limitation**,
**Unverified**. Lead with the mechanism and name the actual symbol or error
string. Bold-led bullets. State plainly what was not verified; several past
releases turned out to hinge on exactly that.

Breaking changes lead with the upgrade action, before the reasoning.

## Shell gotchas in this repo's tooling

`until` and `for` at the top level of a Bash tool call fail to parse under this
shell. Wrap loops in `bash -c '...'`. For CI waits, poll directly rather than
building a watcher — two attempts at a watcher during 0.2.0 both failed on
syntax before the polling worked.
