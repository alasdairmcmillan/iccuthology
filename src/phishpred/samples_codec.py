"""Compact binary codec for raw Monte-Carlo joint samples (deploy plan §4a).

`samples.bin` is the source of truth that lets the web UI answer any run /
chaser / subset query as an *exact* reduction over the simulator's raw output
(deploy plan §4a, DEPLOY-CONTRACTS.md §3). This module is the Python WRITER; the
Cloudflare Worker (`worker/src/samples.ts`) is an independent TypeScript READER.
Both implement the same format and MUST agree byte-for-byte — the reference
vectors in DEPLOY-CONTRACTS.md §3 are the shared conformance suite.

Format (little-endian):
    Header (17 bytes): magic b"PSMP", version 0x01, then n_sims, n_shows,
    n_vocab as uint32 LE.
    Body: for each sim m, for each horizon position t: a uvarint `count`
    followed by `count` uvarint vocab indices, ascending-sorted.
`uvarint` = unsigned LEB128 (protobuf varints).
"""
from __future__ import annotations

import struct

MAGIC = b"PSMP"
VERSION = 1
# magic(4s) version(B) n_sims(I) n_shows(I) n_vocab(I) — "<" => standard sizes,
# no alignment padding => exactly 17 bytes, matching the Worker's HEADER_BYTES.
_HEADER = struct.Struct("<4sBIII")
HEADER_BYTES = _HEADER.size  # 17


def uvarint_encode(n: int) -> bytes:
    """Encode a non-negative int as an unsigned LEB128 varint."""
    if n < 0:
        raise ValueError(f"uvarint cannot encode negative value {n}")
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            break
    return bytes(out)


def uvarint_decode(buf: bytes, pos: int) -> tuple[int, int]:
    """Decode a uvarint starting at `pos`. Returns (value, next_pos)."""
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError(f"uvarint decode: truncated buffer at offset {pos}")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            break
        shift += 7
        if shift > 63:
            raise ValueError("uvarint decode: value too large (>63 bits)")
    return result, pos


def encode_samples(samples: list[list[set[int]]], vocab_index: dict[int, int]) -> bytes:
    """Encode `samples[m][t]` (a set of songids) to the samples.bin format.

    `vocab_index` maps songid -> dense vocab index [0, n_vocab). Every songid in
    `samples` must be present. Indices within one (m, t) are written ascending.
    """
    n_sims = len(samples)
    n_shows = len(samples[0]) if samples else 0
    n_vocab = len(vocab_index)

    out = bytearray(_HEADER.pack(MAGIC, VERSION, n_sims, n_shows, n_vocab))
    for sim in samples:
        for step in sim:
            idxs = sorted(vocab_index[songid] for songid in step)
            out += uvarint_encode(len(idxs))
            for i in idxs:
                out += uvarint_encode(i)
    return bytes(out)


def decode_samples(buf: bytes) -> dict:
    """Inverse of `encode_samples`. Returns a dict with n_sims, n_shows,
    n_vocab, and samples (list[list[list[int]]] of vocab indices)."""
    if len(buf) < HEADER_BYTES:
        raise ValueError(f"samples.bin: buffer too short for header ({len(buf)} bytes)")
    magic, version, n_sims, n_shows, n_vocab = _HEADER.unpack_from(buf, 0)
    if magic != MAGIC:
        raise ValueError(f"samples.bin: bad magic {magic!r}, expected {MAGIC!r}")
    if version != VERSION:
        raise ValueError(f"samples.bin: unsupported version {version}, expected {VERSION}")

    pos = HEADER_BYTES
    samples: list[list[list[int]]] = []
    for _m in range(n_sims):
        row: list[list[int]] = []
        for _t in range(n_shows):
            count, pos = uvarint_decode(buf, pos)
            idxs: list[int] = []
            for _ in range(count):
                value, pos = uvarint_decode(buf, pos)
                idxs.append(value)
            row.append(idxs)
        samples.append(row)
    return {"n_sims": n_sims, "n_shows": n_shows, "n_vocab": n_vocab, "samples": samples}
