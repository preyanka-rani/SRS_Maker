from app.domain.audit_chain import GENESIS_HASH, compute_entry_hash, verify_chain


def _chain(bodies):
    prev = GENESIS_HASH
    out = []
    for body in bodies:
        h = compute_entry_hash(prev, body)
        out.append({"prev_hash": prev, "hash": h, "body": body})
        prev = h
    return out


def test_valid_chain_verifies():
    chain = _chain([{"a": 1}, {"a": 2}, {"a": 3}])
    assert verify_chain(chain) is True


def test_tampered_body_breaks_chain():
    chain = _chain([{"a": 1}, {"a": 2}])
    chain[0]["body"] = {"a": 99}
    assert verify_chain(chain) is False


def test_removed_entry_breaks_chain():
    chain = _chain([{"a": 1}, {"a": 2}, {"a": 3}])
    del chain[1]
    assert verify_chain(chain) is False


def test_hash_is_key_order_independent():
    assert compute_entry_hash(GENESIS_HASH, {"a": 1, "b": 2}) == compute_entry_hash(
        GENESIS_HASH, {"b": 2, "a": 1}
    )


def test_empty_chain_is_valid():
    assert verify_chain([]) is True
