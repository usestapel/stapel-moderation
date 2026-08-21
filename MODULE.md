# stapel-moderation — MODULE.md

> Agent-facing map of this module: what it provides, where to extend it
> without forking, and what not to do. Kept in the same PR as any change
> to a seam. See also README.md and CHANGELOG.md.

## What this module provides

- TODO: 3-5 bullets — domain, models, API, comm surface.

## Extension points (fork-free)

### Settings — `STAPEL_MODERATION` namespace (`conf.py`)

Resolution order per key: `settings.STAPEL_MODERATION[key]` -> flat Django setting
of the same name -> environment variable -> default. Read lazily at call
time; caches invalidate on `setting_changed`.

| Key | Default | What it customizes |
|---|---|---|
| `GREETING` | `"pong"` | Scaffold example — replace. |

State for every registry-style key whether it MERGES over built-ins
(open registry) or REPLACES a single strategy (dotted path).

### Serializer seams (`views.py`)

`SerializerSeamMixin` — subclass a view, set
`request_serializer_class` / `response_serializer_class`, remount the URL.

| View | Request serializer | Response serializer |
|---|---|---|
| `PingView` | — | `PingResponseSerializer` |

### Events & functions (comm surface)

| Kind | Name | Payload | Schema |
|---|---|---|---|
| Function (provides) | `moderation.ping` | `{}` -> `{greeting}` | `schemas/functions/moderation.ping.json` |

## Anti-patterns

- **Don't fork to change behavior** — every knob above is a seam; if a
  change is impossible without editing this package, that is an upstream
  bug: open an issue/contribution instead.
- **Don't import other stapel modules** — cross-module communication is
  comm (Actions/Functions) by string name only.
- **Don't bypass the settings namespace** with `os.getenv` at import time.

## App-layer override vs upstream contribution — rule of thumb

**App-layer** (host project, no fork) if the change fits a seam above: a
settings key, a subclass + URL remount, a comm subscriber.

**Upstream contribution** if it needs new model fields/migrations, new
endpoints, a new settings key or seam, or changes a committed schema.

Litmus test: if you'd have to monkeypatch or edit code inside
`stapel_moderation/` — it's upstream. If a setting, subclass, receiver or comm
call gets you there — it's app-layer.
