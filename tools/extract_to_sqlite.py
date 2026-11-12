#!/usr/bin/env python3
"""Avowed l10n: extract Text_<lang>.uasset from the IoStore container and load all strings into SQLite.

Stdlib only (sqlite3, ctypes, struct, mmap). Binary spec: .agent/skills/avowed-l10n/SKILL.md

Output:
  raw_assets/<Source>_Text_<lang>.uasset   original decompressed Zen packages (templates for the repacker)
  db/avowed_l10n.db                        table `strings`

Re-running is safe: an existing DB is upserted (en/pl/ru refreshed, uk_* / status preserved).
Use --rebuild to drop the DB first.
"""
import argparse
import ctypes
import mmap
import sqlite3
import struct
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_UTOC = r"M:\XboxGames\Avowed\Content\Alabama\Content\Paks\Alabama-WinGDK.utoc"
DEFAULT_OODLE = r"M:\SteamLibrary\steamapps\common\DOOMEternal\oo2core_8_win64.dll"

# Known layout of Alabama-WinGDK.utoc (game build the chunk indexes below were taken from)
EXPECT_HEADER = dict(header_size=144, entry_count=430089, block_count=1985362,
                     block_size=65536, seeds=215045)

SOURCES = ("BaseGame", "D11")
LANGS = ("enus", "plpl", "ruru")
# (source, lang) -> (chunk_idx, expected uncompressed size)
ASSETS = {
    ("BaseGame", "enus"): (4689, 6318939),
    ("BaseGame", "plpl"): (165773, 11551609),
    ("BaseGame", "ruru"): (157656, 10390581),
    ("D11", "enus"): (373035, 3392),
    ("D11", "plpl"): (338689, 5909),
    ("D11", "ruru"): (390019, 5756),
}
PKG_TAG_TAIL = bytes.fromhex("c1832a9e")


# --------------------------------------------------------------------------- IoStore / Oodle
class Oodle:
    def __init__(self, dll_path):
        self.lib = ctypes.CDLL(str(dll_path))
        f = self.lib.OodleLZ_Decompress
        f.restype = ctypes.c_ssize_t
        f.argtypes = [ctypes.c_char_p, ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_ssize_t,
                      ctypes.c_int, ctypes.c_int, ctypes.c_int,
                      ctypes.c_void_p, ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_void_p,
                      ctypes.c_void_p, ctypes.c_ssize_t, ctypes.c_int]
        self._f = f

    def decompress(self, comp: bytes, raw_len: int) -> bytes:
        buf = ctypes.create_string_buffer(raw_len + 64)
        n = self._f(comp, len(comp), buf, raw_len, 1, 0, 0, None, 0, None, None, None, 0, 3)
        if n != raw_len:
            raise RuntimeError(f"Oodle decompress returned {n}, expected {raw_len}")
        return buf.raw[:raw_len]


