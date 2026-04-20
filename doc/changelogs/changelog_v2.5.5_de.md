# v2.5.5

Hotfix für eine Aussperrung, die in den ersten ~30 Minuten nach jedem Neustart auftreten konnte (inkl. dem Neustart, der durch das v2.5.4-Update selbst ausgelöst wurde).

## Bugfixes
- **Katze konnte ~30 Minuten nach einem Neustart nicht hereinkommen**: Das Gate „keine Beute innerhalb der Timeout-Zeit erkannt" in der Innen-Entriegelungslogik verwendete einen naiven Zeitvergleich, der den nicht initialisierten Wert `prey_detection_mono` (`0.0`) so behandelte, als sei „gerade eben Beute erkannt worden". Weil die monotone Uhr direkt nach dem Boot klein ist, war das errechnete Alter kleiner als `LOCK_DURATION_AFTER_PREY_DETECTION` (Standard: 1800 s), und das Gate blieb für die ersten 30 Minuten der Uptime geschlossen — obwohl in dieser Session nie Beute erkannt wurde. Nach einem Neustart konnten RFID-Erkennung, Bewegungserkennung und Mouse-Check alle erfolgreich sein, während die Tür sich trotzdem nicht entriegelte. Der Fix bricht den Vergleich ab, wenn `prey_detection_mono` nie gesetzt wurde, analog zur Schutzprüfung, die bereits im Prey-State-Publisher vorhanden war.
