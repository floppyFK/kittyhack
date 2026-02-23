# v2.5.0

Dieses Release bringt Theme-Unterstützung (Hell/Dunkel/Auto), die Möglichkeit um die CPU intensiven Tätigkeiten auf einen separaten PC auszulagern, eine deutlich überarbeitete Event-Ansicht (Scrubber/Marker/Downloads) sowie viele Performance-Optimierungen und Bugfixes.

## Neue Features
- **Theme-Support (Hell/Dunkel/Auto)**: In der WebGUI kann nun zwischen Light/Dark/Auto umgeschaltet werden.
- **Remote-Control-Modus**: Kittyhack kann auf einem leistungsstärkeren Remote-PC laufen (schnellere Inferenz, Katzen-/Beute-Erkennung in Echtzeit), während die Katzenklappe weiterhin für Sensoren/Verriegelungen verbunden bleibt. Details & Setup: https://github.com/floppyFK/kittyhack/blob/main/doc/remote-mode_de.md
- **Event-View überarbeitet**:
  - **Scrubber/Timeline** im Event-Modal zur schnellen Navigation innerhalb eines Events.
  - **Marker-Overlay** auf der Timeline für Frames mit erkannten Labels (Beute in Rot, Katze in Grün).
  - **Download einzelner Bilder**: Wenn ein Event pausiert ist, kann das aktuell angezeigte Bild direkt heruntergeladen werden.
- **Bilder-Sektion überarbeitet**: Die bisherigen Tabs wurden durch eine Navigationsleiste ersetzt (wenn nicht zu Events gruppiert).
- **IP-Kamera-Stream-Downscaling**: Streams von IP-Kameras mit höheren Auflösungen können nun direkt in den IP-Kamera-Einstellungen herunterskaliert werden.

## Verbesserungen
- **Performance & RAM**: Ressourcenintensive Schritte wurden von der Katzenklappe in den Webclient verlagert.
- **UI/UX**: Viele kleine UI-Verbesserungen (Styles, Icons, Tooltips, Dark-Theme-Details).
- **Konfiguration (Entscheidungszeitpunkt fürs Entriegeln)**: Die Verzögerung bis zur Entscheidung zur Entriegelung wird jetzt in **Sekunden** nach einem Bewegungs-Trigger eingestellt (statt über eine Anzahl von Bildern).
