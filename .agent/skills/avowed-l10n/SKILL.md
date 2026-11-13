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
tools/repack_uasset.py               DB -> Text_<lang>.uasset, byte-exact (`roundtrip` test)
tools/build_iostore_patch.py         DB -> build/Alabama-WinGDK_Ukr_P.{pak,utoc,ucas} (+ `--install`), `selftest`
tools/blake3_py.py                   stdlib BLAKE3 (ChunkMetas)
tools/smoke_translate.py             20 main-menu smoke translations (game/gui)
raw_assets/<Source>_Text_<lang>.uasset   original decompressed Zen packages = templates for the repacker (git-ignored)
db/avowed_l10n.db                    SQLite, tables `strings`, `glossary` (git-ignored: binary + game data; back up uk_* work separately)
tools/batch_io.py                    batch export / validated import / stats / QA / glossary (see "Translation pipeline")
tools/workflows/*.js                 Workflow scripts for parallel translation (translate_wf) and QA (qa_wf)
docs/style_uk.md, docs/glossary_uk.md   style rules and the approved Ukrainian glossary (generated from table `glossary`: `kind` = entity type, `note` = translation rule — the only field sent to translators, `lore` = what it means in Eora, `source` = where checked; `migrate` adds lore/source to an existing DB)
```
`<Source>` ∈ {`BaseGame`, `D11`}; `<lang>` ∈ {`enus`, `plpl`, `ruru`}. The repacker takes `*_plpl.uasset` as template, substitutes DefaultText/FemaleText/ExtraText with `uk_*` (fallback English), fixes CookedSerialSize and keeps the 12-byte footer.

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

## Repacker (`tools/repack_uasset.py`) — byte-exact, verified

`python tools/repack_uasset.py roundtrip` rebuilds all 6 raw assets from the DB (en/pl/ru columns incl. `*_extra`) and requires **byte equality** with `raw_assets/` (passes for all 6; also checks every NodeLinks span is in range). `build --source BaseGame --template plpl --text uk --fallback en --out f` builds one asset.
Rules learned from the originals (all enforced/verified by the writer):
- Zen header is copied verbatim; only `ExportMap[0].CookedSerialSize` (u64 at `ExportMapOffset+8`; `ExportMapOffset` = u32@0x20, `ExportBundleEntriesOffset` = u32@0x24, 72-byte entries, exactly 1 export) is rewritten. **`CookedSerialSize = len(file) - header_size - 4`** (the 4-byte package tag is excluded). `CookedSerialOffset` = 0. That u64 occurs once in the header.
- Every `FOEIStringEntry` has the canonical header `u16 = (4<<9)|(1<<8)|(0x80 if zmask)`; **all three strings are always serialized** (DefaultText, FemaleText, ExtraText) — an empty string is `00 00 00 00`, never masked. Only a zero `e_id` is masked (`zmask = 1`, header `80 09 01`, no id bytes). Non-ASCII → UTF‑16LE `-(units+1)` + `00 00`; pure ASCII → ANSI `len+1` + `00`. Template property metadata (names, 3+3 maps) is copied byte for byte.
- The 12-byte trailer is copied from the template. Its 4-byte `Hash` differs per language and is NOT CRC32/Adler/FNV/Murmur/UE-MemCrc32 of any obvious span (tried); unknown whether the game validates it — kept from the template (PL).
- **NodeLinks (props 4/5/6 = `TMap<int32,FNodeLinkArray>`) are per-language character spans**, not metadata to copy: element = 5 int32 properties `(string key, node id, start, length, 0)`, serialized as header `80 0b` + zmask byte (zero values masked) + the non-zero ints; array = `00 03` + `i32 count`; map = `i32 0, i32 count` + `(i32 key, array)…`. Map 4/5/6 refer to the Default/Female/Extra text of that key; 100 % of the spans in en/pl/ru (15 365 / 19 953 / 16 244) lie inside their text. `rebuild_asset` therefore composes them per string from the *source of the emitted text*: template language → template links; other game language → that language's links (from `raw_assets`); Ukrainian text → **no links** (a PL span on Ukrainian text would be wrong; absent links are valid). Emitting a Polish span over English/Ukrainian text was a real bug found by review. Props 1–3 (`FString→FIntSet`, keyed `enus`) are language independent and copied. Link map key order is arbitrary (TMap): PL-template + EN-text reproduces the original EN asset with identical maps 1–3 and Entries bytes (865/865 tables), links equal as dicts, only key order and 36 header-name bytes differ.
- Resolver protocol: `resolver(table, key, orig) -> (default, female, extra, source)`, source ∈ `template|en|pl|ru|uk`. `build_asset()` re-reads the result strictly and asserts entries, CookedSerialSize, header/trailer, and that every NodeLinks span is inside its text.
- `--fallback en` (default): rows without `uk_default` use the whole English trio (default/female/extra) so untranslated text is English, not Polish. `template` keeps the template language (Polish) text — minimal-diff smoke builds use this. Future work: glossary spans for Ukrainian text (links are currently dropped for `uk` strings).

## IoStore patch mod (`tools/build_iostore_patch.py`)

`python tools/build_iostore_patch.py selftest` (read-only, validates all format code against the ORIGINAL container) and `... build [--install] [--lang plpl|ruru|enus] [--fallback en|pl|ru|template]` → `build/Alabama-WinGDK_Ukr_P.{pak,utoc,ucas}`; `--install` copies them to `M:\XboxGames\Avowed\Content\Alabama\Content\Paks`. Remove those 3 files there to uninstall.
Facts (verified on the original game files):
- utoc v5 (PerfectHashWithOverflow), original ContainerFlags 0x9 = Compressed|Indexed, PartitionSize 0xFFFF…, header reserved bytes 100..143 zero, method-name length 32, **after the compression-method names and the directory index come `ChunkMetas`: 33 B/chunk = BLAKE3 (first 20 B) of the *uncompressed* chunk + 12 zero bytes + 1 flag byte (1 = compressed)**. `tools/blake3_py.py` is a pure-Python BLAKE3 validated against the originals.
- ChunkId = 12 B: `u64 LE id` + `00 00` + `00` + `u8 type`; ExportBundleData = type 1 (id = package id), ContainerHeader = type 6 (id = ContainerId `0x5f8c90ad76795054`; original chunk idx 211452, 16 MB, decompressed copy is not stored).
- **Perfect hash**: `H(seed, id12)`: `h = seed ? seed : 0xcbf29ce484222325; for b in id: h = (h*0x100000001b3 mod 2^64) ^ b`. Lookup: `seed = seeds[H(0,id) % SeedCount]`; `seed==0` → absent; `seed<0` → slot `-seed-1`; else slot `H(seed,id) % N`; the chunk's array index IS the slot (ChunkIds/OffsetLengths/metas are stored in slot order). SeedCount = ceil(N/2). Verified for all 430089 original chunks. Builder: bucket by `H(0,id) % S`, multi-item buckets get a positive seed (search) in descending bucket size, single-item buckets get `-slot-1`.
- Patch container: 3 chunks [D11 text pkg, BaseGame text pkg, ContainerHeader], uncompressed 64 KiB blocks (method 0), chunk virtual offsets aligned to 65536, blocks 16-byte aligned in `.ucas`, ContainerFlags 0 (no directory index), ContainerId = first 8 B (LE) of sha256(name).
- **ContainerHeader v3** layout: `u32 0x496F436E, u32 3, u64 containerId, i32 n + n×u64 PackageIds (ascending), i32 size + StoreEntries blob, i32 0, i32 0 (optional segments), name batch (i32 num, i32 strBytes, u64 hashVersion, num×u64 hashes, num×u16 BE headers, strings), i32 localizedCount + 16 B each (u64 srcPkg + FMappedName), i32 redirectCount(0)`. StoreEntries blob = n×16-byte fixed entries `{u32 importedNum, u32 offsetFromView; u32 shaderNum, u32 offsetFromView}` (offsets are relative to the address of each view; 0 when empty) followed by the data (imported u64 ids, then 20-byte shader hashes, entry by entry). Parse→serialize of the original 16 MB header is byte-identical. The patch header carries only our 2 packages with their ORIGINAL store entries (copied), the original dummy name entry, no localized packages.
- **.pak is required**: UE mounts a `.utoc` only via a same-named `.pak`. Original `Alabama-WinGDK.pak`: v11, unencrypted, footer 221 B (`guid16, enc1, magic 0x5A6F12E1, ver 11, indexOff i64, indexSize i64, sha1(index) 20, 5×32 B compression names`).
  **A v10+ pak MUST declare a path-hash index and/or a full-directory index** — an index with neither (my first attempt: 42-byte index, both flags 0) makes `FPakFile::LoadIndexInternal` return false ("Corrupt pak PrimaryIndex") and `LoadIndex` then logs `Fatal` → game crash at startup (confirmed in UE 5.0.3/5.3/5.5 sources; retoc/repak/UnrealReZen empty paks have both). Our pak = primary index (114 B at offset 0: mount `"../../../"`, 0 entries, seed 0, `hasPHI=1 + off/size/sha1`, `hasFDI=1 + off/size/sha1`, 0 encoded bytes, 0 files) + PHI blob (8 B: `i32 0` path-hash map + `i32 0` pruned dir index) + FDI blob (4 B: `i32 0`) + the 221-byte footer = **347 B**. Footer IndexOffset/Size/SHA1 cover the primary index only; PHI/FDI offsets are absolute. `parse_pak_index` (used by `verify_pak` and by `selftest` on the ORIGINAL pak) parses to the exact byte; the original's PHI blob = `i32 count + count×(u64,i32)` + pruned DirectoryIndex (`TMap<FString,TMap<FString,i32>>`), FDI blob = DirectoryIndex.
- Review (5 independent reviewers + refuters, against UE 5.0.3/5.1/5.3/5.5 mirrors, retoc, CUE4Parse, repak) found nothing wrong in the TOC/ucas writer, perfect hash (3 chunks/2 seeds), ContainerHeader v3, ChunkMetas, mount order (`_P` ⇒ PakOrder 104 vs 4 for the base, applied to the utoc too; readers sorted by descending order), signing (none), fonts (Cyrillic incl. Ґ Є І Ї renders in the Polish slot via DanteeText/M+/Segoe UI; U+02BC and № are missing in EspinosaNova — avoid them). Remaining deviations from the proven recipes: container has no `Indexed` flag/directory index (runtime does not read it), `ContainerId` is sha256-derived (engine only uses it for the header chunk id).
- Still NOT verifiable without launching the game: whether Avowed's engine fork accepts all of this, whether the Xbox app keeps/removes extra files in `Content\` on update/repair, whether the game validates the unknown 4-byte `Hash`. Community mods use `...\Paks\~mods` (order 103); the Paks root works too (order 104). Polish plural rules (one/few/many with `one` only for n=1) apply in the plpl slot: Ukrainian plural strings with 21/31/101 will pick the wrong form (7 strings).

## Translation pipeline (`tools/batch_io.py`) — scope, statuses, workflow

Scope = `D11` (all) + `game/*` except `game/codex` (9 337 rows; conversations/chatter/quests/codex are NOT translated yet and fall back to English in the patch). Docs: `docs/style_uk.md` (binding style: «ти», transliteration h→г g→ґ, apostrophe U+0027, «», —, gender/neutral variants, tags/vars/ICU untouched, Polish plural categories one/few/many) and `docs/glossary_uk.md` (generated from table `glossary`, ~1 050 terms, 13 mandated ones are never overwritten).
- Variants: `uk_default` = masculine/base, `uk_female` = feminine (required when ANY of en/pl/ru_female exists), `uk_extra` = neutral they/them (required when ANY of en/pl/ru_extra exists; 3rd person → plural, 2nd person → rephrase, else «зробив/зробила»). `has_gender_variant` = female variant in any language (ExtraText is NOT counted: it almost always accompanies a female variant, 1 row has Extra without Female, 38 have Female without Extra).
- Status: `new` → `draft` (imported + auto-checks) → `reviewed` (QA ok) | `needs_fix` (reason in `qa_note`) | `skipped` (empty English source: other languages hold only `[plpl N - gui]` debug placeholders). `qa_note`: `glossary` = filled straight from a glossary term, `kept as source` = names/codes (`{"id":..,"keep":1}`).
- All writes by translators go through `python tools/batch_io.py import FILE.jsonl` (validates tags, `{vars}`, ICU `|plural(one=..,few=..,many=..)`, ы э ъ ё, Polish letters, quotes, line breaks, required uf/ux; normalizes apostrophes → `'`, ` - ` → ` — `; writes under `BEGIN IMMEDIATE` + busy_timeout so parallel agents never collide). Other commands: `migrate, export [--fix|--ids-file], validate, stats, sample, review, candidates, glossary-import|-md, apply-glossary, audit-glossary`. Batches: `logs/batches/in|out`, QA: `logs/qa`, per-import failures `logs/import/*.fail.txt` (git-ignored).
- Parallel translation = `Workflow` with `tools/workflows/translate_wf.js` (args `{model:"haiku", names:["003_game-gui",…]}`, one agent per exported batch, ≤3 fix rounds inside the agent) and QA = `tools/workflows/qa_wf.js` (sonnet editors on `logs/qa/in/Q*.jsonl`). Cheap-model bulk + `sonnet` for glossary and QA worked well; the tool-calls budget is small (≈100 s per 260-row batch).
- Session-3 result: 9 321 of 9 321 translatable rows translated; QA sample of 234 rows (2.7 %) found 29 real defects (12.4 %: glossary misses, calques/russisms, meaning errors, stilted lore) — all fixed; the audit found 62 more glossary violations in 154 flags (fixed). Extrapolated ≈10 % of the unreviewed rows probably still carry a real defect → a wider editing pass (`sample --pct 100` per category) is the next quality step.
- Pitfalls: PowerShell pipes stdin to Python in cp1251 — never put Cyrillic literals into `python -I -` scripts, write a UTF-8 file with the Write tool instead; `python -I` ignores PYTHONIOENCODING (use `sys.stdout.reconfigure`). Avoid `//` and quoted `\n` inside `python -c` next to `Remove-Item` (the shell's path-safety parser misreads them).

## SQLite `db/avowed_l10n.db`, table `strings`

`id` (PK, insertion order = EN table/entry order), `source_file` (`BaseGame`|`D11`), `table_name`, `string_id` (int), `en_default`, `en_female`, `pl_default`, `pl_female`, `ru_default`, `ru_female`, `uk_default`, `uk_female`, `has_gender_variant`, `status` (default `new`), `category`, `en_extra`, `pl_extra`, `ru_extra`, `uk_extra`. Missing/empty text = `''`. `UNIQUE(source_file, table_name, string_id)`.
`category` = first path segment of `table_name` (`conversations`, `chatter`, `quests`) or `game/<x>` for `game/*` tables (`game/items`, `game/gui`, …) — 23 values.
`status` values (convention, free text): `new` → `draft` → `reviewed`.