class IoStore:
    def __init__(self, utoc_path, oodle: Oodle):
        self.utoc_path = Path(utoc_path)
        self.ucas_base = self.utoc_path.with_suffix("")
        self.oodle = oodle
        self._fh = open(self.utoc_path, "rb")
        self.toc = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
        h = self.toc
        if h[:16] != b"-==--==--==--==-":
            raise RuntimeError("not a .utoc file")
        (self.header_size, self.entry_count, self.block_count, blk_entry_size, self.method_count,
         self.method_len, self.block_size, _dir_size, self.partition_count) = struct.unpack_from("<9I", h, 20)
        (self.seeds,) = struct.unpack_from("<I", h, 84)
        (self.partition_size,) = struct.unpack_from("<Q", h, 88)
        (self.no_hash,) = struct.unpack_from("<I", h, 96)
        got = dict(header_size=self.header_size, entry_count=self.entry_count,
                   block_count=self.block_count, block_size=self.block_size, seeds=self.seeds)
        if got != EXPECT_HEADER or blk_entry_size != 12:
            raise RuntimeError("utoc layout differs from the one the chunk indexes were taken from:\n"
                               f"  expected {EXPECT_HEADER}\n  got      {got}")
        n = self.entry_count
        self.ol_off = self.header_size + 12 * n                                   # ChunkOffsetLengths
        self.blk_off = self.ol_off + 10 * n + 4 * self.seeds + 4 * self.no_hash   # CompressionBlocks
        names_off = self.blk_off + 12 * self.block_count                          # compression method names
        self.methods = [None]  # index 0 = uncompressed
        for i in range(self.method_count):
            raw = h[names_off + i * self.method_len: names_off + (i + 1) * self.method_len]
            self.methods.append(raw.split(b"\0", 1)[0].decode("ascii"))
        self._ucas = {}

    def _ucas_file(self, part):
        if part not in self._ucas:
            p = self.ucas_base.with_name(self.ucas_base.name + (f"_s{part}" if part else "") + ".ucas")
            self._ucas[part] = open(p, "rb")
        return self._ucas[part]

    def chunk_location(self, idx):
        rec = self.toc[self.ol_off + 10 * idx: self.ol_off + 10 * idx + 10]
        return int.from_bytes(rec[:5], "big"), int.from_bytes(rec[5:], "big")

    def read_chunk(self, idx) -> bytes:
        off, length = self.chunk_location(idx)
        bs = self.block_size
        first, last = off // bs, (off + length - 1) // bs
        out = bytearray()
        for bi in range(first, last + 1):
            r = self.toc[self.blk_off + 12 * bi: self.blk_off + 12 * bi + 12]
            c_off = int.from_bytes(r[0:5], "little")
            c_sz = int.from_bytes(r[5:8], "little")
            u_sz = int.from_bytes(r[8:11], "little")
            meth = r[11]
            part, p_off = divmod(c_off, self.partition_size) if self.partition_size else (0, c_off)
            f = self._ucas_file(part)
            f.seek(p_off)
            comp = f.read(c_sz)
            if meth == 0:
                out += comp
            elif self.methods[meth].lower() == "oodle":
                out += self.oodle.decompress(comp, u_sz)
            else:
                raise RuntimeError(f"unsupported compression method {self.methods[meth]}")
        skip = off - first * bs
        return bytes(out[skip:skip + length])


