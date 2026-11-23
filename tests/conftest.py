import pytest


@pytest.fixture(autouse=True)
def fresh_code_budget():
    """The code-guessing budget is process-wide, so one test's misses must not count against
    the next test's accounts."""
    from classroom_sync.ratelimit import code_attempts

    code_attempts.reset()
    yield
    code_attempts.reset()
