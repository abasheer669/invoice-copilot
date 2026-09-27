import pytest

from ap_agent.db import connect


def test_connect_refuses_roles_outside_the_allowlist():
    with pytest.raises(ValueError, match="unknown role"), connect("postgres"):
        pass
