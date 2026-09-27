from ap_agent.result import _sections


def test_citation_sections():
    assert _sections("FIN-POL-003 v4.0 §2-3") == {2, 3}
    assert _sections("FIN-POL-002 v2.4 §4") == {4}
    assert _sections("ADV-001 v1.0 (UNTRUSTED)") == set()
