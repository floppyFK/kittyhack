# v3.0.1

## New Features

- **Train models on a Mac**: Under *AI Training*, the installation instructions now include macOS, next to Linux and Windows. (by [WeichEi04](https://github.com/floppyFK/kittyhack/pull/193))

## Bugfixes

- **False entry after movement inside only**: If motion was detected only on the inside, without subsequent motion on the outside (for example when your cat sniffs at the flap), a *cat went inside* event was sent via MQTT. The flap could also stay closed for the next real entry afterwards - this is now fixed (by [WeichEi04](https://github.com/floppyFK/kittyhack/pull/193))
