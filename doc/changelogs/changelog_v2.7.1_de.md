# v2.7.1

## Bugfixes

- **Modell-Download nach dem Training**: Nach einem erfolgreichen Remote-Training wird das neue Modell wieder heruntergeladen. In v2.7.0 wurde der Download-Worker im falschen Verzeichnis gestartet, ist sofort mit `No module named 'src'` abgebrochen und die Oberfläche blieb bei der Fehlermeldung.
