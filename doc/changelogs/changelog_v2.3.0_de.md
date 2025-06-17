# v2.3.0

## Neue Features
- **Home Assistant Integration**: Die Katzenklappe kann jetzt über MQTT in Home Assistant eingebunden werden (ein MQTT-Broker wird benötigt)!
  - Unterstützte Entitäten:
    - Bewegungsmelder (innen/außen)
    - Verriegelungszustand (innen/außen)
    - Steuerung der Verriegelung (innen)
    - Letztes Ereignis
    - Erkannte Beute
    - Steuerung der Ausgangsrichtung
    - Steuerung der Eingangsrichtung

## Kleinere Änderungen
- **Konfigurations-Download**: Im Tab `Info` kann nun neben der Datenbank auch die aktuelle Konfiguration heruntergeladen werden.

## Verbesserungen
- **Bilder-Tab**: Die Einstellungen "Nur erkannte Katzen anzeigen" und "Nur erkannte Mäuse anzeigen" werden jetzt dauerhaft gespeichert.
- **Modelltraining**: Bei einem fehlgeschlagenen Modelltraining wird nun eine Fehlermeldung im Web-Interface angezeigt.
- **Sensible Daten**: Sensible Informationen wie Passwörter werden in den Log-Dateien nun maskiert.