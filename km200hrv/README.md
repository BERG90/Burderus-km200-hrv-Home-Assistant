# KM200 HRV Bridge

Bindet eine Buderus Logavent HRV2 über das Gateway Logamatic web KM200 HRV per MQTT in Home Assistant ein.

*Inoffizielles Community-Projekt, nicht von Buderus oder Bosch.*

## Konfiguration

- `host`: IP-Adresse des KM200 HRV (am besten feste IP im Router vergeben)
- `gateway_password`: Gerätepasswort vom Aufkleber am KM200 (Format xxxx-xxxx-xxxx-xxxx, Bindestriche egal)
- `private_password`: Dein persönliches Passwort, das du in der MyVent-App vergeben hast
- `aes_key`: optional, nur falls du schon einen fertigen 64-stelligen Hex-Schlüssel hast (ersetzt die beiden Passwörter)
- `roots`: Bereiche, die ausgelesen werden (Standard `/ventilation`; `/system` kann ergänzt werden)
- `scan_interval`: Abfrageintervall in Sekunden (15–3600)
- `log_level`: `debug`, `info` oder `warning`

## Was passiert

Beim Start werden alle Datenpunkte unter `roots` gesucht und im Log aufgelistet.
Daraus entstehen automatisch Entitäten am Gerät „Logavent HRV2“:

- beschreibbare Auswahlwerte → `select`
- beschreibbare Zahlen → `number`
- alles andere → `sensor`

Schreiben: MQTT-Topic `km200hrv/set/<id>`, Status: `km200hrv/state/<id>`.
