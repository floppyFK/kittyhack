# v1.5.1

## Verbesserungen
- **Bilder für Event-Ansicht werden jetzt gepuffert**: Die Bilder, die in der Event-Ansicht angezeigt werden, sind eine verkleinerte Version der in der Datenbank abgelegten Bilder. Diese wurden bisher on-the-fly generiert, sobald der Button mit der Lupe geklickt wurde. Bei Events mit vielen Bildern konnte es dadurch zu erheblichen Wartezeiten kommen. Diese verkleinerten Versionen werden nun direkt beim Erstellen neuer Bilder mit angelegt.
> Hinweis: Bei Bildern in der Datenbank, die mit v1.5.0 oder früher angelegt wurden, fehlen diese Vorschauvarianten noch. 
> Diese werden im Hintergrund erst nach und nach angelegt, daher kann es direkt nach dem Update auf v1.5.1 einmalig noch etwas länger dauern, wenn du die Bilder eines Events ansehen möchtest.
- **Fallback zu Objekterkennung auf nur einem CPU-Kern**: Falls es zu Neustarts oder System-Freezes kommt, wenn die Objekterkennung startet (ausgelöst durch einen der Bewegungsmelder), kannst du nun probehalber im Konfigurations-Panel die Berechnung auf nur einen CPU-Kern umstellen, statt auf alle Kerne (Neustart erforderlich). Näheres dazu in Issue #72.

## Bugfixes
- **Anzahl Bilder pro Event limitiert**: Die maximale Anzahl an Bildern pro Ereignis ist nun limitiert. Das Limit kann im Konfigurations-Panel angepasst werden (jeweils für Bewegung mit erkannter RFID und für Bewegung ohne erkannter RFID).
- **Internes Limit für Bilder-Cache**: Die maximale Anzahl für die intern gepufferten Bilder ist nun limitiert, um einen Speicherüberlauf zu verhindern.
- **Sperrzeit nach Beuteerkennung**: Der Wert für die Sperrzeit nach Beuteerkennung wird im Konfigurations-Panel jetzt korrekt abgespeichert.
- **Favicon auf Android und iOS**: Wenn du eine Verknüpfung zur Kittyhack-Seite auf den Homescreen deines Smartphones legst, wird nun das korrekte Favicon angezeigt.

---------

## Bekannte Probleme:
- **Darstellung auf iOS**: Die Darstellung der Ereignis-Übersicht auf iOS-Geräten ist defekt und die Buttons zur Anzeige der Events funktionieren nicht.