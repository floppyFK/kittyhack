# v2.5.3

Dieses Release erweitert den KI-Trainings-Workflow um eine Label-Studio-Integration, ergänzt bookmarkbare Tab-URLs und verbessert die Bilder- und Event-Ansichten.

## Neue Features
- **Konfiguration von Label-Studio-Projekten**: Ein API-Token und ein Label-Studio-Projekt können nun direkt in der WebGUI hinterlegt werden.
- **Training direkt aus einem Label-Studio-Projekt**: Ein konfiguriertes Label-Studio-Projekt kann nun direkt aus Kittyhack exportiert und zum Modelltraining übergeben werden, ohne vorher manuell eine ZIP-Datei herunterladen zu müssen.
- **Bilder an Label Studio senden**: Das aktuell sichtbare Bild in einem Event oder in der Bilder-Ansicht können nun direkt an das konfigurierte Label-Studio-Projekt gesendet werden.
- **Bookmarkbare Tab-URLs**: Hauptbereiche wie Live-View, Bilder, KI-Training, System oder Konfiguration besitzen nun eigene URL-Pfade, sodass Reloads, Bookmarks und geteilte Links wieder im selben Tab landen.

## Verbesserungen
- **Event-Scrubber**: Das Durchschalten von Bildern in der Event-Ansicht funktioniert jetzt deutlich performanter.
- **Bilder-Sektion**: Die nicht gruppierte Bilder-Ansicht wurde zu einer modernen Galerie mit Pager und Schnellaktionen für Download, Löschen und den Versand an Label Studio überarbeitet.

## Bugfixes
- **Slider-Feedback auf Mobilgeräten**: Eingabe-Slider werden nach Änderungen nun auch auf mobilen Geräten zuverlässig hervorgehoben.
- **Tab-Wiederherstellung nach Reload/Reconnect**: Der aktive Tab wird nach Seiten-Reloads und Reconnects nun zuverlässiger wiederhergestellt.