#!/usr/bin/env python3
"""Avowed l10n: build the patch mod  Alabama-WinGDK_Ukr_P.{utoc,ucas,pak}  (IoStore v5, UE5, Zen packages).

The mod replaces the *ExportBundleData* chunks of Text_plpl.uasset (BaseGame) and D11/Text_plpl.uasset with
packages re-serialized from db/avowed_l10n.db (tools/repack_uasset.py). The chunk ids are copied from the original
Alabama-WinGDK.utoc, so the engine loads our data instead of the original (higher pak order: `_P`).

Container produced (everything verified against the original container, see `selftest`):
  .ucas  uncompressed 64 KiB blocks (CompressionMethod 0), chunk offsets aligned to 65536
  .utoc  v5 header, ChunkIds in perfect-hash slot order, offsets/lengths, perfect-hash seeds, compression blocks,
         ChunkMetas (BLAKE3-20 + 12 zero bytes + flags). ContainerFlags = 0 (not compressed, no directory index)
  chunks: [D11 text package, BaseGame text package, ContainerHeader v3 with the two original package store entries]
  .pak   minimal valid UE pak v11 with an empty index - UE only mounts a .utoc that has a same-named .pak

Sub-commands:  selftest | build [--install]
"""
import argparse
import hashlib
import mmap
import shutil
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extract_to_sqlite as ex  # noqa: E402
import repack_uasset as rp  # noqa: E402
from blake3_py import blake3  # noqa: E402

REPO = ex.REPO
GAME_PAKS = r"M:\XboxGames\Avowed\Content\Alabama\Content\Paks"
TOC_MAGIC = b"-==--==--==--==-"
HEADER_SIZE, BLOCK_SIZE, TOC_VERSION = 144, 65536, 5
PARTITION_NONE = 0xFFFFFFFFFFFFFFFF
CHUNK_TYPE_EXPORT_BUNDLE, CHUNK_TYPE_CONTAINER_HEADER = 1, 6
CONTAINER_HEADER_SIG, CONTAINER_HEADER_VER = 0x496F436E, 3
PAK_MAGIC, PAK_VERSION = 0x5A6F12E1, 11
M64 = (1 << 64) - 1


# =========================================================================== perfect hash (validated on the original)
def hash_with_seed(seed, cid):
    h = (seed & M64) if seed else 0xCBF29CE484222325
    for b in cid:
        h = ((h * 0x100000001B3) & M64) ^ b
    return h


def perfect_lookup(ids, seeds, cid):
    """UE FIoStoreReader chunk lookup. ids = ChunkIds in slot order. -> index or None."""
    n, s = len(ids), len(seeds)
    if not n:
        return None
    seed = seeds[hash_with_seed(0, cid) % s]
    if seed == 0:
        return None
    slot = (-seed - 1) if seed < 0 else hash_with_seed(seed, cid) % n
    return slot if slot < n and ids[slot] == cid else None


