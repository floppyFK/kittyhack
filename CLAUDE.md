# CLAUDE.md — Orienting notes for kittyhack

Read this **before** exploring. Saves ~3 Explore-agent roundtrips per session.

## What this project is

Shiny-for-Python web app that replaces the defunct Kittyflap cloud/app. Controls a smart cat flap with RFID, camera-based cat+prey detection, per-cat rules, MQTT, optional remote inference. Debian/Raspberry Pi OS.

**Repos (user has maintainer rights on both):**
- Upstream: `floppyFK/kittyhack`
- Fork:     `FabulousGee/kittyhack` (local `origin` → here, `upstream` → upstream repo)
- Feature PRs go fork → upstream, user merges. Release tags live on upstream.

User-facing docs: `README.md` (EN + DE). This file is for the assistant.

## Runtime topology — target vs remote mode

Two deployment shapes, gated by `is_remote_mode()` (`src/mode.py`, reads `.remote-mode` marker or env):

| Mode | Where UI+AI run | What runs on the Kittyflap |
|------|------------------|----------------------------|
| **target** (default) | on the Kittyflap itself | `kittyhack.service` (Shiny UI) + `kittyhack_control.service` (supervisor, watchdog) |
| **remote**           | on a separate Linux PC/VM | `kittyhack_control.service` only (handles sensors, relays camera stream to the remote) |

`kittyhack_control.py` **refuses to start in remote mode** (hard guard at `main()`). So features like the WLAN watchdog live there and only exist on real Kittyflap hardware.

Setup files: `setup/kittyhack.service`, `setup/kittyhack_control.service`, `setup/kittyhack-setup.sh`.

## Where things live

