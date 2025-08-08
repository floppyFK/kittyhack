# v2.3.1

## Neue Features
- **Schnellzugriff auf Eingangs- und Ausgangssteuerung**: Die Konfiguration der Ein- und Ausgangsrichtung ist jetzt direkt auf der Startseite möglich. Ein Wechsel zum Konfigurations-Tab ist nicht mehr erforderlich.

## Bugfixes
- **Manuelles sperren/entsperren**: 
  - ⚠️ Wenn die Katzenklappe im Live View Tab manuell gesperrt oder entsperrt wurde, dann konnte dies unter bestimmten Umständen zu einem kompletten Systemabsturz führen
  - Wenn alle Bedingungen für das entsperren der Innenseite gegeben waren, dann konnte die Klappe durch ein manuelles Sperren nicht zuverlässig verriegelt werden (Notfall-Verriegelung)
- **Event Fenster**: Ein Event kann nun auch durch einen Klick außerhalb des Event-Fensters geschlossen werden
- **Schwellwert für Katzenerkennung**: Der Minimalwert für diese Einstellung ist jetzt an den den Wert *Minimale Erkennungsschwelle* gekoppelt, da niedrigere Werte hier keinen Sinn machen würden.
- **Live View**: Der äußere Bewegungs-Status im Live View Tab zeigt nun korrekt den Zustand der Kameradetektion an, wenn diese als Quelle für die Bewegungserkennung außen konfiguriert ist (`Kamera für die Bewegungserkennung verwenden`). Vorher wurde fälschlicherweise immer der PIR-Bewegungsmelder-Status angezeigt.

## Verbesserungen
- **Watchdog für WLAN Verbindung**: Bei Verlust der WLAN-Verbindung wird nun automatisch versucht, die Verbindung wiederherzustellen. Nach mehreren erfolglosen Versuchen erfolgt ein automatischer Neustart der Kittyflap (standardmäßig aktiv, kann in den Advanced-Einstellungen aber deaktiviert werden).
- **IP Kamera Watchdog**: Die Verbindung zur IP-Kamera wird jetzt automatisch neu aufgebaut, sobald fehlerhafte oder beschädigte h264-Streams erkannt werden (standardmäßig aktiv, kann in den Advanced-Einstellungen aber deaktiviert werden).