def build_perfect_hash(ids):
    """-> (ids in slot order, seeds). SeedCount = ceil(N/2) like the original container."""
    n = len(ids)
    s = max(1, (n + 1) // 2)
    buckets = [[] for _ in range(s)]
    for cid in ids:
        buckets[hash_with_seed(0, cid) % s].append(cid)
    seeds, slot_of, free = [0] * s, {}, set(range(n))
    for bi in sorted(range(s), key=lambda i: -len(buckets[i])):
        items = buckets[bi]
        if len(items) < 2:
            break
        for seed in range(1, 1 << 24):
            slots = [hash_with_seed(seed, c) % n for c in items]
            if len(set(slots)) == len(slots) and free.issuperset(slots):
                break
        else:
            raise RuntimeError("no perfect-hash seed found")
        seeds[bi] = seed
        for c, sl in zip(items, slots):
            slot_of[c] = sl
        free -= set(slots)
    free_sorted = sorted(free)
    for bi in range(s):
        if len(buckets[bi]) == 1:
            sl = free_sorted.pop(0)
            seeds[bi] = -sl - 1
            slot_of[buckets[bi][0]] = sl
    ordered = [None] * n
    for c, sl in slot_of.items():
        ordered[sl] = c
    assert None not in ordered
    return ordered, seeds


# =========================================================================== TOC reader (original + our output)
class TocReader:
    def __init__(self, toc_path, oodle=None):
        self.path = Path(toc_path)
        self._fh = open(self.path, "rb")
        self.t = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
        t = self.t
        if t[:16] != TOC_MAGIC:
            raise ValueError("not a utoc")
        self.version = t[16]
        (self.hs, self.n, self.nblocks, bes, self.nmeth, self.meth_len, self.bsize, self.dir_size,
         self.nparts) = struct.unpack_from("<9I", t, 20)
        (self.container_id,) = struct.unpack_from("<Q", t, 56)
        self.flags = t[80]
        (self.nseeds,) = struct.unpack_from("<I", t, 84)
        (self.part_size,) = struct.unpack_from("<Q", t, 88)
        (self.nohash,) = struct.unpack_from("<I", t, 96)
        if self.version != TOC_VERSION or bes != 12 or self.flags & 4 or self.nohash:
            raise ValueError("unsupported toc variant")
        n = self.n
        self.ol_off = self.hs + 12 * n
        self.seed_off = self.ol_off + 10 * n
        self.blk_off = self.seed_off + 4 * self.nseeds
        self.meth_off = self.blk_off + 12 * self.nblocks
        self.meta_off = self.meth_off + self.meth_len * self.nmeth + self.dir_size
        self.methods = [None] + [bytes(t[self.meth_off + i * self.meth_len:self.meth_off + (i + 1) * self.meth_len])
                                 .split(b"\0", 1)[0].decode() for i in range(self.nmeth)]
        self.seeds = struct.unpack_from(f"<{self.nseeds}i", t, self.seed_off)
        self.expected_len = self.meta_off + 33 * n
        self.oodle = oodle
        self.ucas = self.path.with_suffix(".ucas")

    def chunk_id(self, i):
        return bytes(self.t[self.hs + 12 * i:self.hs + 12 * i + 12])

    def all_ids(self):
        return [self.chunk_id(i) for i in range(self.n)]

    def lookup(self, cid, ids=None):
        ids = ids if ids is not None else self._ids()
        return perfect_lookup(ids, self.seeds, cid)

    def _ids(self):
        if not hasattr(self, "_ids_cache"):
            self._ids_cache = self.all_ids()
        return self._ids_cache

    def location(self, i):
        r = self.t[self.ol_off + 10 * i:self.ol_off + 10 * i + 10]
        return int.from_bytes(r[:5], "big"), int.from_bytes(r[5:], "big")

    def meta(self, i):
        r = bytes(self.t[self.meta_off + 33 * i:self.meta_off + 33 * i + 33])
        return r[:20], r[20:32], r[32]

    def read(self, i):
        off, ln = self.location(i)
        first, last = off // self.bsize, (off + ln - 1) // self.bsize
        out = bytearray()
        with open(self.ucas, "rb") as f:
            for bi in range(first, last + 1):
                r = self.t[self.blk_off + 12 * bi:self.blk_off + 12 * bi + 12]
                c_off, c_sz = int.from_bytes(r[:5], "little"), int.from_bytes(r[5:8], "little")
                u_sz, meth = int.from_bytes(r[8:11], "little"), r[11]
                f.seek(c_off)
                comp = f.read(c_sz)
                if meth == 0:
                    if c_sz != u_sz:
                        raise ValueError("raw block with c_sz != u_sz")
                    out += comp
                elif self.methods[meth].lower() == "oodle" and self.oodle:
                    out += self.oodle.decompress(comp, u_sz)
                else:
                    raise ValueError(f"cannot read block method {self.methods[meth]}")
        skip = off - first * self.bsize
        return bytes(out[skip:skip + ln])


# =========================================================================== ContainerHeader v3
def parse_container_header(d):
    sig, ver, cid = struct.unpack_from("<IIQ", d, 0)
    if sig != CONTAINER_HEADER_SIG or ver != CONTAINER_HEADER_VER:
        raise ValueError(f"unsupported container header sig={sig:#x} ver={ver}")
    (n,) = struct.unpack_from("<i", d, 16)
    pos = 20
    ids = list(struct.unpack_from(f"<{n}Q", d, pos))
    pos += 8 * n
    (bsz,) = struct.unpack_from("<i", d, pos)
    pos += 4
    blob = d[pos:pos + bsz]
    pos += bsz
    entries = []
    for k in range(n):
        a = 16 * k
        inum, ioff, snum, soff = struct.unpack_from("<4I", blob, a)
        imp = struct.unpack_from(f"<{inum}Q", blob, a + ioff) if inum else ()
        sh = tuple(bytes(blob[a + 8 + soff + 20 * j:a + 8 + soff + 20 * j + 20]) for j in range(snum))
        entries.append((tuple(imp), sh))
    # tail: OptionalSegmentPackageIds(0), OptionalSegmentStoreEntries(0), RedirectsNameMap, LocalizedPackages, Redirects
    z1, z2 = struct.unpack_from("<ii", d, pos)
    if (z1, z2) != (0, 0):
        raise ValueError("optional segments not supported")
    pos += 8
    num, strbytes = struct.unpack_from("<II", d, pos)
    nb_len = (16 + 8 * num + 2 * num + strbytes) if num else 4
    names_raw = bytes(d[pos:pos + nb_len])
    pos += nb_len
    (loc_n,) = struct.unpack_from("<i", d, pos)
    pos += 4
    localized = bytes(d[pos:pos + 16 * loc_n])
    pos += 16 * loc_n
    (red_n,) = struct.unpack_from("<i", d, pos)
    pos += 4
    if red_n or pos != len(d):
        raise ValueError("unsupported package redirects / trailing bytes in container header")
    return dict(container_id=cid, ids=ids, entries=entries, names_raw=names_raw, localized=localized, loc_n=loc_n)


def serialize_blob(entries):
    k = len(entries)
    fixed, data = bytearray(16 * k), bytearray()
    for i, (imp, sh) in enumerate(entries):
        a = 16 * i
        ioff = soff = 0
        if imp:
            ioff = 16 * k + len(data) - a
            data += struct.pack(f"<{len(imp)}Q", *imp)
        if sh:
            soff = 16 * k + len(data) - (a + 8)
            data += b"".join(sh)
        struct.pack_into("<4I", fixed, a, len(imp), ioff, len(sh), soff)
    return bytes(fixed + data)


def serialize_container_header(h):
    blob = serialize_blob(h["entries"])
    return b"".join([
        struct.pack("<IIQ", CONTAINER_HEADER_SIG, CONTAINER_HEADER_VER, h["container_id"]),
        struct.pack("<i", len(h["ids"])), struct.pack(f"<{len(h['ids'])}Q", *h["ids"]),
        struct.pack("<i", len(blob)), blob,
        struct.pack("<ii", 0, 0),
        h["names_raw"],
        struct.pack("<i", h["loc_n"]), h["localized"],
        struct.pack("<i", 0),
    ])


def carve_container_header(orig, container_id, package_ids):
    """Header for the patch container: only `package_ids`, with their original store entries.
    The name batch of the original is kept verbatim (a valid, unreferenced entry); no localized packages."""
    pos = {p: i for i, p in enumerate(orig["ids"])}
    ids = sorted(package_ids)
    return dict(container_id=container_id, ids=ids, entries=[orig["entries"][pos[p]] for p in ids],
                names_raw=orig["names_raw"], localized=b"", loc_n=0)


# =========================================================================== container writer
def pack_block_entry(c_off, c_sz, u_sz, meth):
    return c_off.to_bytes(5, "little") + c_sz.to_bytes(3, "little") + u_sz.to_bytes(3, "little") + bytes([meth])


def write_container(chunks, container_id, out_base: Path):
    """chunks: list of (chunk_id[12], data). Writes <out_base>.utoc/.ucas (uncompressed, no directory index)."""
    ids = [c[0] for c in chunks]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate chunk ids")
    data_of = dict(chunks)
    ordered, seeds = build_perfect_hash(ids)
    n = len(ordered)

    ucas, blocks, offlens, metas = bytearray(), [], [], []
    virt = 0
    for cid in ordered:
        d = data_of[cid]
        nb = (len(d) + BLOCK_SIZE - 1) // BLOCK_SIZE
        for b in range(nb):
            piece = d[b * BLOCK_SIZE:(b + 1) * BLOCK_SIZE]
            blocks.append(pack_block_entry(len(ucas), len(piece), len(piece), 0))
            ucas += piece
            ucas += b"\0" * (-len(ucas) % 16)
        offlens.append(virt.to_bytes(5, "big") + len(d).to_bytes(5, "big"))
        virt += nb * BLOCK_SIZE
        metas.append(blake3(d)[:20] + b"\0" * 12 + b"\0")        # flags 0: not compressed, not memory-mapped

    hdr = bytearray(HEADER_SIZE)
    hdr[0:16] = TOC_MAGIC
    hdr[16] = TOC_VERSION
    struct.pack_into("<9I", hdr, 20, HEADER_SIZE, n, len(blocks), 12, 0, 32, BLOCK_SIZE, 0, 1)
    struct.pack_into("<Q", hdr, 56, container_id)
    struct.pack_into("<I", hdr, 84, len(seeds))
    struct.pack_into("<Q", hdr, 88, PARTITION_NONE)
    toc = b"".join([bytes(hdr), b"".join(ordered), b"".join(offlens), struct.pack(f"<{len(seeds)}i", *seeds),
                    b"".join(blocks), b"".join(metas)])
    out_base = Path(out_base)
    out_base.with_suffix(".utoc").write_bytes(toc)
    out_base.with_suffix(".ucas").write_bytes(bytes(ucas))
    return dict(chunks=n, blocks=len(blocks), toc=len(toc), ucas=len(ucas))


# =========================================================================== minimal pak v11
def parse_pak_index(data, require_both=True):
    """Parse a v10/v11 pak the way UE FPakFile::LoadIndexInternal does. Returns a dict; raises on anything the
    engine would call 'Corrupt pak PrimaryIndex' (neither secondary index, bad hashes, offsets outside the file)."""
    foot = data[-221:]
    magic, ver, idx_off, idx_size = struct.unpack_from("<IIqq", foot, 17)
    if (magic, ver) != (PAK_MAGIC, PAK_VERSION) or foot[16] != 0:
        raise ValueError("pak magic/version/encryption")
    index = data[idx_off:idx_off + idx_size]
    if hashlib.sha1(index).digest() != foot[41:61]:
        raise ValueError("pak primary index hash")
    (mlen,) = struct.unpack_from("<i", index, 0)
    pos = 4 + mlen
    mount = index[4:pos - 1].decode("ascii")
    num_entries, seed, has_phi = struct.unpack_from("<iQi", index, pos)
    pos += 16
    out = dict(mount=mount, num_entries=num_entries, seed=seed, idx_off=idx_off, idx_size=idx_size)
    blobs = {}
    if has_phi:
        off, size = struct.unpack_from("<qq", index, pos)
        blobs["phi"] = (off, size, bytes(index[pos + 16:pos + 36]))
        pos += 36
    (has_fdi,) = struct.unpack_from("<i", index, pos)
    pos += 4
    if has_fdi:
        off, size = struct.unpack_from("<qq", index, pos)
        blobs["fdi"] = (off, size, bytes(index[pos + 16:pos + 36]))
        pos += 36
    if require_both and not (has_phi and has_fdi):
        raise ValueError("pak has no path-hash index and/or no full-directory index -> engine: Corrupt pak PrimaryIndex")
    if not (has_phi or has_fdi):
        raise ValueError("neither path-hash index nor full-directory index -> engine: Corrupt pak PrimaryIndex")
    for name, (off, size, sha) in blobs.items():
        if off < 0 or size < 0 or off + size > len(data) - 221:
            raise ValueError(f"{name} index outside the file")
        if hashlib.sha1(data[off:off + size]).digest() != sha:
            raise ValueError(f"{name} index hash")
    (enc_size,) = struct.unpack_from("<i", index, pos)
    pos += 4 + enc_size
    (files,) = struct.unpack_from("<i", index, pos)
    pos += 4
    if pos != len(index):
        raise ValueError(f"primary index: {len(index) - pos} unparsed bytes")
    out.update(blobs=blobs, enc_size=enc_size, files=files)
    return out


def skip_dir_index(buf, pos):
    """TMap<FString, TMap<FString,int32>> (pak DirectoryIndex) -> end position."""
    def fstr(p):
        (n,) = struct.unpack_from("<i", buf, p)
        return p + 4 + (n if n > 0 else -2 * n)
    (dirs,) = struct.unpack_from("<i", buf, pos)
    pos += 4
    for _ in range(dirs):
        pos = fstr(pos)
        (files,) = struct.unpack_from("<i", buf, pos)
        pos += 4
        for _ in range(files):
            pos = fstr(pos) + 4
    return pos


def build_pak(mount_point="../../../"):
    """Smallest pak v11 the engine accepts: empty primary index + empty path-hash index + empty full-directory
    index (UE rejects a v10+ pak with neither secondary index). Layout like the original/UnrealPak/repak:
    primary index at offset 0, then the two secondary blobs, then the 221-byte footer = 347 bytes."""
    ms = mount_point.encode("ascii") + b"\0"
    phi = struct.pack("<ii", 0, 0)        # PathHashIndex TMap<u64,loc> count 0 + pruned DirectoryIndex count 0
    fdi = struct.pack("<i", 0)            # FullDirectoryIndex: DirectoryIndex count 0
    primary_len = 4 + len(ms) + 4 + 8 + 2 * (4 + 8 + 8 + 20) + 4 + 4
    phi_off, fdi_off = primary_len, primary_len + len(phi)
    sha = lambda b: hashlib.sha1(b).digest()  # noqa: E731
    index = b"".join([struct.pack("<i", len(ms)), ms,
                      struct.pack("<i", 0),                                    # NumEntries
                      struct.pack("<Q", 0),                                    # PathHashSeed
                      struct.pack("<i", 1), struct.pack("<qq", phi_off, len(phi)), sha(phi),   # bHasPathHashIndex
                      struct.pack("<i", 1), struct.pack("<qq", fdi_off, len(fdi)), sha(fdi),   # bHasFullDirectoryIndex
                      struct.pack("<i", 0),                                    # EncodedPakEntries (TArray<u8>) size
                      struct.pack("<i", 0)])                                   # non-encoded FileCount
    assert len(index) == primary_len
    footer = b"".join([b"\0" * 16, b"\0",                                      # EncryptionKeyGuid, bEncryptedIndex
                       struct.pack("<IIqq", PAK_MAGIC, PAK_VERSION, 0, len(index)),
                       sha(index),
                       b"Oodle".ljust(32, b"\0") + b"\0" * 128])               # 5 x 32 B compression method names
    return index + phi + fdi + footer


def verify_pak(data):
    foot = data[-221:]
    assert foot[:17] == b"\0" * 17, "pak guid/encryption"
    info = parse_pak_index(data)
    assert info["mount"] == "../../../" and info["num_entries"] == 0 and info["files"] == 0 and info["enc_size"] == 0
    assert info["idx_off"] + info["idx_size"] + sum(b[1] for b in info["blobs"].values()) + 221 == len(data), "layout"
    return True


# =========================================================================== verification of a written container
def verify_container(base: Path, chunks, container_id, expect_header):
    tr = TocReader(base.with_suffix(".utoc"))
    assert tr.expected_len == base.with_suffix(".utoc").stat().st_size, "toc size"
    assert tr.container_id == container_id and tr.flags == 0 and tr.nmeth == 0 and tr.part_size == PARTITION_NONE
    assert tr.nseeds == max(1, (tr.n + 1) // 2) and tr.n == len(chunks)
    ids = tr.all_ids()
    assert len(set(ids)) == tr.n
    for cid, data in chunks:
        i = tr.lookup(cid, ids)
        assert i is not None, f"chunk {cid.hex()} not found by perfect hash"
        off, ln = tr.location(i)
        assert off % BLOCK_SIZE == 0 and ln == len(data)
        got = tr.read(i)
        assert got == data, f"chunk {cid.hex()} data differs"
        h, pad, fl = tr.meta(i)
        assert h == blake3(data)[:20] and pad == b"\0" * 12 and fl == 0, "chunk meta"
    # container header chunk must parse back to exactly what we meant to write
    hid = struct.pack("<Q", container_id) + b"\0\0\0" + bytes([CHUNK_TYPE_CONTAINER_HEADER])
    hdata = tr.read(tr.lookup(hid, ids))
    assert parse_container_header(hdata) == parse_container_header(serialize_container_header(expect_header))
    assert serialize_container_header(parse_container_header(hdata)) == hdata
    return tr


# =========================================================================== commands
def open_original(args):
    oodle = ex.Oodle(args.oodle)
    orig = TocReader(args.utoc, oodle)
    store = ex.IoStore(args.utoc, oodle)       # same chunk reader as extract_to_sqlite.py
    return orig, store


def cmd_selftest(args):
    orig, store = open_original(args)
    ids = orig.all_ids()
    bad = sum(perfect_lookup(ids, orig.seeds, c) != i for i, c in enumerate(ids))
    print(f"[1] perfect hash lookup on original: {len(ids) - bad}/{len(ids)} chunks found at own index")
    assert not bad
    assert orig.nseeds == max(1, (orig.n + 1) // 2), "SeedCount formula"
    assert (orig.meth_len, orig.nparts, orig.part_size, orig.bsize) == (32, 1, PARTITION_NONE, BLOCK_SIZE)
    new_ids, new_seeds = build_perfect_hash(ids[:1000])
    assert all(perfect_lookup(new_ids, new_seeds, c) is not None for c in new_ids)
    print("[2] build_perfect_hash(1000 original ids): every id found")
    hid = struct.pack("<Q", orig.container_id) + b"\0\0\0" + bytes([CHUNK_TYPE_CONTAINER_HEADER])
    hi = orig.lookup(hid, ids)
    hdr_bytes = orig.read(hi)
    h = parse_container_header(hdr_bytes)
    assert serialize_container_header(h) == hdr_bytes
    print(f"[3] original ContainerHeader ({len(hdr_bytes):,} B, {len(h['ids']):,} packages): parse -> serialize is byte-identical")
    assert orig.meta(hi)[0] == blake3(hdr_bytes)[:20]
    print("[4] original ChunkMeta of the ContainerHeader == BLAKE3-20 of its data")
    for (src, lang), (idx, size) in ex.ASSETS.items():
        if lang != "plpl":
            continue
        tmpl = (REPO / "raw_assets" / f"{src}_Text_{lang}.uasset").read_bytes()
        assert orig.location(idx)[1] == size == len(tmpl)
        assert orig.read(idx) == tmpl == store.read_chunk(idx)
        assert orig.meta(idx)[0] == blake3(tmpl)[:20]
        cid = orig.chunk_id(idx)
        assert cid[11] == CHUNK_TYPE_EXPORT_BUNDLE and orig.lookup(cid, ids) == idx
        print(f"[5] {src} {lang}: chunk {idx} id={cid.hex()} reads == raw_assets, meta hash OK")
    pak = build_pak()
    assert verify_pak(pak)
    orig_pak = Path(args.utoc).with_suffix(".pak")
    with open(orig_pak, "rb") as f:
        f.seek(-221, 2)
        of = f.read(221)
    assert of[:17] == pak[-221:][:17] and of[17:25] == pak[-221:][17:25] and of[61:] == pak[-221:][61:]
    print("[6] pak footer layout (guid/enc/magic/version/compression names) identical to the original pak footer")
    # the original pak is parsed with the same code that validates our pak, and its two secondary indexes are walked
    # to their exact end: PHI = TMap<u64,i32> + pruned DirectoryIndex, FDI = DirectoryIndex.
    with open(orig_pak, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as pm:
        oi = parse_pak_index(pm)
        phi_off, phi_size, _ = oi["blobs"]["phi"]
        fdi_off, fdi_size, _ = oi["blobs"]["fdi"]
        (cnt,) = struct.unpack_from("<i", pm, phi_off)
        assert cnt == oi["num_entries"] and oi["idx_off"] + oi["idx_size"] == phi_off and phi_off + phi_size == fdi_off
        assert skip_dir_index(pm, phi_off + 4 + 12 * cnt) == phi_off + phi_size
        assert skip_dir_index(pm, fdi_off) == fdi_off + fdi_size
        print(f"[7] original pak ({oi['num_entries']} entries): primary index + path-hash index + full-directory index parse "
              f"to the exact byte; both flags=1, SHA1s OK")
    print("selftest OK")
    return 0


def cmd_build(args):
    orig, store = open_original(args)
    ids = orig.all_ids()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / args.name

    chunks, pkg_ids = [], []
    for src in ex.SOURCES:
        idx, size = ex.ASSETS[(src, args.lang)]
        cid = orig.chunk_id(idx)
        assert cid[11] == CHUNK_TYPE_EXPORT_BUNDLE and orig.location(idx)[1] == size, "chunk table out of date"
        tmpl_live = orig.meta(idx)[0] == blake3(rp.template_path(args.raw_dir, src, args.lang).read_bytes())[:20]
        assert tmpl_live, f"raw_assets/{src}_Text_{args.lang} no longer matches the game's chunk {idx} (game updated?)"
        data, tmpl, stats = rp.build_asset(args.raw_dir, args.db, src, args.lang, args.text, args.fallback)
        chunks.append((cid, data))
        pkg_ids.append(struct.unpack_from("<Q", cid, 0)[0])
        print(f"[asset] {src}: chunk {idx} id={cid.hex()} {len(tmpl):,} -> {len(data):,} B, verified: {stats}")

    container_id = int.from_bytes(hashlib.sha256(args.name.encode()).digest()[:8], "little")
    hid = struct.pack("<Q", orig.container_id) + b"\0\0\0" + bytes([CHUNK_TYPE_CONTAINER_HEADER])
    orig_hdr = parse_container_header(orig.read(orig.lookup(hid, ids)))
    new_hdr = carve_container_header(orig_hdr, container_id, pkg_ids)
    chunks.append((struct.pack("<Q", container_id) + b"\0\0\0" + bytes([CHUNK_TYPE_CONTAINER_HEADER]),
                   serialize_container_header(new_hdr)))

    info = write_container(chunks, container_id, base)
    verify_container(base, chunks, container_id, new_hdr)
    pak = build_pak()
    assert verify_pak(pak)
    base.with_suffix(".pak").write_bytes(pak)
    print(f"[mod] {base.name}: containerId={container_id:#018x} chunks={info['chunks']} blocks={info['blocks']} "
          f".utoc={info['toc']:,} B .ucas={info['ucas']:,} B .pak={len(pak)} B -> verified by re-reading")

    if args.install:
        dst = Path(args.install_dir)
        for ext in (".pak", ".utoc", ".ucas"):
            shutil.copyfile(base.with_suffix(ext), dst / (args.name + ext))
        print(f"[install] copied 3 files to {dst}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--utoc", default=ex.DEFAULT_UTOC)
    ap.add_argument("--oodle", default=ex.DEFAULT_OODLE)
    ap.add_argument("--raw-dir", default=str(REPO / "raw_assets"))
    ap.add_argument("--db", default=str(REPO / "db" / "avowed_l10n.db"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest", help="validate the format code against the ORIGINAL container (read-only)")
    b = sub.add_parser("build")
    b.add_argument("--out-dir", default=str(REPO / "build"))
    b.add_argument("--name", default="Alabama-WinGDK_Ukr_P")
    b.add_argument("--lang", choices=("plpl", "ruru", "enus"), default="plpl", help="game language slot that is replaced")
    b.add_argument("--text", choices=rp.TEXT_LANGS, default="uk")
    b.add_argument("--fallback", choices=("en", "pl", "ru", "template"), default="en",
                   help="text for rows without uk_default (default: English)")
    b.add_argument("--install", action="store_true", help="copy .pak/.utoc/.ucas into --install-dir")
    b.add_argument("--install-dir", default=GAME_PAKS)
    args = ap.parse_args()
    sys.exit({"selftest": cmd_selftest, "build": cmd_build}[args.cmd](args))


if __name__ == "__main__":
    main()
