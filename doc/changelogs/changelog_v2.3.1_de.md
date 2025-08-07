# v2.3.1

## Neue Features
- **Schnellzugriff auf Eingangs- und Ausgangssteuerung**: Die Konfiguration der Ein- und Ausgangsrichtung ist jetzt direkt auf der Startseite möglich. Ein Wechsel zum Konfigurations-Tab ist nicht mehr erforderlich.

## Bugfixes
- **Watchdog für WLAN Verbindung**: Bei Verlust der WLAN-Verbindung wird nun automatisch versucht, die Verbindung wiederherzustellen. Nach mehreren erfolglosen Versuchen erfolgt ein automatischer Neustart der Kittyflap (standardmäßig aktiv, kann in den Advanced-Einstellungen aber deaktiviert werden).
- **Event Fenster**: Ein Event kann nun auch durch einen Klick außerhalb des Event-Fensters geschlossen werden
- **Schwellwert für Katzenerkennung**: Der Minimalwert für diese Einstellung ist jetzt an den den Wert *Minimale Erkennungsschwelle* gekoppelt, da niedrigere Werte hier keinen Sinn machen würden.

## Verbesserungen
- **IP Kamera Watchdog**: Die Verbindung zur IP-Kamera wird jetzt automatisch neu aufgebaut, sobald fehlerhafte oder beschädigte h264-Streams erkannt werden (standardmäßig aktiv, kann in den Advanced-Einstellungen aber deaktiviert werden).