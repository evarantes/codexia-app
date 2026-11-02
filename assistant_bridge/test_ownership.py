"""Owner isolation regression checks; no providers or database required."""
from assistant_bridge.ownership import is_project_owner


def test_two_accounts_are_isolated():
    alice = {"_production": {"user_id": 101}}
    bob = {"_production": {"user_id": 202}}
    assert is_project_owner(alice, 101)
    assert not is_project_owner(alice, 202)
    assert is_project_owner(bob, 202)
    assert not is_project_owner(bob, 101)


def test_missing_or_malformed_ownership_fails_closed():
    assert not is_project_owner({}, 101)
    assert not is_project_owner({"_production": None}, 101)
    assert not is_project_owner({"_production": {"user_id": "101"}}, 101)
    assert not is_project_owner({"_production": {"user_id": True}}, 1)
