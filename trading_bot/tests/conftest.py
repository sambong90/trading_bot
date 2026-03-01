"""
pytest configuration: sys.path setup and DB stub.

trading_bot.db creates a SQLAlchemy engine at import time. Without stubbing it,
the import triggers a filesystem SQLite connection attempt which may fail in CI.
This conftest stubs the engine before any test module imports trading_bot code.
"""
import sys
import pathlib
import types

# Ensure workspace root is on the path regardless of how pytest is invoked.
ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── Stub trading_bot.db before any import triggers engine creation ────────────
# We insert a minimal fake module so `from trading_bot.db import get_session`
# works in unit tests without a real database connection.
_fake_db = types.ModuleType('trading_bot.db')


class _FakeSession:
    """Minimal SQLAlchemy session stub. Override per-test with unittest.mock.patch."""
    def query(self, *a, **kw):
        return self
    def filter(self, *a, **kw):
        return self
    def order_by(self, *a, **kw):
        return self
    def limit(self, *a, **kw):
        return self
    def first(self):
        return None
    def all(self):
        return []
    def count(self):
        return 0
    def add(self, obj):
        pass
    def commit(self):
        pass
    def rollback(self):
        pass
    def close(self):
        pass


def _fake_get_session():
    return _FakeSession()


_fake_db.get_session = _fake_get_session
_fake_db.engine = None

sys.modules.setdefault('trading_bot.db', _fake_db)
