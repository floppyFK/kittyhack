# v2.5.5

Hotfix-Release für zwei Fehler, die nach einem Neustart bzw. bei PIR-basierter Bewegungserkennung auftreten konnten.

## Bugfixes
- **Eingangsrichtung nach Neustart vorübergehend blockiert**: Nach einem Neustart war die Eingangsrichtung für die Dauer von der Einstellung *Verriegelungsdauer nach Beuteerkennung* blockiert, obwohl in der aktuellen Laufzeit noch keine Beute erkannt wurde.
- **Hohe CPU-Last bei PIR-basierter Bewegungserkennung**: Bei deaktivierter Option *Kamera für die Bewegungserkennung verwenden* (also PIR-Betrieb) konnte es zu hoher CPU-Last und fortlaufenden Log-Einträgen kommen.