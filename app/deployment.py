"""Fail closed before changing schemas shared by live blue/green processes."""
import hashlib
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory

from .db import engine


# An online migration requires a reviewed exact file digest. Only additive,
# backward-compatible changes may be listed; destructive changes need a separate
# maintenance deployment. Never downgrade a shared production database.
ONLINE_MIGRATIONS: dict[str, str] = {}


def schema_plan(scripts, current, approved=ONLINE_MIGRATIONS):
    heads = scripts.get_heads()
    if len(heads) != 1 or len(current) != 1:
        raise RuntimeError("Expected one initialized database and migration head")
    target, previous = heads[0], current[0]
    revisions = list(scripts.iterate_revisions(target, previous))
    for revision in revisions:
        digest = hashlib.sha256(Path(revision.path).read_bytes()).hexdigest()
        if approved.get(revision.revision) != digest:
            raise RuntimeError("Schema change needs an explicit online compatibility review")
    return previous, target, bool(revisions)


def main():
    try:
        scripts = ScriptDirectory.from_config(Config("alembic.ini"))
        with engine.connect() as connection:
            current = MigrationContext.configure(connection).get_current_heads()
        previous, target, upgrade = schema_plan(scripts, current)
    except Exception:
        raise SystemExit("Schema preflight failed; keep the current release. Review migration compatibility before deployment.")
    print(f"{previous} {target} {'upgrade' if upgrade else 'current'}")


if __name__ == "__main__":
    main()
