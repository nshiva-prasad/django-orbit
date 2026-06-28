"""
orbit/adapters.py — Database adapter serialisation/replay for Orbit.

Background
----------
Django's ORM adapts Python values into database-driver objects before they reach
Orbit's query recorder.  A ``JSONField`` value, by the time the recorder's
``execute`` wrapper fires, is no longer a plain ``dict`` — it is a driver-level
wrapper such as psycopg3's ``Jsonb`` or psycopg2's ``Json``.  If Orbit stores a
``str(Jsonb({...}))`` repr it can never replay the value faithfully; if it stores
the raw ``dict`` the driver later raises "cannot adapt type 'dict'" during EXPLAIN.

The solution implemented here has three stages:

1. **Detect** — identify live adapter objects before they are serialised.
2. **Unwrap** — extract the underlying Python value and store it with a generic
   marker that names the adapter *kind* (e.g. ``"json"``).
3. **Rebind** — at EXPLAIN replay time, reconstruct a driver-appropriate bindable
   object for the target vendor from the stored marker.

Why a named-kind marker rather than a boolean ``__orbit_json_value__``?
-----------------------------------------------------------------------
A boolean flag bakes in one adapter type for ever.  A string kind tag
(``"__orbit_adapter__": "json"``) is open: adding ``"array"`` or ``"range"``
support in future means writing one small detection/unwrap/rebind triplet and
registering it — no changes to the shared dispatch logic or call sites.

Backend-specific rebind notes
------------------------------
* **PostgreSQL** — wraps the value in ``psycopg.types.json.Jsonb`` (psycopg3) or
  ``psycopg2.extras.Json``.  Falls back to a plain JSON string which Postgres
  accepts for ``json``/``jsonb`` columns via an implicit cast.
* **MySQL / SQLite** — both bind JSON columns from plain text.  Django's own
  ``JSONField.get_prep_value()`` returns ``json.dumps(value, cls=self.encoder)``
  (verified against Django source; neither backend overrides ``adapt_json_value``
  with a wrapper object).  Plain ``json.dumps`` is therefore correct and
  sufficient for both.

Graceful degradation
--------------------
Every public function is safe to call with any value — including values from
older Orbit entries, unknown adapter types, or environments where psycopg is not
installed.  Failures are absorbed and callers receive a best-effort result or
``None``/unchanged value, never an exception.
"""

from __future__ import annotations

import json as _json
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Public marker constant — single source of truth shared by recorders and
# explain modules.  Import this; never hard-code the string elsewhere.
# ---------------------------------------------------------------------------

ADAPTER_MARKER_KEY = "__orbit_adapter__"


# ---------------------------------------------------------------------------
# Internal per-kind implementations
# ---------------------------------------------------------------------------

# --- json -------------------------------------------------------------------


def _detect_json(value: Any) -> bool:
    """Return True if *value* is a recognised psycopg JSON/JSONB adapter."""
    module = getattr(type(value), "__module__", "") or ""
    if not module.startswith("psycopg"):
        return False
    return type(value).__name__ in ("Json", "Jsonb")


def _unwrap_json(value: Any) -> Optional[Dict[str, Any]]:
    """
    Extract the inner Python object from a psycopg Json/Jsonb wrapper.

    psycopg does not document a stable public attribute for the wrapped object,
    so we probe a short list of plausible names rather than hard-code one.  If
    none resolves, return ``None`` so the caller can fall back to a safe repr.
    """
    for attr in ("obj", "adapted", "_obj", "wrapped"):
        try:
            inner = getattr(value, attr, _MISSING)
        except Exception:
            continue
        if inner is _MISSING:
            continue
        # Only tag values that are themselves JSON-storable.  Anything else falls
        # through to the generic str() path in the recorder's serialiser.
        if inner is None or isinstance(inner, (dict, list, str, int, float, bool)):
            return {ADAPTER_MARKER_KEY: "json", "value": inner}
    return None


def _rebind_json(marker: Dict[str, Any], vendor: str) -> Any:
    """Reconstruct a bindable value from a ``"json"`` marker for *vendor*."""
    inner = marker.get("value")

    if vendor == "postgresql":
        # Prefer psycopg3, fall back to psycopg2, then plain JSON text.
        try:
            from psycopg.types.json import Jsonb  # type: ignore[import]

            return Jsonb(inner)
        except Exception:
            pass
        try:
            from psycopg2.extras import Json  # type: ignore[import]

            return Json(inner)
        except Exception:
            pass

    # MySQL, SQLite, and the Postgres fallback all accept plain JSON text.
    # Django's JSONField.get_prep_value() uses json.dumps() for these backends.
    return _json.dumps(inner)


