from ap_agent.masking import mask, mask_values


def test_long_digit_runs_keep_only_the_last_four():
    assert mask("pay 062000123444471 now") == "pay ***********4471 now"
    assert mask("acct 123456") == "acct **3456"


def test_ordinary_references_and_amounts_are_left_alone():
    text = "INV-5521 for 11000.00 AUD, account ending 4471, PO-7788"
    assert mask(text) == text


def test_nested_values_are_masked():
    assert mask_values({"args": {"account": "062000123444471", "k": 5}, "ids": ["9876543210"]}) == {
        "args": {"account": "***********4471", "k": 5},
        "ids": ["******3210"],
    }
