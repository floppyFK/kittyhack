# Locale Workflow

The German PO file is the editable translation source and should stay versioned in git.

English uses gettext fallback to msgids (default source strings), so no `locales/en` catalog is required.
The MO file is a compiled runtime artifact and does not need to be versioned.

# UI Writing Style

Kittyhack uses one warm, calm voice for the whole web UI. Write for anyone in
the household and use plain language before technical terminology.

- German addresses one person with **du**. English uses **you**.
- Use sentence case. Navigation and section headings are short nouns.
- Start button labels with a verb. Name the object only when the surrounding
  screen does not make it clear: “Save”, “Save changes”, “Restart Kittyflap”.
- Ask one direct question before destructive actions. Keep the action button
  short: “Delete” / “Löschen”.
- Keep success messages short and in the past tense. Errors say what happened
  and, where useful, what to do next.
- Use exclamation marks only when the content genuinely requires urgency. Use
  the ellipsis character (`…`) for ongoing work.
- Keep help text to one or two sentences: explain what Kittyhack does, then the
  consequence of the choice. Avoid `NOTE:` / `HINWEIS:` labels.

Use these product terms consistently:

- **Kittyhack** is the software; **Kittyflap** is the physical device.
- Use **flap / Klappe**, **unlock / entriegeln**, **lock / verriegeln**, and
  **inside / innen**, **outside / außen**.
- Use **prey / Beute** in household-facing text. Use **mouse / Maus** only when
  the detector specifically means a mouse, such as in AI Training.
- Event summaries are complete sentences. Timeline entries are short facts.

Technical terms such as YOLO, TFLite, Label Studio, MQTT, and SSID are
appropriate in Training, API, WLAN, Home Assistant, Advanced, and remote-mode
content. Keep them out of everyday screens and flap settings where plain
language is sufficient.

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