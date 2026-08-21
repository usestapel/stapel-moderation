"""stapel-moderation — the fleet's single producer of moderation verdicts.

A target-generic queue over every kind of moderated thing (listings, reviews,
chat messages, profiles): one ``Case`` per target, one status vocabulary, an
append-only audit trail, LLM-first screening with a human always in the loop,
and sanctions with a reason, a scope, a clock and an appeal.

Public API (lazily exported, PEP 562 — importing this package never pulls in
Django or requires configured settings):

- ``moderation_settings`` — resolved app settings (``stapel_moderation.conf``);
- ``register_target_type`` / ``register_reason`` / ``register_rule`` — the
  three merge-registries (``stapel_moderation.registry``);
- ``NotSanctioned`` — the DRF permission a HOST hangs on its own write views
  to refuse a sanctioned user (``stapel_moderation.authz``).
"""

__all__ = [
    "moderation_settings",
    "register_reason",
    "register_rule",
    "register_target_type",
    "NotSanctioned",
]

# name -> submodule that defines it. Resolution is deferred until first
# attribute access so that `import stapel_moderation` stays Django-free.
_LAZY_EXPORTS = {
    "moderation_settings": ".conf",
    "register_target_type": ".registry",
    "register_reason": ".registry",
    "register_rule": ".registry",
    "NotSanctioned": ".authz",
}


def __getattr__(name):
    if name in _LAZY_EXPORTS:
        from importlib import import_module

        value = getattr(import_module(_LAZY_EXPORTS[name], __name__), name)
        globals()[name] = value  # cache for subsequent lookups
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(__all__))