| Concern | File | Notes |
|---------|------|-------|
| ASGI entry + middleware chain | `app.py` | Order: `ApiMiddleware → TabRoutingMiddleware → shiny_app` |
| Config defaults / load / save | `src/baseconfig.py` | `CONFIG` global dict, `DEFAULT_CONFIG`, `load_config()`, `save_config()` |
| Shiny UI + reactive handlers | `src/server.py` | ~9800 lines. `ui_system()`, `ui_configuration()`, `ui_info()`, `ui_live_view`, etc. |
| Backend loop (runs in kittyhack.service) | `src/backend.py` | `backend_main()`, `manual_door_override` dict, MQTT publish/receive hooks |
| Target-side supervisor | `src/kittyhack_control.py` | `_wlan_watchdog_loop`, relay camera, boot-wait logic |
| Subprocess wrappers (nmcli, systemctl, git, pip, …) | `src/system.py` | `update_kittyhack()`, `switch_wlan_connection()`, `apply_wlan_runtime_settings()`, `systemctl()` |
| Version check / release notes / update-repo resolution | `src/helper.py` | `read_latest_kittyhack_version()`, `fetch_github_release_notes()`, `resolved_update_repo()` |
| Door hardware | `src/magnets_rfid.py` | `Magnets` singleton (`Magnets.instance`), `queue_command("unlock_inside" | "lock_inside" | "unlock_outside" | "lock_outside")`, state getters |
| DB access | `src/database.py` | `db_get_cats`, `db_get_motion_blocks`, `get_cat_settings_map`, backup helpers |
| MQTT | `src/mqtt.py` | `MqttPublisher`, topics, `handle_manual_override()` bridge in backend |
| REST API (PR #153, not yet merged to main) | `src/api.py` | Token storage, auth, Starlette routes, middleware |
| Translations | `locales/{de,en}/LC_MESSAGES/messages.{po,mo}` | gettext, compile with `msgfmt` |
| Per-release changelogs | `doc/changelogs/changelog_vX.Y.Z_{en,de}.md` | One file per version per language |
| Static assets (JS toggles, CSS) | `www/` | `server-ui.js` handles conditional-visibility toggles |

## Config pattern (adding a new setting)

Three places, exactly in this order, or load/save drifts silently:

1. `DEFAULT_CONFIG["Settings"][…]` in `baseconfig.py` (~line 185+): lowercase ini-key → default value.
2. `new_config = { … "UPPER_KEY": safe_str/int/bool/float/enum(…) }` in `load_config()` (~line 442+).
3. `settings["lowercase_key"] = CONFIG["UPPER_KEY"]` in `save_config()` (~line 611+).

For UI: add input to `ui_configuration()` in `server.py` (~line 6441+), then assign from `input.<id>()` in `on_save_kittyhack_config()` (~line 7645+).

Conditional visibility pattern: give the container an `id=` and toggle via a new init function in `www/server-ui.js` (see `initIpCameraUrlToggle`, `initUpdateRepoToggle`).

## Door control — do NOT bypass

Canonical: set a flag in `backend.manual_door_override = { unlock_inside, unlock_outside, lock_inside, lock_outside }`. The backend loop in `backend.py` picks it up, runs safety checks, and calls `Magnets.instance.queue_command(…)` with correct state tracking (`inside_manually_unlocked`, event annotations, MQTT mirror). MQTT and the REST API both use this path.

Directly calling `Magnets.instance.queue_command("…")` **skips** that state tracking. Only do it for teardown/shutdown.

Read state with `Magnets.instance.get_inside_state()` / `get_outside_state()` (returns `True` when unlocked).

## Shiny patterns used throughout

- `@output @render.ui def ui_xxx()` → builds a DOM fragment returned to `output_ui("ui_xxx")` slots.
- `@reactive.Effect` (or `@reactive.effect`) + `@reactive.event(input.btn_id)` → button-click handler.
- `reactive.Value(0)` as a trigger → bump with `reload_trigger_X.set(reload_trigger_X.get() + 1)` to force a `@render.ui` that reads `.get()` to re-run. Global triggers sit at `server.py:838+`.
- Modals: `ui.modal(body, title=…, footer=ui.div(ui.input_action_button(…)))` → `ui.modal_show(m)` → `ui.modal_remove()`. Many handlers share `input.btn_modal_cancel` as the dismiss button — OK, the generic cancel handler is a no-op for unrelated modals.
- Notifications: `ui.notification_show(msg, duration=N, type="message|warning|error")`.
- Input mutation from server: `ui.update_text/select/switch("id", value=…)`.

## i18n workflow (gettext)

Wrap every user-visible string in `_("…")`. Adjacent string literals in Python auto-concatenate → one gettext entry.

Adding translations:

1. Append entries to `locales/de/LC_MESSAGES/messages.po` (German) and `locales/en/LC_MESSAGES/messages.po` (leave msgstr empty → gettext falls back to msgid).
2. Compile **both** `.mo` files and commit them (the .mo is what gets loaded at runtime, the setup script does not re-compile):
   ```
   msgfmt -o locales/de/LC_MESSAGES/messages.mo locales/de/LC_MESSAGES/messages.po
   msgfmt -o locales/en/LC_MESSAGES/messages.mo locales/en/LC_MESSAGES/messages.po
   ```

Tools are not on the dev box's default PATH. On this user's Windows machine: `mlocati.GetText` (via winget) lives at `C:\Users\Fabian\AppData\Local\Programs\gettext-iconv\bin\` — add to PATH in the shell before running.

Multi-line msgids use `""`-continuation syntax matching Python's literal concatenation. Markdown in a string = normal characters, no escaping needed for backticks.

## Release workflow

Tags are **lightweight** (no annotation, `git tag vX.Y.Z`). Follow the style.

1. Per-release changelogs in `doc/changelogs/changelog_vX.Y.Z_{en,de}.md` — headline `# vX.Y.Z`, sections `## New Features` / `## Improvements` / `## Bugfixes` with `- **Title**: description` bullets.
2. GitHub release body combines both languages: `# vX.Y.Z - Deutsch\n\n…\n--------\n\n# vX.Y.Z - English\n\n…`. The in-app `filter_release_notes_for_language()` parses that exact header pattern.
3. Commit message for changelog additions: `added changelogs for vX.Y.Z`.

## Update install flow — critical detail

`src/system.py:update_kittyhack()` does in order:

1. `git restore .` (wipe working-tree changes)
2. `git clean -fd` (delete **untracked files**)
3. `git remote set-url origin <resolved_url>` (if custom update repo configured)
4. `git fetch --all --tags`
5. `git checkout <tag>` (tag mode) OR `git checkout -B <ref> origin/<ref>` (branch mode)
6. `pip install -r requirements.txt` if requirements hash changed
7. Install systemd unit files, `daemon-reload`, apply boot semantics
8. Rollback branch (`git checkout <current_version>`) on any failure

**Implication:** anything that must survive updates has to be in `.gitignore` (step 2). Already there: `config.ini`, `config.remote.ini`, `notifications.json`, `api_tokens.json`, `*.db`, `kittyhack.log*`, `.venv/`, etc.

Update source is resolved via `resolved_update_repo()` in `helper.py` — driven by `CONFIG['UPDATE_REPOSITORY_MODE']` (`standard`|`custom`) and `CONFIG['UPDATE_REPOSITORY']` (format `owner/repo` or `owner/repo@ref`). Branch mode returns `<ref>@<sha7>` as the "latest version" so the UI's version comparison keeps working.

## WLAN watchdog — lessons from PR #155

Lives in `kittyhack_control.py:_wlan_watchdog_loop()`. Target-side only.

Design:
- Polls `is_gateway_reachable()` (pings default gateway) + `get_wlan_connections()` every 5 s.
- After 5 failed checks (~25 s): attempt reconnect (stop/start NetworkManager, try top-6 SSIDs).
- After 8 failed checks (~40 s): `/sbin/reboot`.
- **Wall-clock hard deadline:** 120 s outage → reboot regardless of counter (safety net for a hung reconnect).

**Every subprocess call in this path needs an explicit `timeout=`.** Without it, `nmcli connection up` blocks indefinitely when the AP is down, freezes the async loop, and the emergency reboot never fires. `systemctl()` wrapper has `timeout=15` default.

Post-reconnect: call `apply_wlan_runtime_settings()` to re-apply TX-Power + `power_save off` — Broadcom BCM43xx on Pi reverts these on re-association. Same helper is used by the boot-time init in both `server.py` and `kittyhack_control.py`.

## REST API (PR #153, feat/rest-api branch)

Under `/api/v1/*`. Three auth methods (priority order): `Authorization: Bearer`, `X-API-Key` header, `?token=`/`?api_key=` query param (for URL-only clients like Stream Deck).

Tokens in `api_tokens.json` next to `config.ini`, SHA-256 hashed. Managed via the **System tab** card (list/create/revoke) or CLI `tools/api_token.py`.

Routes:
- `GET /status` — door state + mode
- `GET|POST /door/{open,close,unlock_inside,lock_inside,unlock_outside,lock_outside}`
- `GET /mode` · `PUT|POST /mode` (JSON or query) · `GET|POST /mode/entry/{value}` · `GET|POST /mode/exit/{value}`
- `GET /cats` · `PUT|POST /cats/{ident}`
- `GET /events?limit=N`

Full values reference + curl examples: `doc/api.md`.

## Feature backlog + memory

Cross-session backlog of planned features is in the assistant's auto-memory at `project_kittyhack_feature_backlog.md`:
- A1 Outgoing Webhooks / ntfy
- A2 HA MQTT Autodiscovery
- A3 Basic-Auth for WebUI (manual activation, credentials required first)
- A4 RFID Auto-Registration
- B1 Cat-Statistics Dashboard (prey detections **per day**, not rate — user preference)
- B2 Event Video Clips
- B3 In-UI Log Viewer
- B4 Multi-Kittyflap orchestration
- B5 External watchdog / uptime pings

Read via memory tooling, not by re-exploring.

## Gotchas — will bite future-you

- **Windows cmd vs Git-Bash:** `#` is not a comment in cmd. Never paste multi-line shell commands with `#` comments into Windows cmd for this user — it breaks the chain silently (got hit by this during the v2.5.4 tag move).
- **Squash/rebase merges rewrite SHAs:** after `gh pr merge` on upstream, local tracking branch and upstream diverge even though content is identical. Use `git reset --hard upstream/main`, not `--ff-only`.
- **`git clean -fd` in updates** destroys untracked files. Every new persistent file must be added to `.gitignore`.
- **Modals and `btn_modal_cancel`:** multiple modals in the codebase reuse that button ID as their dismiss. It's intentional — the generic `modal_cancel()` handler is a no-op for unrelated modals. When writing a new confirmation modal, reuse `btn_modal_cancel` for cancel and use a **specific** ID for the OK action.
- **Theme-aware CSS:** Bootstrap theme (light/dark/auto) means hard-coded hex colors in inline styles break in one theme. Use `var(--bs-tertiary-bg)`, `var(--bs-body-color)`, `var(--bs-border-color)` with hex fallbacks.
- **`upstream` vs `origin` on the Kittyflap:** the production clone has only `origin` pointing at floppyFK/kittyhack. The dev clone on Windows has `origin`=fork and `upstream`=floppyFK. Commands differ.
- **Custom repo field format:** `owner/repo` or `owner/repo@ref`. Not `owner:ref` (that's GitHub's PR head-ref shorthand and it's not accepted).
- **Python not installed locally** on the dev Windows box — can't run py_compile or unit tests here. Syntax errors only surface on the Kittyflap (`sudo systemctl restart kittyhack`, then `journalctl -u kittyhack -n 200 --no-pager`).
