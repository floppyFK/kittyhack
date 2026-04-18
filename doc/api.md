# Kittyhack REST API

Kittyhack ships a small JSON API under `/api/v1/*` that lets you control the
flap remotely from scripts, Home Assistant, Stream Deck, iOS Shortcuts or any
other HTTP client.

## Authentication

All endpoints require a token. Pick whichever method matches your client:

| Method | Example | When to use |
|--------|---------|-------------|
| `Authorization: Bearer <token>` header | `curl -H 'Authorization: Bearer khk_…' …` | Scripts, Home Assistant, anything programmable |
| `X-API-Key: <token>` header | `curl -H 'X-API-Key: khk_…' …` | Tools that dislike the Bearer scheme |
| `?token=<token>` query parameter | `http://kittyhack/api/v1/door/open?token=khk_…` | Fire-and-forget URL clients (Stream Deck, bookmarks, iOS Shortcuts) |

> **Query-parameter warning:** tokens in URLs may be written to web-server
> access logs or browser history. If you use this method, create a dedicated
> token per device and revoke it the moment it's no longer needed.

### Managing tokens

Tokens are stored hashed (SHA-256) in `api_tokens.json` next to `config.ini`.
The clear-text value is only shown once — at creation.

**Primary path — the web UI:**

1. Open the Kittyhack web interface and go to the **System** tab
2. Scroll to the **API Tokens** card
3. Enter a label (e.g. `stream-deck`, `home-assistant`) → click **Create Token**
4. Copy the token from the dialog — it is only shown once
5. To revoke, pick the token from the dropdown → **Revoke Selected**

**Alternative — CLI** (useful for bootstrap / recovery over SSH):

```bash
# From the kittyhack project root
python tools/api_token.py create stream-deck
python tools/api_token.py list
python tools/api_token.py revoke <id>
```

Brute-force protection: 10 failed auth attempts per source IP within 60 s
return `429 Too Many Requests`.

## Endpoints

All responses are JSON. Success responses always include `"ok": true`; errors
include `"ok": false` plus an `"error"` string.

### Status

```
GET /api/v1/status
```

Returns current door state and active mode.

```json
{
  "ok": true,
  "door": { "inside_unlocked": false, "outside_unlocked": false, "available": true },
  "mode": { "entry": "known", "exit": "allow" }
}
```

### Door control

Every door endpoint accepts both `GET` and `POST`.

| Endpoint | Action |
|----------|--------|
| `/api/v1/door/open` | Unlock the inside flap (let a cat in). Alias of `unlock_inside`. |
| `/api/v1/door/close` | Lock the inside flap. Alias of `lock_inside`. |
| `/api/v1/door/unlock_inside` | Unlock inside magnet explicitly |
| `/api/v1/door/lock_inside` | Lock inside magnet explicitly |
| `/api/v1/door/unlock_outside` | Unlock outside magnet |
| `/api/v1/door/lock_outside` | Lock outside magnet |

The action is queued via the same `manual_door_override` mechanism used by
the UI and MQTT integration — so all safety rules (max-unlock timeouts,
prey-detection locking, etc.) continue to apply.

### Mode

Read:

```
GET /api/v1/mode
```

Set explicitly:

```
PUT /api/v1/mode          Content-Type: application/json
{"entry": "known", "exit": "allow"}
```

or via query parameters (handy for URL-only clients):

```
GET /api/v1/mode?entry=known&exit=allow
```

Valid `entry` values: `all`, `all_rfids`, `known`, `none`, `configure_per_cat`.
Valid `exit` values: `allow`, `deny`, `configure_per_cat`.

**Per-direction URLs** — change only one direction, leave the other untouched:

```
GET /api/v1/mode/entry/all       # let any cat enter
GET /api/v1/mode/entry/known     # only cats with a known RFID may enter
GET /api/v1/mode/entry/none      # block all entries
GET /api/v1/mode/entry/all_rfids # any cat with any registered RFID
GET /api/v1/mode/entry/configure_per_cat

GET /api/v1/mode/exit/allow      # any cat may leave
GET /api/v1/mode/exit/deny       # block all exits
GET /api/v1/mode/exit/configure_per_cat
```

**Combined presets** — change both directions at once:

| Endpoint | `entry` | `exit` |
|----------|---------|--------|
| `GET /api/v1/mode/open` | `all` | `allow` |
| `GET /api/v1/mode/normal` | `known` | `allow` |
| `GET /api/v1/mode/closed` | `none` | `deny` |

### Cats

List configured cats:

```
GET /api/v1/cats
```

Update a single cat (by RFID tag or case-insensitive name):

```
PUT /api/v1/cats/<rfid_or_name>        Content-Type: application/json
{"allow_entry": true, "allow_exit": false, "enable_prey_detection": true}
```

Only `allow_entry`, `allow_exit`, `enable_prey_detection` can be changed via
the API. All fields are optional — send only what you want to update.

### Events

Recent motion/detection events, newest first:

```
GET /api/v1/events?limit=50
```

`limit` defaults to 50 and is clamped to `[1, 500]`.

## Example: Stream Deck setup

1. In Kittyhack's **System** tab, scroll to **API Tokens**, enter a label
   like `stream-deck` and click **Create Token**. Copy the shown value.
2. In Stream Deck, add a **Website** action with URL:
   ```
   http://<kittyhack-host>/api/v1/door/open?token=<paste-token-here>
   ```
3. Add more buttons for `/api/v1/mode/closed`, `/api/v1/mode/normal`,
   `/api/v1/mode/open`, `/api/v1/mode/entry/none`, `/api/v1/mode/exit/deny`
   etc. as needed.
