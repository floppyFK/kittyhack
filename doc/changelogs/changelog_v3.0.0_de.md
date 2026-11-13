# v3.0.0

**Wichtig:** Dieses Release setzt mindestens **v2.7.0** voraus. Wenn du noch eine ältere Version verwendest, führe das Update bitte über das Installationsskript aus (dasselbe wie bei der Erstinstallation, siehe [Setup Script ausführen](https://github.com/floppyFK/kittyhack#2-setup-script-ausf%C3%BChren)) und nicht über das Update in der Weboberfläche.

## Neue Features

- **Statistik**: Neuer Tab *Statistik* mit Ein- und Ausgängen, Beuteerkennungen, Zeit drinnen/draußen und typischen Aktivitätszeiten - auch pro Katze und für verschiedene Zeiträume.
- **Drinnen/draußen Status**: Die *Live-Ansicht* zeigt, wo jede Katze ist und seit wann. Tippe auf den Zustand, um ihn manuell zu ändern. Unter *Katzen* kannst du eine Katze dort ausblenden.
- **Bewegungssensor als Bestätigung bei Kamera-Einlass**: Optionale Einstellung unter *Einstellungen*. Wenn die Kamera Katzen erkennt, öffnet sich die Klappe nur, wenn auch der äußere Bewegungssensor auslöst. Hilfreich, wenn Sonne oder Spiegelungen manchmal eine Katze vortäuschen.
- **Hinweise ausblenden**: Manche Hinweise können mit *Nicht mehr benachrichtigen* ausgeblendet werden. Unter *Einstellungen* kannst du sie später wieder aktivieren.
- **Modell lokal trainieren**: Modelle lassen sich nun auch im eigenen Netzwerk trainieren, nicht nur auf dem öffentlichen Server. Der öffentliche Server begrenzt die Anzahl der Bilder und die Größe des Uploads. Auf einem eigenen Computer läuft das Training wahlweise über die CPU (langsam) oder mit einer Nvidia / AMD GPU (deutlich schneller).

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
- **Systeminformationen**: Unter *Info* siehst du neben der Datenbank auch die Größe des Bilderordners und des Modellordners. Balken zeigen, wie voll Speicher und Arbeitsspeicher sind.
- **Versionshinweise**: Lange Hinweise unter *Info* lassen sich mit *Mehr anzeigen* und *Weniger anzeigen* auf- und zuklappen.
- **Ereignisse ohne Chip**: Im Modus *Nur Chip* zeigt die Liste *Unbekannte Katze*, wenn kein Chip gelesen wurde. Eine erkannte Person erscheint als *Person*. Sonst bleibt es bei *Kein Mikrochip gefunden* (von [WeichEi04](https://github.com/floppyFK/kittyhack/pull/193))
- **Chip + Kamera**: Neben den Ereignissen schaltest du zwischen *Chip + Kamera* und *Nur Chip*. Ohne erkannten Chip kommt der Name aus den Bildern des Ereignisses, oder es zählt nur der Chip.

## Bugfixes

- **Durchschnittliche FPS**: Die Anzeige der durchschnittlichen FPS pro Modell in der Modellverwaltung wurde nicht mehr aktualisiert. Das ist jetzt behoben.
- **Remote-Update von einem eigenen Branch**: Ein Update der Kittyflap über ein Remote-Gerät verwendet jetzt das Update-Repository, das auf dem Remote-Gerät eingestellt ist.
- **Update-Fenster auf einem Remote-Gerät**: Während eines Updates verdeckt der Hinweis "Remote-Verbindung getrennt" nicht mehr das Update- und Neustart-Fenster.
- **Zu große Datei beim Modell-Training**: Ist die Datei zu groß, erscheint eine klare Meldung statt eines unklaren Fehlers.
- **Mehrere Markierungen in einem Bild**: Sieht die Kamera in einem Bild mehrere Katzen oder Beute, zählt die sicherste Erkennung. Vorher konnte eine schwache zweite Markierung eine starke erste verdecken (von [WeichEi04](https://github.com/floppyFK/kittyhack/pull/193))
- **Zuordnung per Kamera**: Die Kamera schaut sich die Bilder des laufenden Ereignisses an. Eine Katze braucht mehrere Bilder, und es zählen nur registrierte Katzen direkt an der Klappe. Ein einzelnes Fehlbild, eine Person oder Aufnahmen eines früheren Ereignisses legen den Namen nicht mehr fest (von [WeichEi04](https://github.com/floppyFK/kittyhack/pull/193))
- **Katze bleibt vor der Klappe**: Sitzt eine Katze länger vor der Klappe, wird sie nach dem Ende eines Erkennungsblocks weiter eingelassen, sobald ihr Chip gelesen wurde. Vorher blieb die Klappe dann zu (von [WeichEi04](https://github.com/floppyFK/kittyhack/pull/193))
