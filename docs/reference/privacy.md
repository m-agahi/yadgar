# Privacy Policy — yadgar Update Check

**Version:** v5.48.0. **Last updated:** 2026-06-07.

---

## Summary

The optional update-check feature makes **one anonymous HTTP request** to the PyPI JSON API per daemon start. No other data leaves your machine.

---

## Exact wire format

```
GET https://pypi.org/pypi/yadgar/json HTTP/1.1
Host: pypi.org
User-Agent: yadgar/<version>
Accept: application/json
```

No request body. No cookies. No query parameters. No other headers.

Example with `yadgar/5.48.0`:

```
GET https://pypi.org/pypi/yadgar/json HTTP/1.1
Host: pypi.org
User-Agent: yadgar/5.48.0
Accept: application/json
```

The `User-Agent` header contains the yadgar version number only. This is publicly visible information (also printed by `yadgar --version`). It is used to let operators identify traffic sources in PyPI server logs; it does not identify the user.

---

## What is sent

| Field | Value | Notes |
|---|---|---|
| HTTP method | GET | Read-only |
| URL | `https://pypi.org/pypi/yadgar/json` | Public PyPI JSON API |
| `User-Agent` | `yadgar/<version>` | Yadgar version only |
| `Accept` | `application/json` | Standard content negotiation |
| Request body | (none) | Nothing sent |
| Cookies | (none) | httpx discards `Set-Cookie` responses |
| Other headers | (none) | Strictly UA + Accept |

---

## What is NOT sent

- User identity or account information
- System hostname, username, or OS details
- Project paths, memory content, or conversation data
- IP address (the server sees the outbound IP as with any HTTP request, but yadgar does not transmit it as a data field)
- Usage statistics or telemetry of any kind

---

## What is received

The response contains the PyPI package metadata JSON. Yadgar extracts only:

```
response.json()["info"]["version"]   →  "5.48.0" (string)
```

All other response fields are ignored.

---

## Default: OFF

`update_check_on_start` defaults to `false`. The probe is never triggered unless the user explicitly sets:

```yaml
# ~/.config/yadgar/config.yaml
update_check_on_start: true
```

---

## Opt-out

Set `update_check_on_start: false` (or omit the field — false is the default) in `~/.config/yadgar/config.yaml`.

The `yadgar update --check` CLI command makes the same probe on demand and is also opt-in (user must run it manually).

---

## Corporate firewalls

The probe respects the `HTTPS_PROXY` environment variable (httpx default behavior):

```bash
export HTTPS_PROXY=http://proxy.corp:3128
yadgar daemon start
```

In air-gapped environments, keep `update_check_on_start: false` (the default).

---

## PyPI server-side logging

PyPI is operated by the Python Software Foundation. Like any web server, PyPI logs standard HTTP request metadata (timestamp, IP, path, UA string). Yadgar has no control over PyPI's logging policy. PyPI's privacy policy: https://www.python.org/privacy/

---

## Changes to this document

This document is updated whenever the probe format changes. Format changes will be accompanied by a CHANGELOG and MIGRATION_NOTES entry.
