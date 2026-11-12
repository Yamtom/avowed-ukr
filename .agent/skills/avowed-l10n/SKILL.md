---
name: avowed-l10n
description: Ukrainian localization of Avowed (UE5 IoStore). Binary spec of Text_<lang>.uasset (UStringTableBundleSet), utoc/ucas layout, chunk indexes, project layout, SQLite schema. Read before touching extraction, the DB or the repacker.
---

# Avowed → Ukrainian localization

**Rule for every session:** do NOT re-investigate the archive/asset format — it is fully described below and implemented in
`tools/extract_to_sqlite.py` (reuse its `IoStore`, `Oodle`, `parse_text_asset`). Don't dump tables to the console (≤20 lines of stats per call). Use stdlib only (`sqlite3`, `ctypes`, `struct`, `mmap`).

## Project layout

```
.agent/skills/avowed-l10n/SKILL.md   this file
tools/extract_to_sqlite.py           extract 6 assets -> raw_assets/, parse -> db/avowed_l10n.db (idempotent)
raw_assets/<Source>_Text_<lang>.uasset   original decompressed Zen packages = templates for the repacker (git-ignored)
db/avowed_l10n.db                    SQLite, table `strings` (git-ignored: binary + game data; back up uk_* work separately)
```
`<Source>` ∈ {`BaseGame`, `D11`}; `<lang>` ∈ {`enus`, `plpl`, `ruru`}. The repacker (not written yet) takes `*_enus.uasset` (or plpl) as template, substitutes DefaultText/FemaleText/ExtraText with `uk_*`, rewrites sizes, and must keep the 12-byte footer valid.

Run: `python tools/extract_to_sqlite.py` (re-run = upsert: en/pl/ru refreshed, `uk_*`/`status` preserved; raw files reused if size matches; `--force-extract`, `--rebuild` (⚠ wipes uk_*), `--utoc`, `--oodle`, `--db`, `--raw-dir`).

## Game files

- Container: `M:\XboxGames\Avowed\Content\Alabama\Content\Paks\Alabama-WinGDK.utoc` + `.ucas` — IoStore **v5**, unencrypted, Oodle.
- Oodle DLL (works): `M:\SteamLibrary\steamapps\common\DOOMEternal\oo2core_8_win64.dll`, export `OodleLZ_Decompress` (14 args: comp, compLen, raw, rawLen, fuzzSafe=1, checkCRC=0, verbosity=0, NULL, 0, NULL, NULL, NULL, 0, threadPhase=3).

## .utoc layout (Alabama-WinGDK.utoc)

Header (144 B, LE): magic `-==--==--==--==-`@0; HeaderSize@20=144; EntryCount@24=430089; CompBlockCount@28=1985362; CompBlockEntrySize@32=12; MethodNameCount@36; MethodNameLen@40 (32); CompBlockSize@44=65536; DirIndexSize@48; PartitionCount@52; PerfectHashSeedsCount@84=215045; PartitionSize@88; ChunksWithoutPerfectHashCount@96.

After the header, in order:
1. `ChunkIds` — 12 B × EntryCount (from offset 144)
2. `ChunkOffsetLengths` — 10 B × EntryCount: **5 B big-endian offset + 5 B big-endian length** (offset in the uncompressed stream)
3. `PerfectHashSeeds` — 4 B × 215045 (+ 4 B × ChunksWithoutPerfectHash)
4. `CompressionBlocks` — 12 B × CompBlockCount: 5 B LE `c_off` (in .ucas) + 3 B LE `c_sz` + 3 B LE `u_sz` + 1 B `c_meth` (0 = raw, n = n-th name in the method-name table = Oodle)
5. compression method names (32 B each), …directory index

Read chunk `idx`: `off,len = ChunkOffsetLengths[idx]`; blocks `off//65536 … (off+len-1)//65536`; decompress each (`u_sz` bytes, Oodle) from `.ucas[c_off : c_off+c_sz]`, concatenate, cut `[off % 65536 : +len]`.

## Text assets (`UStringTableBundleSet`), chunk_idx = index into the arrays above

| Asset | chunk_idx | size (B) |
|---|---|---|
| BaseGame EN `Text_enus.uasset` | 4689 | 6 318 939 |
| BaseGame PL `Text_plpl.uasset` | 165773 | 11 551 609 (4 065 FemaleText) |
| BaseGame RU `Text_ruru.uasset` | 157656 | 10 390 581 |
| D11 EN `D11/Localized/enus/Text/Text_enus.uasset` | 373035 | 3 392 |
| D11 PL `D11/Localized/plpl/Text/Text_plpl.uasset` | 338689 | 5 909 |
| D11 RU `D11/Localized/ruru/Text/Text_ruru.uasset` | 390019 | 5 756 |

