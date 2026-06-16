# v2.6.0

## Highlights

- (Nur Remote-Modus) Hardware-Beschleunigung für IP-Kamera-Streams
- (Nur Remote-Modus) Experimentelle Hardware-Unterstützung für Inferenz auf kompatiblen Geräten
- Verbesserte WLAN-Stabilität und automatische Wiederherstellungsmechanismen
- Bessere RFID- und Video-Erkennungslogik

---------------

## Neue Features

- **Hardware-Decodierung für IP-Kameras**: Experimentelle Option zur Nutzung von Hardware-Beschleunigung für H264/H265-Videostreams von IP-Kameras im Remote-Modus (nicht verfügbar, wenn Kittyhack direkt auf der Kittyflap läuft!)
- **Hardware-Inference-Unterstützung**: Experimentelles Feature zur Nutzung von Hardware-Beschleunigung für Objekterkennung, verfügbar auf unterstützten Plattformen (nicht verfügbar, wenn Kittyhack direkt auf der Kittyflap läuft!).
- **GitHub-PR-Referenz-Kurzform**: Das Feld für benutzerdefinierte Update-Repositorys akzeptiert nun GitHub-Pull-Request-Head-References im Format `owner:branch` (z.B. `FabulousGee:feat/xyz`), was das Testen von Contributor-Zweigen direkt von PR-Seiten erleichtert.
- **Validierung von benutzerdefinierten Repositorys**: Das Konfigurationssystem überprüft nun, ob benutzerdefinierte Update-Repositorys auf GitHub existieren, bevor sie gespeichert werden, um stille Fehler beim Auto-Update zu verhindern.

## Verbesserungen

- **IP-Kamera-Stabilität**: Verbesserte Frame-Erfassungs-Timing und Pufferverwaltung für H264/H265-Streams zur Steigerung der Decodierstabilität, besonders bei GPU-Inference.
- **FPS-Beschränkungen und Puffern**: Hinzugefügte konfigurierbare FPS-Beschränkungen und adaptive Pufferung für IP-Kamera-Streams, um unnötige CPU Last zu reduzieren.
- **Ereignis-Zeitlinie**: Verbesserte und detailliertere Darstellung der einzelnen Schritte pro Ereignis (nur für neue Events nach dem Update verfügbar)

## Bugfixes

- **RFID-Erkennungs-Priorität**: Im Eingangs-Modus "Nur registrierte Katzen" und "Individuelle Konfiguration pro Katze" haben ausgelesene RFID-Tags nun immer Priorität über eine (möglicherweise falsche) Erkennung via Video (#171).
- **WLAN-Watchdog**: Doppelte Sicherheit beim WLAN Watchdog eingeführt - wenn der Retry-Mechanismus bei fehlender WLAN Verbindung versagt, wird spätestens nach 120 ein Neustart ausgeführt.

## Kleinere Änderungen

- **Übersetzungs-Workflow**: Aktualisierung des deutschen Übersetzungs-Generierungsprozesses und der Dokumentation.
- **WLAN-Laufzeit-Einstellungen**: Die WLAN-Stabilität wurde verbessert, indem Konfigurationen (TX-Power, Power Save) nach einer Wiederverbindung erneut angewendet werden.