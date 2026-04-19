# v2.5.4

Dieses Release ergänzt Kittyhack um die Möglichkeit, Update-Prüfung und Updates auf einen eigenen Fork oder einen Feature-Branch umzulenken — praktisch, um Pull-Requests direkt auf einer Kittyflap zu testen, ohne auf ein offizielles Release warten zu müssen.

## Neue Features
- **Konfigurierbares Update-Repository**: Unter *Konfiguration → Allgemeine Einstellungen* gibt es die neue Option „Update-Repository". Zur Auswahl stehen „Standard" (nutzt wie bisher die Releases von `floppyFK/kittyhack`) und „Benutzerdefiniert". Im benutzerdefinierten Modus akzeptiert das Eingabefeld entweder `owner/repo` (verwendet den neuesten Release-Tag des angegebenen Forks) oder `owner/repo@branch-oder-tag` (verfolgt den HEAD des angegebenen Refs). Bei einem Branch zeigt die Versionsanzeige `<branch>@<short-sha>`, damit die Update-Benachrichtigung weiterhin wie gewohnt funktioniert.
- **Automatische Update-Nachfrage nach Wechsel der Update-Quelle**: Wenn das Update-Repository (Modus oder Wert) geändert und gespeichert wird, fragt Kittyhack jetzt direkt, ob das Update sofort ausgeführt werden soll. Die neue Quelle hat meistens eine andere Code-Basis — das spart einen zusätzlichen manuellen Schritt.
- **„Force Update"-Schaltfläche**: Im Tab *Info* gibt es einen neuen, stets sichtbaren Button „Force update now". Er führt das Update gegen die konfigurierte Quelle erneut aus — nützlich, um den aktuellen Branch-HEAD (`git checkout -B <ref> origin/<ref>`) zu aktualisieren oder den gewählten Tag erneut zu installieren, ohne auf eine neue Versionsnummer warten zu müssen.
