# v2.5.4

Dieses Release ergänzt Kittyhack um die Möglichkeit, Update-Prüfung und Updates auf einen eigenen Fork oder einen Feature-Branch umzulenken — praktisch, um Pull-Requests direkt auf einer Kittyflap zu testen, ohne auf ein offizielles Release warten zu müssen.

## Neue Features
- **Konfigurierbares Update-Repository**: Unter *Konfiguration → Allgemeine Einstellungen* gibt es die neue Option „Update-Repository". Zur Auswahl stehen „Standard" (nutzt wie bisher die Releases von `floppyFK/kittyhack`) und „Benutzerdefiniert". Im benutzerdefinierten Modus akzeptiert das Eingabefeld entweder `owner/repo` (verwendet den neuesten Release-Tag des angegebenen Forks) oder `owner/repo@branch-oder-tag` (verfolgt den HEAD des angegebenen Refs). Bei einem Branch zeigt die Versionsanzeige `<branch>@<short-sha>`, damit die Update-Benachrichtigung weiterhin wie gewohnt funktioniert.
