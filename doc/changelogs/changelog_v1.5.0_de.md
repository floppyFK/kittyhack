# v1.5.0

## Highlights
Mit Version 1.5.0 beginnen die ersten Vorbereitungen, damit du die "KI" an deine Katze und deine Umgebung anpassen kannst! 
Zunächst müssen viele Daten gesammelt werden, um die "KI" später trainieren zu können.  

Was bedeutet das konkret?  
Für eine zuverlässige Objekterkennung werden später **mindestens** 100 Bilder für die Kategorie *Keine Maus* und ebenfalls mindestens 100 Bilder für die Kategorie *Maus* benötigt. (Vögel und andere Tiere werden dabei einfach als Maus klassifiziert 😉).  
In der Event-Ansicht gibt es nun einen Download-Button, über den du alle Bilder eines Ereignisses als *.zip*-Datei herunterladen kannst.  
Du solltest möglichst viele verschiedene Bilder sammeln. Je größer später die Varianz ist, desto besser – also sammle Fotos bei Tag, bei Nacht, bei Sonnenschein mit starken Schattenwürfen usw.  

> **Hinweis:** Beim Herunterladen der Bilder als *.zip*-Datei kann dein Browser (z. B. Google Chrome) eine Warnung anzeigen, dass die Verbindung nicht sicher ist und der Download blockiert wurde. Diese Meldung ist unbedenklich, solange du nur aus deinem heimischen WLAN auf die Kittyflap zugreifst. Falls nötig, musst du bestätigen, dass du die Datei trotzdem behalten möchtest.  
> Eine sichere Verbindung über HTTPS schafft hier Abhilfe. Anleitungen dazu findest du online – ein möglicher Ansatz ist ein *Reverse Proxy*, wie beispielsweise der [NGINX Proxy Manager](https://nginxproxymanager.com/).

Zusätzlich wurde eine neue Modellvariante für die Objekterkennung als Standard ausgewählt. Damit sollte die Fehldetektion von vermeintlichen Mäusen an Terrassenmöbeln oder ähnlichen Objekten reduziert werden. Ob dieses Modell in allen Situationen besser funktioniert, lässt sich nicht pauschal sagen – daher kannst du zwischen der neuen und der alten Variante wechseln.

---------------

## Neue Features
- **Bilder-Download**: Bilder können nun pro Event heruntergeladen werden.
- **Bessere Performance**: Die Objekterkennung arbeitet nun deutlich schneller. Bisher wurden etwa 3 Bilder pro Sekunde analysiert, mit dieser Version sind es 5-6 Bilder pro Sekunde!
- **Wahl zwischen verschiedenen Modellen**: Es kann nun zwischen zwei verschiedenen Varianten zur Objekterkennung gewählt werden
- **Einstellbare Verriegelung bei erkannter Beute**: Im Konfigurationsmenü lässt sich nun eine Sperrzeit für die Klappe festlegen, wenn eine Beute erkannt wurde. Ist die Sperrzeit aktiv, wird ein entsprechender Hinweis auf der Startseite angezeigt. Diese Sperrung kann über einen Button vorzeitig aufgehoben werden.

## Verbesserungen
- **Speichern von Ereignissen bei Bewegung**: Ein Ereignis wird nur noch dann angelegt, wenn der äußere Bewegungsmelder eine Bewegung erkennt und in mindestens einem der Bilder der Wert für die `Minimale Erkennungsschwelle` (*Maus* oder *Keine Maus*) überschritten wurde. In diesem Fall werden **alle** zugehörigen Bilder während des Ereignisses gespeichert.
- **Gepufferte Bilder**: Da der äußere Bewegungsmelder etwa 2-3 Sekunden benötigt, um eine Bewegung zu melden, werden Kamerabilder nun für mehrere Sekunden gepuffert. Dadurch sollten auch sehr schnelle Katzen zuverlässig in den ausgewerteten Bildern zu sehen sein.
- **Neues Konfigurationsmenü**: Das Panel *Konfiguration* wurde übersichtlicher gestaltet.

## Bugfixes
- **Kontinuierliche Auswertung**: Wenn die Mindestanzahl an auszuwertenden Bildern bereits erreicht ist und erst in einem der nachfolgenden Bilder eine Maus erkannt wird, wird die Klappe nun auch dann wieder verriegelt.
- **Ausgehende Ereignisse**: Ein Fehler wurde behoben, durch den Ereignisse nicht gespeichert wurden, wenn eine Katze nach draußen ging.

## Hinweis zur Speicherverwaltung
Da nun deutlich mehr Bilder gespeichert werden als in vorherigen Versionen, empfiehlt es sich, die `Maximale Anzahl an Bildern in der Datenbank` zu erhöhen.  
Je nach Ausstattung deiner Kittyflap (16 GB oder 32 GB Speicher) kannst du den Wert entsprechend anpassen.  
**8000** Bilder sollten in beiden Varianten problemlos möglich sein.  
Überwache den freien Speicherplatz am besten im **Info**-Panel. Als Richtwert gilt: Pro 1000 Bilder werden etwa **200 MB** Speicherplatz (100MB Datenbank + 100MB Backup) benötigt.
