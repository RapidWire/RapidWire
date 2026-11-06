from __future__ import annotations

import hashlib

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from rapidwire.api.auth import Principal
from rapidwire.core.tokens import TOKEN_PREFIX, IssuedToken, SecretToken, authenticate_token, generate_token, hash_token, is_well_formed_token

MALFORMED: list[object] = [
    "",
    "rw_",
    "rw_short",
    "xx_" + "a" * 43,
    "rw_" + "a" * 42 + "!",
    "rw_" + "a" * 44,
    "rw_" + "a" * 42 + chr(0xE9),
    "rw_" + "a" * 42 + "'",
    " " + generate_token(),
    generate_token() + "\n",
    None,
    12345,
    b"rw_" + b"a" * 43,
]


def test_generated_tokens_are_well_formed_and_distinct() -> None:
    tokens = {generate_token() for _ in range(100)}
    assert len(tokens) == 100
    for token in tokens:
        assert token.startswith(TOKEN_PREFIX)
        assert is_well_formed_token(token)


def test_hash_is_sha256() -> None:
    token = generate_token()
    assert hash_token(token) == hashlib.sha256(token.encode()).digest()
    assert len(hash_token(token)) == 32


def test_issued_token_repr_hides_the_plaintext() -> None:
    token = generate_token()
    issued = IssuedToken(token=token, token_id=1, execution_id=2, revoked_token_ids=())
    assert token not in repr(issued)
    assert token not in str(issued)


def test_secret_wrapper_redacts_the_plaintext() -> None:
    token = generate_token()
    secret = SecretToken(token)
    assert token not in repr(secret)
    assert token not in str(secret)
    assert token not in repr([secret])
    assert secret.reveal() == token
    principal = Principal(account_id=5, token=secret)
    assert token not in repr(principal)
    assert principal.token is secret


@pytest.mark.parametrize("token", MALFORMED)
def test_malformed_tokens_are_not_well_formed(token: object) -> None:
    assert not is_well_formed_token(token)


@pytest.mark.parametrize("token", MALFORMED)
async def test_malformed_tokens_never_reach_the_database(token: object) -> None:
    engine = create_async_engine("mysql+asyncmy://nobody:unused@127.0.0.1:1/none")
    try:
        assert await authenticate_token(engine, token) is None
    finally:
        await engine.dispose()
