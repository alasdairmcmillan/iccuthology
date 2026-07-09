"""samples.bin codec — the cross-language contract (DEPLOY-CONTRACTS.md §3).

These reference vectors are the shared conformance suite between this Python
writer and the Worker's TypeScript reader (`worker/src/samples.ts`). If either
side changes, both must still pass every vector here.
"""
from __future__ import annotations

import struct

import pytest

from phishpred.samples_codec import (
    MAGIC,
    VERSION,
    decode_samples,
    encode_samples,
    uvarint_decode,
    uvarint_encode,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, b"\x00"),
        (1, b"\x01"),
        (127, b"\x7f"),
        (128, b"\x80\x01"),
        (300, b"\xac\x02"),
    ],
)
def test_uvarint_reference_vectors(value, expected):
    assert uvarint_encode(value) == expected
    decoded, pos = uvarint_decode(expected, 0)
    assert decoded == value
    assert pos == len(expected)


def test_uvarint_roundtrip_large():
    for v in (0, 1, 63, 64, 16383, 16384, 2**20, 2**40):
        buf = uvarint_encode(v)
        assert uvarint_decode(buf, 0) == (v, len(buf))


def test_uvarint_rejects_negative():
    with pytest.raises(ValueError):
        uvarint_encode(-1)


def test_file_reference_vector():
    # DEPLOY-CONTRACTS §3 worked example: n_sims=1, n_shows=1, vocab {0,1,2},
    # single sample {0, 2} -> body bytes [count=2, idx 0, idx 2].
    samples = [[{0, 2}]]
    vocab_index = {0: 0, 1: 1, 2: 2}
    blob = encode_samples(samples, vocab_index)

    header = struct.pack("<4sBIII", MAGIC, VERSION, 1, 1, 3)
    assert blob == header + bytes([0x02, 0x00, 0x02])
    assert blob[:4] == b"PSMP"
    assert blob[4] == 1


def test_encode_decode_roundtrip():
    samples = [
        [{5, 1, 9}, set(), {2}],
        [{9}, {1, 2, 5}, {5}],
    ]
    vocab_index = {sid: i for i, sid in enumerate(sorted({1, 2, 5, 9}))}
    inv = {i: sid for sid, i in vocab_index.items()}

    blob = encode_samples(samples, vocab_index)
    dec = decode_samples(blob)

    assert dec["n_sims"] == 2
    assert dec["n_shows"] == 3
    assert dec["n_vocab"] == 4
    # indices come back ascending-sorted; map back to songids and compare as sets
    for m, sim in enumerate(samples):
        for t, step in enumerate(sim):
            idxs = dec["samples"][m][t]
            assert idxs == sorted(idxs)  # ascending invariant
            assert {inv[i] for i in idxs} == step


def test_decode_rejects_bad_magic():
    blob = b"XXXX" + bytes([1]) + struct.pack("<III", 0, 0, 0)
    with pytest.raises(ValueError, match="magic"):
        decode_samples(blob)


def test_decode_rejects_bad_version():
    blob = struct.pack("<4sBIII", MAGIC, 99, 0, 0, 0)
    with pytest.raises(ValueError, match="version"):
        decode_samples(blob)


def test_decode_rejects_short_buffer():
    with pytest.raises(ValueError, match="too short"):
        decode_samples(b"PSMP")
