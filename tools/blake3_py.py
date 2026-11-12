"""Minimal pure-Python BLAKE3 (stdlib only), used for IoStore ChunkMetas (UE stores the first 20 bytes).

The compression function is generated as straight-line code at import time (7 rounds x 8 G, message
schedule resolved statically), which makes hashing ~10 MB take a few seconds.
Self-test: python tools/blake3_py.py
"""
import struct

_IV = (0x6A09E667, 0xBB67AE85, 0x3C6EF372, 0xA54FF53A, 0x510E527F, 0x9B05688C, 0x1F83D9AB, 0x5BE0CD19)
_PERM = (2, 6, 3, 10, 7, 0, 4, 13, 1, 11, 12, 5, 9, 14, 15, 8)
CHUNK_START, CHUNK_END, PARENT, ROOT = 1, 2, 4, 8
_G = ((0, 4, 8, 12, 0, 1), (1, 5, 9, 13, 2, 3), (2, 6, 10, 14, 4, 5), (3, 7, 11, 15, 6, 7),
      (0, 5, 10, 15, 8, 9), (1, 6, 11, 12, 10, 11), (2, 7, 8, 13, 12, 13), (3, 4, 9, 14, 14, 15))


def _gen_compress():
    L = ["def compress(cv, m, counter, block_len, flags):",
         "    M = 0xFFFFFFFF",
         "    v0, v1, v2, v3, v4, v5, v6, v7 = cv",
         "    v8, v9, v10, v11 = %d, %d, %d, %d" % _IV[:4],
         "    v12 = counter & M; v13 = (counter >> 32) & M; v14 = block_len; v15 = flags",
         "    " + ", ".join(f"m{i}" for i in range(16)) + " = m"]
    sched = list(range(16))
    for _ in range(7):
        for a, b, c, d, mx, my in _G:
            X, Y = f"m{sched[mx]}", f"m{sched[my]}"
            L += [f"    v{a} = (v{a} + v{b} + {X}) & M; t = v{d} ^ v{a}; v{d} = ((t >> 16) | (t << 16)) & M",
                  f"    v{c} = (v{c} + v{d}) & M; t = v{b} ^ v{c}; v{b} = ((t >> 12) | (t << 20)) & M",
                  f"    v{a} = (v{a} + v{b} + {Y}) & M; t = v{d} ^ v{a}; v{d} = ((t >> 8) | (t << 24)) & M",
                  f"    v{c} = (v{c} + v{d}) & M; t = v{b} ^ v{c}; v{b} = ((t >> 7) | (t << 25)) & M"]
        sched = [sched[_PERM[i]] for i in range(16)]
    L.append("    return (" + ", ".join(f"v{i} ^ v{i + 8}" for i in range(8)) + ")")
    ns = {}
    exec("\n".join(L), ns)
    return ns["compress"]


_compress = _gen_compress()


def _chunk_cv(chunk: bytes, counter: int, root: bool):
    """-> 8-word chaining value of a (<=1024 B) chunk; if root, returns the root output words instead."""
    cv = _IV
    nblocks = max(1, (len(chunk) + 63) // 64)
    for j in range(nblocks):
        blk = chunk[64 * j:64 * j + 64]
        blen = len(blk)
        words = struct.unpack("<16I", blk + b"\0" * (64 - blen))
        flags = (CHUNK_START if j == 0 else 0) | (CHUNK_END if j == nblocks - 1 else 0)
        if root and j == nblocks - 1:
            flags |= ROOT
        cv = _compress(cv, words, counter, blen, flags)
    return cv


def _parent(left, right, root=False):
    return _compress(_IV, left + right, 0, 64, PARENT | (ROOT if root else 0))


def blake3(data: bytes) -> bytes:
    """32-byte BLAKE3 digest."""
    data = bytes(data)
    if len(data) <= 1024:
        return struct.pack("<8I", *_chunk_cv(data, 0, True))
    stack, n = [], (len(data) + 1023) // 1024
    for i in range(n):
        cv = _chunk_cv(data[1024 * i:1024 * i + 1024], i, False)
        t = i + 1
        while t & 1 == 0:
            cv = _parent(stack.pop(), cv)
            t >>= 1
        stack.append(cv)
    cv = stack.pop()
    while stack:
        left = stack.pop()
        cv = _parent(left, cv, root=not stack)
    return struct.pack("<8I", *cv)


def selftest():
    assert blake3(b"").hex() == "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"
    assert blake3(b"abc").hex() == "6437b3ac38465133ffb63b75273a8db548c558465d79db03fd359c6cd5bd9d85"
    return True


if __name__ == "__main__":
    print("blake3 selftest:", selftest())
