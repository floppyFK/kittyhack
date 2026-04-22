# Locale Workflow

The German PO file is the editable translation source and should stay versioned in git.

English uses gettext fallback to msgids (default source strings), so no `locales/en` catalog is required.
The MO file is a compiled runtime artifact and does not need to be versioned.

At startup, kittyhack performs a locale refresh check and regenerates catalogs only when needed:

1. Check if gettext tools are available (`xgettext`, `msgmerge`, `msgfmt`).
2. If missing, try to install gettext automatically on Debian-based systems.
3. Build a temporary POT from `app.py` and all Python files in `src/`.
4. Merge the tracked German PO with the current POT in a temporary file (the tracked PO is not modified at runtime).
5. Compile the German MO from that merged temporary file (`msgfmt`).
6. Store a small state file so this runs again only when locale inputs changed.

This keeps the tracked translation source clean while still creating up-to-date runtime catalogs.

# Updating German Translations During Development

Whenever gettext source strings `_("...")` are added or changed in code, update the German PO file before committing.

Run these commands from the project root:

```bash
xgettext -d messages -o /tmp/messages.pot --from-code UTF-8 --no-location app.py $(find src -type f -name '*.py' | sort)
msgmerge --update --backup=none --no-location locales/de/LC_MESSAGES/messages.po /tmp/messages.pot
```

This workflow should be repeated regularly while developing features that touch user-visible text.

After merging, open `locales/de/LC_MESSAGES/messages.po` and translate new or changed entries.

# Runtime behavior

- Locale regeneration runs before translations are loaded on process startup.
- No extra restart is needed for that startup.
- If locale-relevant source code changes while kittyhack is already running, the new texts are applied on the next service start.