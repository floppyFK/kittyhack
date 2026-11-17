# v3.0.1

## Neue Features

- **Modell-Training auf dem Mac**: Unter *KI Training* gibt es die Installationsanleitung jetzt auch für macOS, neben Linux und Windows. (von [WeichEi04](https://github.com/floppyFK/kittyhack/pull/193))

## Bugfixes

- **Falscher Eingang bei Bewegung nur innen**: Wenn nur auf der Innenseite Bewegung erkannt wurde, ohne darauffolgende Bewegung auf der Außenseite (zum Beispiel wenn deine Katze an der Klappe schnuppert), wurde ein Ereignis *Katze ging hinein* via MQTT ausgesendet. Die Klappe konnte danach auch für den nächsten echten Eingang geschlossen bleiben - dies ist nun gefixt (von [WeichEi04](https://github.com/floppyFK/kittyhack/pull/193))
