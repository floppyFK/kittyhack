# v2.7.0

## Neue Features

- **Beta-Updates**: Unter *Konfiguration* kannst du als Update-Quelle nun **Beta** wählen, um Beta-Releases frühzeitig zu erhalten.

## Verbesserungen

- **MQTT-Zuverlässigkeit**: Die MQTT-Verbindung stellt sich nach Unterbrechungen zuverlässiger wieder her (z. B. Home-Assistant-Integration).
- **Wechsel der Update-Quelle**: Ein Wechsel des Update-Repositorys (Standard / Beta / Benutzerdefiniert) startet kein Update mehr automatisch. Kittyhack prüft die neue Quelle; das Update kannst du bei Bedarf selbst im Tab *Info* starten.
- **Datenbank im Log-Download**: Der Logdatei-Download im Tab *Info* enthält jetzt auch einen Snapshot der Kittyhack-Datenbank.

## Bugfixes

- **Sofortige Verriegelung nach Durchgang**: Bei aktivierter Option *Sofortige Verriegelung nach Durchgang* öffnet sich die Klappe nicht mehr fälschlicherweise erneut für dieselbe Katze, die gerade hinausgegangen ist.
- **Sichereres Speichern der Konfiguration**: Einstellungen gehen nicht mehr verloren, wenn während dem Speichern von Einstellungen ein Reboot ausgelöst wird.