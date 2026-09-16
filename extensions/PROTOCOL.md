# Writing an extension

An extension is a folder with a manifest and a binary:

```
my-extension/
  extension.json
  my-extension.exe
```

**extension.json**

```json
{
  "id": "weather",
  "name": "Weather",
  "version": "1.0.0",
  "description": "Looks up current weather for a location.",
  "entry": "my-extension.exe",
  "interpreter": null,
  "args": []
}
```

`id`, `name`, and `entry` are required. `entry` is a path relative to
the manifest. `interpreter` is only for development: set it to
`"python"` (or omit it and just name a `.py` file as `entry`) to run a
script instead of a compiled binary while you're building the thing.

**Getting it running**

- Drop the folder into `data/extensions/` and hit "Rescan" in the
  Configuration window's Extensions tab (or restart the app) -- this is
  "import."
- Or add it from anywhere else on disk via "Add extension..." in that
  same tab, which just records the path -- this is "point." Nothing
  gets copied.

Either way, the backend never needs to be rebuilt or repackaged to pick
up a new extension.

## The protocol

The backend starts your binary once, when the app starts (or when you
add/rescan it), and keeps it running -- it does **not** spawn a fresh
process per call. Talk to it over stdin/stdout with one JSON object per
line:

**Backend -> you:**
```
{"id": "<uuid>", "method": "<name>", "params": {...}}
```

**You -> backend:**
```
{"id": "<uuid>", "ok": true, "result": {...}}
```
or
```
{"id": "<uuid>", "ok": false, "error": "what went wrong"}
```

Always echo back the same `"id"` you were given -- that's how the
backend matches your response to the right pending call.

### The handshake

The very first request you'll get, right after you start up, is:
```
{"id": "...", "method": "handshake", "params": {}}
```
Answer it within 5 seconds with at least a name and version:
```
{"id": "...", "ok": true, "result": {"name": "Weather", "version": "1.0.0"}}
```
This is what shows up in the Extensions tab. Anything else you put in
`result` is fine too -- future callers (e.g. a tool-calling layer) can
use it to learn what you're capable of, but nothing in the backend
requires a particular shape here yet.

### Rules

- **stdout is the protocol channel. Never print anything else to it** --
  no debug logging, no library that logs to stdout by default. One
  stray non-JSON line and the backend just logs a warning and ignores
  it, but a library that spams stdout will drown out real responses.
- **stderr is yours** -- anything you write there gets forwarded into
  the backend's system log automatically, so it's fine (encouraged,
  even) for your own debug output.
- **Keep running.** Read a line, handle it, write a response, go back
  to reading. If you exit, the backend marks you "crashed" in the
  Extensions tab; it won't restart you automatically, but a human can
  hit "Restart" there.
- **Respond to everything**, even if just with an error -- an unanswered
  request eventually times out on the backend's side (15s default) and
  that whole call fails, but a request that's silently dropped is
  harder to debug than one that came back with a clear error.
