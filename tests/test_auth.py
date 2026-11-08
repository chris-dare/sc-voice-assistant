import pytest

from app.auth import AuthError, external_id_for, issue_token, verify_token


def test_round_trip():
    tok = issue_token("s", "user_12345678", 60, now=1000)
    assert verify_token("s", tok, now=1001) == "user_12345678"


@pytest.mark.parametrize("mutate", [lambda t: t + "x", lambda t: t.replace("user", "evil"), lambda t: "garbage"])
def test_tampering_rejected(mutate):
    tok = issue_token("s", "user_12345678", 60, now=1000)
    with pytest.raises(AuthError):
        verify_token("s", mutate(tok), now=1001)


def test_expiry_and_wrong_secret():
    tok = issue_token("s", "user_12345678", 60, now=1000)
    with pytest.raises(AuthError):
        verify_token("s", tok, now=2000)
    with pytest.raises(AuthError):
        verify_token("other", tok, now=1001)


def test_external_id_prefix():
    assert external_id_for("abc12345") == "voicepoc-abc12345"
