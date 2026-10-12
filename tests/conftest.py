import pytest

from prozorro_mcp.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()
