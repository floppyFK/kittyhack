# v2.4.0

## Neue Features
- **Ein- und Ausgang pro Katze** (BETA): Eingang und Ausgang können jetzt für jede Katze separat gesteuert werden.
  - Hinweis für Home Assistant: Durch diese Erweiterung haben sich einige Entitäten geändert. Bitte die aktualisierte Dashboard-Konfiguration aus dem Konfigurations-Tab verwenden.

## Bugfixes
- **MQTT**: Veraltete `object_id`-Werte für Home Assistant wurden entfernt.
- **Logging**: Logfiles wurden teilweise nicht korrekt geschrieben; dieses Verhalten ist behoben.
- **Konfigurationsdatei**: Fehlerhafte Einträge in `config.ini` führen nicht mehr zu Abstürzen.
- **Bildansicht**: Die klassische (ungruppierte) Bildansicht funktioniert wieder.

## Kleinere Änderungen
- **RFID-Reader deaktivieren**: Im Konfigurations-Tab kann der RFID-Reader optional komplett deaktiviert werden (z. B. bei Hardwaredefekt, der sonst einen Neustart der Katzenklappe verursacht).
- **Backup & Wiederherstellung**: Es können nun sowohl Datenbank- als auch Konfigurations-Backups erstellt und wiederhergestellt werden.

## Verbesserungen
- **Datenbank**: Durch ein angepasstes Datenbankkonzept reduziert sich der Bedarf an freiem Speicherplatz um ca. 50 %.
- **Schnellerer Start**: Die Startdauer der Anwendung wurde insbesondere bei großen Datenbanken deutlich reduziert.
- **Konfiguration**: Der Konfigurations-Tab ist übersichtlicher. Detailbeschreibungen sind nun in ausklappbaren Elementen zusammengefasst.
- **Entscheidungslogik**: Die Logik zur Freigabe der Ein- bzw. Ausgangsrichtung kann im Konfigurations-Tab als Diagramm angezeigt werden.
- **RFID-Validierung**: Im Tab "Katzen verwalten / Katzen hinzufügen" wird die eingegebene RFID auf korrekte Länge und zulässige Zeichen überprüft.
- **Neustart-Hinweis**: Bei Änderungen, die einen Neustart erfordern (z. B. Auswahl eines anderen Modells), wird nun ein deutlicher Hinweis angezeigt.
- **Datenbank-Backup**: Das Backup-Konzept wurde grundlegend überarbeitet. Nächtliche Backups sind jetzt deutlich schneller und verursachen weniger Systemlast.