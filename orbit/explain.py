"""
On-demand query EXPLAIN.

Runs a query plan for a captured SQL statement, on demand only (never on the
recording path).  Vendor-aware (PostgreSQL / MySQL / SQLite) with graceful
fallback.

Plain ``EXPLAIN`` does not execute the statement; ``EXPLAIN ANALYZE`` does, so
it is gated behind config and only ever allowed for read-only ``SELECT``
statements, wrapped in a rolled-back savepoint.

Parameter replay
----------------
When Orbit recorded the query, any database-adapter objects (e.g. psycopg
``Jsonb``) were unwrapped by ``orbit.adapters.unwrap_adapter`` and stored as
plain marker dicts.  At replay time, ``rebind_params`` reconstructs the correct
driver-level object for the current backend so the planner receives properly
typed values.

For legacy entries recorded before this mechanism existed, stored parameters may
be bare ``dict``/``list`` values or stringified reprs such as ``"Jsonb({...})"``.
These cannot be safely rebound; ``has_unbindable_param`` detects them and the
function returns a clear explanatory error rather than a cryptic database
exception.
"""

from typing import Any, Dict, Optional

from django.db import connections, transaction

from orbit.adapters import has_unbindable_param, rebind_params


def _is_select(sql: str) -> bool:
    return (
        sql.lstrip().lower().startswith(("select", "with", "explain", "show", "pragma"))
    )


def _is_dml(sql: str) -> bool:
    """True for INSERT/UPDATE/DELETE — statements plain EXPLAIN plans but does not execute."""
    return sql.lstrip().lower().startswith(("insert", "update", "delete"))


def _build_explain_sql(vendor: str, sql: str, analyze: bool) -> Optional[str]:
    sql = sql.rstrip().rstrip(";")
    if vendor == "postgresql":
        opts = "ANALYZE, COSTS, FORMAT TEXT" if analyze else "COSTS, FORMAT TEXT"
        return "EXPLAIN ({}) {}".format(opts, sql)
    if vendor == "mysql":
        return "EXPLAIN ANALYZE {}".format(sql) if analyze else "EXPLAIN {}".format(sql)
    if vendor == "sqlite":
        # SQLite has no ANALYZE-style timing; the query plan is the useful artifact.
        return "EXPLAIN QUERY PLAN {}".format(sql)
    return None


def explain_query(
    sql: str,
    params: Any = None,
    analyze: bool = False,
    using: str = "default",
) -> Dict[str, Any]:
    """
    Return a dict with keys: ``supported``, ``vendor``, ``analyze``, and either
    ``plan`` (list of strings) or ``error`` (string).

    Never raises: any failure is reported in ``error`` so callers and the UI can
    degrade gracefully.
    """
    result: Dict[str, Any] = {"supported": True,
                              "analyze": False, "vendor": None}

    if not sql or not sql.strip():
        return {"supported": False, "error": "No SQL to explain"}

    conn = connections[using]
    vendor = conn.vendor
    result["vendor"] = vendor

    # ANALYZE executes the statement — restrict to read-only SELECTs.
    if analyze and not _is_select(sql):
        analyze = False
    result["analyze"] = analyze

    # Rebuild driver-appropriate objects for any stored adapter markers.
    # This must run before has_unbindable_param so that successfully rebound
    # values are not falsely flagged as unbindable.
    params = rebind_params(params, vendor)

    # Plain EXPLAIN on DML still plans (and therefore binds) the statement.
    # Detect legacy params that cannot be rebound and return a clear message
    # instead of letting the database raise a cryptic type error.
    if _is_dml(sql) and not analyze and has_unbindable_param(params):
        return {
            "supported": True,
            "vendor": vendor,
            "analyze": False,
            "error": (
                "Can't EXPLAIN this statement: it has a JSON-typed parameter that was "
                "serialised for storage and can no longer be faithfully rebound. This "
                "is a limitation of replaying captured parameters, not a database or "
                "EXPLAIN support issue. Entries recorded after upgrading Orbit's "
                "recorder should not hit this — only older captured entries can."
            ),
        }

    explain_sql = _build_explain_sql(vendor, sql, analyze)
    if explain_sql is None:
        return {
            "supported": False,
            "vendor": vendor,
            "error": "EXPLAIN not supported for this database",
        }

    def _run(bind_params):
        with conn.cursor() as cursor:
            cursor.execute(explain_sql, bind_params)
            rows = cursor.fetchall()
        return ["  ".join(str(c) for c in row) for row in rows]

    bound = params if isinstance(params, (list, tuple)) else None

    try:
        if analyze:
            # Execute inside a savepoint that is always rolled back.
            with transaction.atomic(using=using):
                sid = transaction.savepoint(using=using)
                try:
                    plan = _run(bound)
                finally:
                    transaction.savepoint_rollback(sid, using=using)
        elif _is_dml(sql):
            # Bindable (confirmed above); params are required for DML planning.
            plan = _run(bound)
        else:
            # For SELECT, fall back to no params if binding fails — the plan
            # rarely depends on literal values, and a parameterless EXPLAIN is
            # far more useful than an outright error.
            try:
                plan = _run(bound)
            except Exception:
                plan = _run(None)

        result["plan"] = plan
        return result

    except Exception as e:  # pragma: no cover — defensive
        return {
            "supported": True,
            "vendor": vendor,
            "analyze": analyze,
            "error": str(e),
        }