# --------------------------------------------------------------------------- Text_<lang>.uasset parser
def read_header(b, pos):
    """UE unversioned property header -> (new_pos, set of property indices that are serialized)."""
    frags = []
    zero_total = 0
    while True:
        (v,) = struct.unpack_from("<H", b, pos)
        pos += 2
        skip, has_zero, last, num = v & 0x7F, (v >> 7) & 1, (v >> 8) & 1, v >> 9
        frags.append((skip, has_zero, num))
        if has_zero:
            zero_total += num
        if last:
            break
    if zero_total == 0:
        zbytes = 0
    elif zero_total <= 8:
        zbytes = 1
    elif zero_total <= 16:
        zbytes = 2
    else:
        zbytes = ((zero_total + 31) // 32) * 4
    zmask = int.from_bytes(b[pos:pos + zbytes], "little")
    pos += zbytes
    present, idx, zi = set(), 0, 0
    for skip, has_zero, num in frags:
        idx += skip
        for _ in range(num):
            if has_zero:
                if not (zmask >> zi) & 1:
                    present.add(idx)
                zi += 1
            else:
                present.add(idx)
            idx += 1
    return pos, present


def read_fstr(b, pos):
    (n,) = struct.unpack_from("<i", b, pos)
    pos += 4
    if n == 0:
        return "", pos
    if n > 0:  # ANSI, includes trailing NUL
        return b[pos:pos + n - 1].decode("latin-1"), pos + n
    n = -n     # UTF-16LE, includes trailing NUL
    return b[pos:pos + 2 * n - 2].decode("utf-16-le"), pos + 2 * n


def parse_names(b):
    (num,) = struct.unpack_from("<I", b, 0x34)
    pos = 0x44 + 8 * num  # skip hashes
    heads = [(b[pos + 2 * i] << 8) | b[pos + 2 * i + 1] for i in range(num)]
    pos += 2 * num
    names = []
    for h in heads:
        ln = h & 0x7FFF
        if h & 0x8000:
            names.append(b[pos:pos + 2 * ln].decode("utf-16-le"))
            pos += 2 * ln
        else:
            names.append(b[pos:pos + ln].decode("utf-8"))
            pos += ln
    return names


def mapped_name(names, idx, num):
    s = names[idx & 0x3FFFFFFF]
    return f"{s}_{num - 1}" if num else s


def skip_str_intset_map(b, pos):
    """TMap<FString, FIntSet> (search index) -- skipped."""
    rem, cnt = struct.unpack_from("<ii", b, pos)
    pos += 8
    if rem:
        raise ValueError("TMap with removals")
    for _ in range(cnt):
        (n,) = struct.unpack_from("<i", b, pos)
        pos += 4 + (n if n > 0 else -2 * n)
        pos, pres = read_header(b, pos)
        if 0 in pres:
            _srem, scnt = struct.unpack_from("<ii", b, pos)
            pos += 8 + 4 * scnt
    return pos


def skip_int_nodelink_map(b, pos):
    """TMap<int32, FNodeLinkArray> -- skipped."""
    rem, cnt = struct.unpack_from("<ii", b, pos)
    pos += 8
    if rem:
        raise ValueError("TMap with removals")
    unpack_h = struct.Struct("<H").unpack_from
    for _ in range(cnt):
        pos += 4
        pos, pres = read_header(b, pos)
        if 0 in pres:
            (acnt,) = struct.unpack_from("<i", b, pos)
            pos += 4
            for _ in range(acnt):
                if unpack_h(b, pos)[0] == 0x0B00:  # fast path: 5 non-zero int32
                    pos += 22
                else:
                    pos, p2 = read_header(b, pos)
                    pos += 4 * len(p2)
    return pos


def parse_text_asset(b):
    """-> (tables, info); tables = [(table_name, [(key, id, default, female, extra), ...]), ...]"""
    if b[-4:] != PKG_TAG_TAIL:
        raise ValueError("missing package tag footer")
    names = parse_names(b)
    (header_size,) = struct.unpack_from("<I", b, 4)
    pos, present = read_header(b, header_size)
    tables = []
    info = {"key_ne_name": 0}
    if 0 in present:
        _rem, tcnt = struct.unpack_from("<ii", b, pos)
        pos += 8
        for _ in range(tcnt):
            kidx, knum = struct.unpack_from("<II", b, pos)
            pos += 8
            tname = mapped_name(names, kidx, knum)
            pos, pres = read_header(b, pos)
            entries = []
            for p in range(8):
                if p not in pres:
                    continue
                if p == 0:
                    nidx, nnum = struct.unpack_from("<II", b, pos)
                    pos += 8
                    if mapped_name(names, nidx, nnum) != tname:
                        info["key_ne_name"] += 1
                elif p <= 3:
                    pos = skip_str_intset_map(b, pos)
                elif p <= 6:
                    pos = skip_int_nodelink_map(b, pos)
                else:
                    _erem, ecnt = struct.unpack_from("<ii", b, pos)
                    pos += 8
                    for _ in range(ecnt):
                        (e_key,) = struct.unpack_from("<i", b, pos)
                        pos += 4
                        pos, ep = read_header(b, pos)
                        e_id = None
                        if 0 in ep:
                            (e_id,) = struct.unpack_from("<i", b, pos)
                            pos += 4
                        d = f = x = ""
                        if 1 in ep:
                            d, pos = read_fstr(b, pos)
                        if 2 in ep:
                            f, pos = read_fstr(b, pos)
                        if 3 in ep:
                            x, pos = read_fstr(b, pos)
                        entries.append((e_key, e_id, d, f, x))
            tables.append((tname, entries))
    info["tail_bytes"] = len(b) - 12 - pos  # 0 == whole export consumed
    return tables, info


# --------------------------------------------------------------------------- category heuristic
def categorize(table_name: str) -> str:
    """'conversations/...' -> 'conversations'; 'game/items' -> 'game/items'; 'quests/...' -> 'quests'."""
    parts = table_name.split("/")
    if parts[0] == "game" and len(parts) > 1:
        return f"game/{parts[1]}"
    return parts[0]


# --------------------------------------------------------------------------- main
SCHEMA = """
CREATE TABLE IF NOT EXISTS strings (
    id                 INTEGER PRIMARY KEY,
    source_file        TEXT    NOT NULL,
    table_name         TEXT    NOT NULL,
    string_id          INTEGER NOT NULL,
    en_default         TEXT    NOT NULL DEFAULT '',
    en_female          TEXT    NOT NULL DEFAULT '',
    pl_default         TEXT    NOT NULL DEFAULT '',
    pl_female          TEXT    NOT NULL DEFAULT '',
    ru_default         TEXT    NOT NULL DEFAULT '',
    ru_female          TEXT    NOT NULL DEFAULT '',
    uk_default         TEXT    NOT NULL DEFAULT '',
    uk_female          TEXT    NOT NULL DEFAULT '',
    has_gender_variant INTEGER NOT NULL DEFAULT 0,
    status             TEXT    NOT NULL DEFAULT 'new',
    category           TEXT    NOT NULL DEFAULT '',
    -- beyond the requested columns: FOEIStringEntry.ExtraText (3rd text variant, e.g. neutral "they" lines)
    en_extra           TEXT    NOT NULL DEFAULT '',
    pl_extra           TEXT    NOT NULL DEFAULT '',
    ru_extra           TEXT    NOT NULL DEFAULT '',
    uk_extra           TEXT    NOT NULL DEFAULT '',
    qa_note            TEXT    NOT NULL DEFAULT '',
    UNIQUE (source_file, table_name, string_id)
);
CREATE INDEX IF NOT EXISTS idx_strings_status ON strings(status);
CREATE INDEX IF NOT EXISTS idx_strings_table  ON strings(source_file, table_name);
"""

UPSERT = """
INSERT INTO strings (source_file, table_name, string_id, en_default, en_female, pl_default, pl_female,
                     ru_default, ru_female, has_gender_variant, category, en_extra, pl_extra, ru_extra)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(source_file, table_name, string_id) DO UPDATE SET
    en_default=excluded.en_default, en_female=excluded.en_female,
    pl_default=excluded.pl_default, pl_female=excluded.pl_female,
    ru_default=excluded.ru_default, ru_female=excluded.ru_female,
    has_gender_variant=excluded.has_gender_variant, category=excluded.category,
    en_extra=excluded.en_extra, pl_extra=excluded.pl_extra, ru_extra=excluded.ru_extra
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--utoc", default=DEFAULT_UTOC)
    ap.add_argument("--oodle", default=DEFAULT_OODLE)
    ap.add_argument("--raw-dir", default=str(REPO / "raw_assets"))
    ap.add_argument("--db", default=str(REPO / "db" / "avowed_l10n.db"))
    ap.add_argument("--force-extract", action="store_true", help="re-extract even if raw files already exist")
    ap.add_argument("--rebuild", action="store_true", help="delete the DB first (DESTROYS uk_* translations)")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    raw_dir, db_path = Path(args.raw_dir), Path(args.db)
    raw_dir.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # 1. extract + parse
    t0 = time.time()
    store = None
    parsed = {}
    for (src, lang), (idx, exp_size) in ASSETS.items():
        path = raw_dir / f"{src}_Text_{lang}.uasset"
        if path.exists() and path.stat().st_size == exp_size and not args.force_extract:
            data = path.read_bytes()
            how = "cached"
        else:
            if store is None:
                store = IoStore(args.utoc, Oodle(args.oodle))
            data = store.read_chunk(idx)
            if len(data) != exp_size:
                raise RuntimeError(f"{src}/{lang}: size {len(data)} != expected {exp_size}")
            path.write_bytes(data)
            how = "extracted"
        tables, info = parse_text_asset(data)
        if info["tail_bytes"] != 0:
            print(f"  WARN {src}/{lang}: {info['tail_bytes']} unparsed bytes before footer")
        parsed[(src, lang)] = (tables, info)
        print(f"[{how:9}] {path.name:28} {len(data):>10,} B  tables={len(tables):>3} "
              f"entries={sum(len(t[1]) for t in tables):>6}")

    # 2. merge by (source, table, key)
    rows, stats = [], {}
    for src in SOURCES:
        per_lang = {}
        for lang in LANGS:
            d = {}
            for tname, ents in parsed[(src, lang)][0]:
                if tname in d:
                    raise ValueError(f"duplicate table {tname} in {src}/{lang}")
                d[tname] = {e[0]: e for e in ents}
            per_lang[lang] = d
        order = list(per_lang["enus"]) + [t for lg in ("plpl", "ruru") for t in per_lang[lg]]
        order = list(dict.fromkeys(order))
        absent = {lg: 0 for lg in LANGS}
        for tname in order:
            cat = categorize(tname)
            keys = []
            for lang in LANGS:
                keys += list(per_lang[lang].get(tname, {}))
            for key in dict.fromkeys(keys):
                vals = {}
                for lang in LANGS:
                    e = per_lang[lang].get(tname, {}).get(key)
                    if e is None:
                        absent[lang] += 1
                        vals[lang] = ("", "", "")
                    else:
                        vals[lang] = (e[2], e[3], e[4])
                # gender variant = a FemaleText in any language; ExtraText (3rd/neutral variant) is not counted
                gender = int(bool(vals["plpl"][1]) or bool(vals["enus"][1]) or bool(vals["ruru"][1]))
                rows.append((src, tname, key, *vals["enus"][:2], *vals["plpl"][:2], *vals["ruru"][:2], gender, cat,
                             vals["enus"][2], vals["plpl"][2], vals["ruru"][2]))
        stats[src] = dict(tables=len(order), absent=absent)

    # 3. SQLite
    if args.rebuild and db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    with con:
        con.executemany(UPSERT, rows)
    print(f"[db] {len(rows):,} rows upserted -> {db_path}  ({time.time() - t0:.0f}s)")

    # 4. summary
    def q(sql, *a):
        return con.execute(sql, a).fetchone()

    print("\nsource     tables  strings   en>0     pl>0     ru>0   en_f  pl_f  ru_f  gender  ex en/pl/ru     absent en/pl/ru")
    for src in (*SOURCES, None):
        w, a = ("source_file=?", (src,)) if src else ("1=1", ())
        r = q(f"""SELECT COUNT(DISTINCT table_name), COUNT(*), SUM(en_default!=''), SUM(pl_default!=''),
                  SUM(ru_default!=''), SUM(en_female!=''), SUM(pl_female!=''), SUM(ru_female!=''),
                  SUM(has_gender_variant), SUM(en_extra!=''), SUM(pl_extra!=''), SUM(ru_extra!='')
                  FROM strings WHERE {w}""", *a)
        if src:
            ab = "/".join(str(stats[src]["absent"][lg]) for lg in LANGS)
        else:
            ab = "/".join(str(sum(stats[x]["absent"][lg] for x in SOURCES)) for lg in LANGS)
        ex = f"{r[9]}/{r[10]}/{r[11]}"
        print(f"{(src or 'TOTAL'):<10}{r[0]:>6}{r[1]:>9}{r[2]:>8}{r[3]:>9}{r[4]:>9}{r[5]:>6}{r[6]:>6}{r[7]:>6}{r[8]:>8}  {ex:<14}  {ab}")
    print("status:", dict(con.execute("SELECT status, COUNT(*) FROM strings GROUP BY status").fetchall()),
          "| uk filled:", q("SELECT SUM(uk_default!='') FROM strings")[0])
    con.close()


if __name__ == "__main__":
    main()
