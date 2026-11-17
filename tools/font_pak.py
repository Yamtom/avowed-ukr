"""Read unencrypted Avowed PAK fonts and write a full-directory-index PAK v11.

Encoded entry layout reference: https://github.com/trumank/repak/blob/master/repak/src/entry.rs
The IoStore helpers remain in build_iostore_patch.py.
"""
import hashlib
import struct
from pathlib import Path

import build_iostore_patch as bp


def fstring(value):
    data = value.encode('utf-8') + b'\0'
    return struct.pack('<i', len(data)) + data


class Reader:
    def __init__(self, path, oodle=None):
        self.data = Path(path).read_bytes()
        self.info = bp.parse_pak_index(self.data, require_both=False)
        self.oodle = oodle
        self.entries = {}
        b = self.data
        pos = self.info['blobs']['fdi'][0]

        def integer():
            nonlocal pos
            value = struct.unpack_from('<i', b, pos)[0]
            pos += 4
            return value

        def string():
            nonlocal pos
            n = integer()
            if n <= 0:
                raise ValueError('expected ANSI path')
            value = b[pos:pos + n - 1].decode('utf-8')
            pos += n
            return value

        for _ in range(integer()):
            directory = string()
            for _ in range(integer()):
                name = string()
                self.entries[directory + name] = integer()
        assert pos == sum(self.info['blobs']['fdi'][:2])
        self.encoded = self.info['idx_off'] + self.info['idx_size'] - 4 - self.info['enc_size']

    def read(self, name):
        b = self.data
        loc = self.entries[name]
        if loc < 0:
            raise ValueError('non-encoded entry unsupported')
        flags = struct.unpack_from('<I', b, self.encoded + loc)[0]
        pos = self.encoded + loc + 4 + (4 if flags & 63 == 63 else 0)
        offset = struct.unpack_from('<I' if flags >> 31 else '<Q', b, pos)[0]
        _, size, usize, method = struct.unpack_from('<QQQI', b, offset)
        pos = offset + 48
        blocks = []
        if method:
            count = struct.unpack_from('<I', b, pos)[0]
            pos += 4
            blocks = [struct.unpack_from('<QQ', b, pos + 16 * j) for j in range(count)]
            pos += 16 * count
        encrypted, block_size = struct.unpack_from('<BI', b, pos)
        pos += 5
        if encrypted:
            raise ValueError('encrypted entry')
        raw = b[pos:pos + size] if not method else b''.join(b[offset + a:offset + z] for a, z in blocks)
        assert hashlib.sha1(raw).digest() == b[offset + 28:offset + 48], name
        if method:
            # Original Avowed uses method 1 = Oodle; refuse other layouts.
            assert method == 1 and b[-160:-128].split(b'\0')[0] == b'Oodle'
            raw = b''.join(self.oodle.decompress(b[offset + a:offset + z], min(block_size, usize - j * block_size))
                           for j, (a, z) in enumerate(blocks))
        assert len(raw) == usize
        return raw


def write(path, files):
    """Write uncompressed entries; verify directory, hashes and payloads by reading back."""
    payload = bytearray()
    encoded = bytearray()
    directories = {'': {}}
    for name, data in sorted(files.items()):
        offset, location = len(payload), len(encoded)
        assert offset < 2**32 and len(data) < 2**32
        encoded += struct.pack('<III', 0xC0000000, offset, len(data))
        payload += struct.pack('<QQQI', 0, len(data), len(data), 0)
        payload += hashlib.sha1(data).digest() + struct.pack('<BI', 0, 0) + data
        parts = name.split('/')
        for i in range(1, len(parts)):
            directories.setdefault('/'.join(parts[:i]) + '/', {})
        directories['/'.join(parts[:-1]) + '/'][parts[-1]] = location
    fdi = struct.pack('<i', len(directories))
    for directory, entries in sorted(directories.items()):
        fdi += fstring(directory) + struct.pack('<i', len(entries))
        for name, location in sorted(entries.items()):
            fdi += fstring(name) + struct.pack('<i', location)
    index_offset = len(payload)
    prefix = fstring('../../../') + struct.pack('<iQi', len(files), 0, 0)
    index_size = len(prefix) + 40 + 4 + len(encoded) + 4
    index = prefix + struct.pack('<iqq', 1, index_offset + index_size, len(fdi)) + hashlib.sha1(fdi).digest()
    index += struct.pack('<i', len(encoded)) + encoded + struct.pack('<i', 0)
    assert len(index) == index_size
    footer = b'\0' * 17 + struct.pack('<IIqq', bp.PAK_MAGIC, 11, index_offset, len(index))
    footer += hashlib.sha1(index).digest() + b'Oodle'.ljust(32, b'\0') + b'\0' * 128
    Path(path).write_bytes(payload + index + fdi + footer)
    check = Reader(path)
    assert set(check.entries) == set(files)
    for name, data in files.items():
        assert check.read(name) == data
