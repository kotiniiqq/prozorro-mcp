import pytest

from prozorro_mcp.store import Store

# Data "as of" this moment: factory tenders are created 2026-10-01 with deadlines after it.
AS_OF = "2026-10-01T00:00:00+03:00"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    s.set_state("last_sync", AS_OF)
    yield s
    s.close()
