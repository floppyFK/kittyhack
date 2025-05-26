# v2.1.0

## Neue Features
- **PWA Support**: Die Kittyflap-Benutzeroberfläche kann jetzt als Progressive Web App auf Mobilgeräten und Desktops installiert werden. So hast du einen schnelleren Zugriff auf die Katzenklappe ohne den Browser öffnen zu müssen (Für diese Funktion wird eine HTTPS-Verbindung benötigt)  
  > ℹ️ Weitere Infos dazu im **`Info`**-Tab
- **Konfiguration des Hostnamen**: Der Hostname der Kittyflap kann jetzt über die Einstellungen angepasst werden. Dies ermöglicht einen einfacheren Zugriff auf die Katzenklappe über einen benutzerdefinierten Namen im lokalen Netzwerk (z.B. `http://kittyflap.local`).
- **Bewegungserkennung per Kamera**: Alternativ zum äußeren PIR-Sensor kann nun auch das Kamerabild zur Erkennung von Bewegung genutzt werden. Dies reduziert Fehlauslösungen durch Bewegungen von Bäumen oder Menschen im Bild deutlich. Voraussetzung ist jedoch ein gut trainiertes, eigenes Erkennungsmodell.  
  > ℹ️ Die Option findest du unter **`Konfiguration`** -> **`Kamera für die Bewegungserkennung verwenden`**.


## Verbesserungen
- **Zusätzliche Event Infos**: Die Event-Liste zeigt jetzt zusätzliche Icons bei manuellen Entriegelungen / Verriegelungen während eines Events an.
- **Konfigurations-Tab**: Geänderte Eingabefelder im Konfigurations-Tab werden nun vor dem Speichern optisch hervorgehoben. Dadurch sind Fehleingaben (z. B. beim Scrollen auf Smartphones) leichter erkennbar.

## Bugfixes
- **RFID-Reader**: Das Ausleseverhalten des RFID-Readers wurde weiter verbessert
