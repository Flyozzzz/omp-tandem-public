"""Forward state compatibility for guard-aware runtimes, before schema mutation."""

STATE_SCHEMA_VERSION = 2


class StateCompatibilityError(ValueError):
    """The database requires a different runtime; never migrate it speculatively."""


def guard_database(db) -> None:
    """Run under initialize_database's BEGIN IMMEDIATE, before any DDL or DML.

    The caller commits the version with its schema changes, or rolls everything
    back on failure. Version zero is the pre-guard legacy schema. This cannot
    constrain old binaries that do not implement this guard.
    """
    if not db.in_transaction:
        raise StateCompatibilityError(
            "State compatibility must be checked inside the initialization transaction"
        )
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, 1, STATE_SCHEMA_VERSION):
        raise StateCompatibilityError(
            f"State schema {version} is unsupported by this runtime (supports {STATE_SCHEMA_VERSION}). "
            "Stop without modifying this state. Keep a consistent backup before any operator migration; "
            "use a compatible installed runtime or --candidate with a new isolated state directory. "
            "Do not reset active work or open this state with older binaries that ignore the compatibility guard."
        )
    if version < STATE_SCHEMA_VERSION:
        db.execute(f"PRAGMA user_version={STATE_SCHEMA_VERSION}")


def state_compatibility() -> dict:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "legacy_schema_version": 0,
        "upgradeable_schema_versions": [0, 1],
        "enforcement": "guard-aware runtimes only; older binaries are not controlled",
        "on_incompatible": "stop; retain state and back up before explicit migration, or use new isolated candidate state",
    }
