# v2.0.0


## Highlights
Es ist soweit! Mit dieser Version hast du die Möglichkeit, die KI individuell auf deine Katze zu trainieren. Keine Chance mehr für Mäuse 😉

> ⚠️ **ACHTUNG:** Für dieses Update müssen ca. 250 MB an Daten aus dem Internet nachgeladen und installiert werden. Der Updatevorgang wird also deutlich länger dauern als gewohnt!
> Am sichersten ist es, wenn du für diese Version das Update über das Setup-Script ausführst:
> ```bash
> sudo curl -sSL https://raw.githubusercontent.com/floppyFK/kittyhack/main/setup/kittyhack-setup.sh -o /tmp/kittyhack-setup.sh && sudo chmod +x /tmp/kittyhack-setup.sh && sudo /tmp/kittyhack-setup.sh de && sudo rm /tmp/kittyhack-setup.sh
> ```
>
> Du kannst das Update auch über das Web-Interface ausführen. Dabei kann es aber passieren, dass während des Updates die Meldung "Connection Lost" erscheint - **keine Sorge, die Installation läuft im Hintergrund weiter!**
> Bitte warte **mindestens 15 Minuten** (oder länger, falls du eine sehr langsame Internetverbindung hast), und lade dann die Seite neu. Anschließend musst du die Katzenklappe noch einmal neu starten und du bist auf dem Stand der v2.0!

---------------

## Neue Features
- **KI Training**: Individuelles Training der KI auf deine Katze und deine Umgebung. Mit Hilfe des eingebauten Label Studio Server kannst du kontinuierlich das Modell verbessern, das zur Auswertung der Kamerabilder verwendet wird.
- **Entsperren über Bildauswertung**: Neben der Entsperrung anhand der RFID deiner Katze gibt es jetzt auch die Möglichkeit, die Klappe anhand der Bilderkennung zu entriegeln.

## Verbesserungen
- **Design Anpassung**: Alle Tabs haben nun ein einheitliches Design.
- **Übersetzung**: Alle Kacheln sind jetzt vollständig auf deutsch verfügbar.
- **Automatischer Reconnect**: Wenn die Verbindung zur Kittyhack unterbrochen wurde (etwa weil du den Browser auf deinem Smartphone minimiert hast), wird diese nun automatisch wieder hergestellt.

## Bugfixes
- **Darstellung auf iOS**: Die Darstellung der Event-Liste auf iOS Geräten wurde gefixt.

## Kleinere Änderungen
- **Logfile Download**: Es werden nun über einen Button alle relevanten Logs auf einmal heruntergeladen
- **Absturzerkennung**: Die Software erkennt nun, ob es unerwartete Abstürze gab. Bei mehreren aufeinanderfolgenden Abstürzen wird eine Warnmeldung angezeigt.