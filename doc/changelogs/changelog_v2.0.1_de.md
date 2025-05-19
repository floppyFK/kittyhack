# v2.0.1

## Bugfixes
- **Loggen von Events mit RFID**: Events werden jetzt immer gespeichert, wenn eine RFID ausgelesen werden konnte - selbst wenn die `Minimale Erkennungsschwelle` für die Objekterkennung nicht erreicht wurde.
- **RFID-Reader**: Falsche RFIDs wie `E54` werden jetzt nicht mehr gelesen (Das Problem sollte bereits in v2.0.0 behoben sein, es hat sich aber noch ein weiterer Fehler eingeschlichen)
- **Fehlender Button**: Der fehlende `Changelogs`-Button im Info-Tab ist jetzt wieder verfügbar