# ---------------------------------------------------------------------------
# Kind registry — maps kind tag → (detect, unwrap, rebind)
# ---------------------------------------------------------------------------
# To add a new adapter type (e.g. "array", "range"):
#   1. Write _detect_<kind>, _unwrap_<kind>, _rebind_<kind> functions above.
#   2. Add one entry here.  Nothing else needs to change.

_MISSING = object()  # sentinel for attribute probing

_REGISTRY: Dict[str, tuple] = {
    "json": (_detect_json, _unwrap_json, _rebind_json),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_supported_adapter(value: Any) -> Optional[str]:
    """
    Return the adapter *kind* string (e.g. ``"json"``) if *value* is a
    recognised database adapter object, otherwise return ``None``.

    This is the single place where adapter detection logic lives.  Callers
    should use this function rather than inspecting types directly.
    """
    for kind, (detect, _unwrap, _rebind) in _REGISTRY.items():
        try:
            if detect(value):
                return kind
        except Exception:
            pass
    return None


def unwrap_adapter(value: Any) -> Any:
    """
    If *value* is a recognised database adapter object, return a
    JSON-serialisable marker dict that encodes the adapter kind and the
    underlying Python value::

        {"__orbit_adapter__": "json", "value": {...}}

    If *value* is not a recognised adapter, or if unwrapping fails for any
    reason, return *value* unchanged so the recorder's generic serialiser can
    handle it (falling back to ``str()`` if necessary).
    """
    kind = is_supported_adapter(value)
    if kind is None:
        return value

    _detect, unwrap_fn, _rebind = _REGISTRY[kind]
    try:
        result = unwrap_fn(value)
    except Exception:
        result = None

    # If the per-kind unwrap couldn't produce a marker, return the original so
    # the caller's fallback (str repr) is used instead of silently losing data.
    return result if result is not None else value


def is_adapter_marker(value: Any) -> bool:
    """Return True if *value* is an Orbit adapter marker dict."""
    return isinstance(value, dict) and isinstance(value.get(ADAPTER_MARKER_KEY), str)


def rebind_adapter(marker: Dict[str, Any], vendor: str) -> Any:
    """
    Given an Orbit adapter *marker* dict and a Django database *vendor* string
    (``"postgresql"``, ``"mysql"``, ``"sqlite"``), return a value that the
    corresponding driver can bind in a fresh ``cursor.execute()`` call.

    If the marker kind is unknown or rebinding fails, returns the raw ``value``
    field from the marker (a plain Python object) so the caller can decide how
    to handle it.
    """
    kind = marker.get(ADAPTER_MARKER_KEY)
    if kind not in _REGISTRY:
        # Future-proof: unknown kind, return the wrapped value as-is.
        return marker.get("value")

    _detect, _unwrap, rebind_fn = _REGISTRY[kind]
    try:
        return rebind_fn(marker, vendor)
    except Exception:
        return marker.get("value")


def rebind_params(params: Any, vendor: str) -> Any:
    """
    Walk *params* (a list or tuple) and rebind any Orbit adapter markers using
    ``rebind_adapter``.  Non-marker values pass through unchanged.

    Returns a new list; never mutates *params*.  Safe to call with ``None``.
    """
    if not isinstance(params, (list, tuple)):
        return params
    return [rebind_adapter(p, vendor) if is_adapter_marker(p) else p for p in params]


def has_unbindable_param(params: Any) -> bool:
    """
    Return True if *params* contains a value that cannot be faithfully rebound
    into a fresh ``cursor.execute()`` call, even after marker-tagged values have
    been rebound by ``rebind_params``.

    Unbindable shapes are legacy entries recorded before the adapter-unwrapping
    fix was in place:

    * A bare ``dict`` or ``list`` that is *not* an Orbit adapter marker — the
      driver would raise "cannot adapt type 'dict'" when binding.
    * A ``str`` that is a stringified adapter repr such as ``"Jsonb({...})"`` —
      binding this raises "invalid input syntax for type json" against a JSON
      column.

    This check exists solely to produce a clear user-facing error instead of a
    cryptic database exception.
    """
    if not isinstance(params, (list, tuple)):
        return False
    for p in params:
        if is_adapter_marker(p):
            continue  # handled by rebind_params
        if isinstance(p, (dict, list)):
            return True
        if isinstance(p, str) and p.startswith(("Jsonb(", "Json(")):
            return True
    return False
