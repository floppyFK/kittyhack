# v3.0.2

## Bugfixes

- **Kamera nach einem Netzwerkausfall**: Im Remote-Modus konnte die Kittyflap nach einem Verbindungsabbruch nicht mehr auf Port 80 erreichbar sein. Der Remote-Rechner bekam dann keinen Kamerastream und hat die Klappe nach innen nicht entriegelt, bis `kittyhack_control` neu gestartet wurde. Die Infoseite beendet hängende Video-Verbindungen jetzt und lauscht von selbst wieder.
- **Seite nach Neustart oder Update**: Ein offener Browser-Tab hat sich neu verbunden und den Inhalt der alte Seite behalten. Der Tab *Info* konnte so noch die vorherige Version anzeigen. Die Seite lädt jetzt einmal neu, wenn die Kittyflap neu gestartet wurde.
- **Einstellungen beim ersten Remote-Sync**: Beim ersten Abgleich kamen Ereignisse und Bilder an, die Einstellungen blieben aber auf den Standardwerten. Die Datenbank wird jetzt vollständig übertragen, und eine übrig gebliebene `config.ini` überschreibt sie nicht mehr.
- **Erkennungen auf schwarzen Bildern**: Wenn das Licht ausgeht, liefert die Kamera für einen Moment fast schwarze Bilder, während sie auf Nachtsicht umschaltet. Das Modell konnte darauf eine Katze melden. Daraus wurde ein Ereignis *Katze an der Klappe* mit leeren Bildern, und die Entscheidung für den Eingang konnte davon beeinflusst werden. Solche Bilder werden jetzt ignoriert.
