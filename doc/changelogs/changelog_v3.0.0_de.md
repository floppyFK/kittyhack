# v3.0.0

**Wichtig:** Dieses Release setzt mindestens **v2.7.0** voraus. Wenn du noch eine ältere Version verwendest, führe das Update bitte über das Installationsskript aus (dasselbe wie bei der Erstinstallation, siehe [Setup Script ausführen](https://github.com/floppyFK/kittyhack#2-setup-script-ausf%C3%BChren)) und nicht über das Update in der Weboberfläche.

## Neue Features

- **Statistik**: Neuer Tab *Statistik* mit Ein- und Ausgängen, Beuteerkennungen, Zeit drinnen/draußen und typischen Aktivitätszeiten - auch pro Katze und für verschiedene Zeiträume.
- **Drinnen/draußen Status**: Die *Live-Ansicht* zeigt, wo jede Katze ist und seit wann. Tippe auf den Zustand, um ihn manuell zu ändern. Unter *Katzen* kannst du eine Katze dort ausblenden.
- **Bewegungssensor als Bestätigung bei Kamera-Einlass**: Optionale Einstellung unter *Einstellungen*. Wenn die Kamera Katzen erkennt, öffnet sich die Klappe nur, wenn auch der äußere Bewegungssensor auslöst. Hilfreich, wenn Sonne oder Spiegelungen manchmal eine Katze vortäuschen.
- **Hinweise ausblenden**: Manche Hinweise können mit *Nicht mehr benachrichtigen* ausgeblendet werden. Unter *Einstellungen* kannst du sie später wieder aktivieren.

## Verbesserungen

- **Überarbeitete Oberfläche**: Die Weboberfläche wurde neu gestaltet. Die Texte sind verständlicher, Karten und Schaltflächen übersichtlicher, und die Seite ist im hellen wie im dunklen Erscheinungsbild besser lesbar.
- **Einfacheres Menü**: Eine neue Katze wird jetzt auf der Seite *Katzen* hinzugefügt. Die WLAN-Einstellungen findest du im Tab *System*. Aus dem Tab *Konfiguration* wurde *Einstellungen*.
- **Bilder**: Der Tab *Bilder* wurde neu aufgebaut. Die Filter sitzen übersichtlich über den Bildern, weitere Bilder erscheinen beim Scrollen, und mit *Zu heute wechseln* kommst du zurück zum heutigen Tag.
- **Ereignisse**: In der Zeitlinie eines Ereignisses siehst du, wann die Klappe entriegelt oder verriegelt wird. Abschnitte ohne Bild sind klarer zu erkennen. Vor dem Löschen wird nachgefragt.
- **Entscheidungslogik**: Unter *Einstellungen* zeigt Kittyhack die Schritte für Ein- und Ausgang als übersichtliche Folge, passend zu der Option, die du gewählt hast.
- **Verbindungsabbruch**: Wenn die Verbindung abreißt, erscheint ein klarer Hinweis. Mit *Jetzt versuchen* verbindest du dich sofort neu.
- **Hinweis zum mitgelieferten Modell**: Solange noch das originale Modell der Kittyflap aktiv ist, erinnert Kittyhack daran. Es erkennt deine eigenen Katzen kaum. Ein selbst trainiertes Modell ist deutlich genauer.
- **Optimierung fürs Smartphone**: Die Weboberfläche bleibt auf dem Smartphone zuverlässiger verbunden, wenn der Browser mal kurz im Hintergrund ist.
- **Schnellere Erkennung**: Die Katzen- und Beuteerkennung auf der Kittyflap ist nun etwas schneller.
- **Modell-Training**: Kittyhack meldet sich jetzt am Server für das Modell-Training an. Ältere Versionen der Reihe 2.x funktionieren noch für eine kurze Übergangszeit. Danach ist das Update auf v3 nötig, um Modelle zu trainieren.
- **Remote-Modus**: Läuft Kittyhack auf einem separaten Computer, fällt die Installation kleiner aus. Die Erkennung über Prozessor oder Grafikkarte bleibt möglich. Beim Wechsel führt Kittyhack die erforderlichen zusätzlichen Installationen im Hintergrund aus.
- **Sichern und Wiederherstellen**: Einstellungen werden nicht mehr in einer eigenen config.ini gespeichert. Alle Informationen liegen in der Kittyhack-Datenbank, die du unter *Info* sichern und wieder einspielen kannst. Eine config.ini aus einer älteren Version kannst du dort weiterhin wiederherstellen.

## Bugfixes

- **Durchschnittliche FPS**: Die Anzeige der durchschnittlichen FPS pro Modell in der Modellverwaltung wurde nicht mehr aktualisiert. Das ist jetzt behoben.
- **Remote-Update von einem eigenen Branch**: Ein Update der Kittyflap über ein Remote-Gerät verwendet jetzt das Update-Repository, das auf dem Remote-Gerät eingestellt ist.
- **Update-Fenster auf einem Remote-Gerät**: Während eines Updates verdeckt der Hinweis "Remote-Verbindung getrennt" nicht mehr das Update- und Neustart-Fenster.
- **Zu große Datei beim Modell-Training**: Ist die Datei zu groß, erscheint eine klare Meldung statt eines unklaren Fehlers.