(Chunk indexes are tied to this game build; the script verifies utoc header values and asset sizes and aborts on mismatch.)

### Binary format of `Text_<lang>.uasset` (Zen package, all LE except where noted)

- Zen header: `header_size = u32@0x04`, `num_names = u32@0x34`. NameBatch starts at `0x44`: `num_names × 8 B` hashes, then `num_names × 2 B` **big-endian** headers (`len = val & 0x7FFF`, bit 15 = wide/UTF-16), then the name strings (UTF-8, no NUL). FName = `idx u32 (mask 0x3FFFFFFF) + num u32` (`num>0` → suffix `_{num-1}`). Table names are paths like `game/items`, `conversations/01_emerald_stair/...`.
- Export 0 starts at `pos = header_size`: `00 05` (2 B) + `rem_count` (4 B) + `table_count` (4 B).
- Each table (`FOEIStringTable`): `key_idx,key_num` (8 B) + `00 11` (2 B) + `name_idx,name_num` (8 B) + 3 × `TMap<FString,FIntSet>` + 3 × `TMap<int32,FNodeLinkArray>` + `Entries` map.
  - `TMap<FString,FIntSet>`: `rem,cnt` (8 B); per entry: `FString` + `00 03` + `s_rem,s_cnt` (8 B) + `s_cnt × 4 B`.
  - `TMap<int32,FNodeLinkArray>`: `rem,cnt` (8 B); per entry: `int32 key` + `00 03` + `arr_cnt` (4 B); per array element: `eb0,eb1` (2 B) + optional `zmask` (1 B, if `eb0 & 0x80`) + up to 5 × `int32` for bits 0..4 not set in zmask (usual element = `00 0B` + 20 B).
  - `Entries`: `ent_rem,entry_cnt` (8 B). Per `FOEIStringEntry`: `e_key` (int32) + `eb0,eb1` (2 B) + optional `e_zmask` (1 B, if `eb0 & 0x80`) + `e_id` int32 (if `!(e_zmask&1)`) + `DefaultText` FString (if `!(e_zmask&2)`) + `FemaleText` FString (if `!(e_zmask&4)`) + `ExtraText` FString (if `!(e_zmask&8)`).
- `FString`: `int32 n`; `n>0` → n bytes ANSI incl. NUL; `n<0` → `-n` UTF‑16LE chars incl. NUL; `0` → empty. (UTF-16 is needed for Ukrainian text.)
- The `eb0,eb1` pairs are UE *unversioned property headers* (u16 LE: bits0‑6 skip, bit7 has‑zero‑mask, bit8 last, bits9‑15 value count; zero‑mask bit set ⇒ property not serialized/empty). The parser implements them generally (`read_header`), which covers the spec above. Header counts: `00 05` = 2 props, `00 11` = 8 props, `00 03` = 1 prop.
- Footer, exactly 12 B: `Hash` (4 B) + `00 00 00 00` (4 B) + `c1 83 2a 9e` (4 B, package tag). After parsing, `pos == len - 12` (the script checks this; verified for all 6 assets).

## Facts found while parsing (verified)

- 865 tables in BaseGame + 2 in D11 (`game/achievements`, `game/gui`) = **867**; 63 325 + 26 = **63 351** strings; keys are identical across en/pl/ru (no orphans). `e_key` is unique per table and equals `e_id` except where `e_id` is zero-masked → `string_id = e_key` (signed int32).
- Female variants: en 190, pl 4 065, ru 804. **`ExtraText`** (3rd variant; e.g. neutral "their" line) is non-empty: en 190, pl 4 036, ru 753 → kept in `en_extra/pl_extra/ru_extra/uk_extra` (extra to the requested schema, so nothing is lost for the repacker).
- `has_gender_variant = 1` iff `pl_female != '' OR en_female != ''` (4 096 rows). Not covered by that rule: 179 rows with only `ru_female`, 171 rows with only an `*_extra` — consider widening if Ukrainian needs them.

## SQLite `db/avowed_l10n.db`, table `strings`

`id` (PK, insertion order = EN table/entry order), `source_file` (`BaseGame`|`D11`), `table_name`, `string_id` (int), `en_default`, `en_female`, `pl_default`, `pl_female`, `ru_default`, `ru_female`, `uk_default`, `uk_female`, `has_gender_variant`, `status` (default `new`), `category`, `en_extra`, `pl_extra`, `ru_extra`, `uk_extra`. Missing/empty text = `''`. `UNIQUE(source_file, table_name, string_id)`.
`category` = first path segment of `table_name` (`conversations`, `chatter`, `quests`) or `game/<x>` for `game/*` tables (`game/items`, `game/gui`, …) — 23 values.
`status` values (convention, free text): `new` → `draft` → `reviewed`.
