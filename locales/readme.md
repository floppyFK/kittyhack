# Initial Setup

*NOTE:* Run these commands in the project root folder!  
The steps below require gettext (on ubuntu systems install it with `apt install gettext`).

##### Initially create POT file:
```
xgettext -d messages -o locales/messages.pot src/server.py --from-code UTF-8
xgettext -d messages -o locales/messages.pot src/ui.py --from-code UTF-8 --join-existing
```

##### Initially create po file:
```
msginit -l de_DE.UTF-8 -o locales/de/LC_MESSAGES/messages.po -i locales/messages.pot --no-translator
msginit -l en_EN.UTF-8 -o locales/en/LC_MESSAGES/messages.po -i locales/messages.pot --no-translator
```

##### Create mo file from po file:
```
msgfmt -o locales/de/LC_MESSAGES/messages.mo locales/de/LC_MESSAGES/messages.po
msgfmt -o locales/en/LC_MESSAGES/messages.mo locales/en/LC_MESSAGES/messages.po
```

# Update locales

##### Update POT file:
```
xgettext -d messages -o locales/messages.pot src/server.py --from-code UTF-8
xgettext -d messages -o locales/messages.pot src/ui.py --from-code UTF-8 --join-existing
```

##### Merge existing po file with new values from POT:
```
msgmerge -U locales/de/LC_MESSAGES/messages.po locales/messages.pot
msgmerge -U locales/en/LC_MESSAGES/messages.po locales/messages.pot
```

##### Update mo file:
```
msgfmt -o locales/de/LC_MESSAGES/messages.mo locales/de/LC_MESSAGES/messages.po
msgfmt -o locales/en/LC_MESSAGES/messages.mo locales/en/LC_MESSAGES/messages.po
```