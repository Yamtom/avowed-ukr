export const meta = {
  name: 'translate-batches',
  description: 'EN->UK translation of exported batches by parallel sub-agents; every agent writes only through tools/batch_io.py import (auto-validation)',
  phases: [{ title: 'Translate', detail: 'one agent per batch, validation loop inside the agent' }],
}
const ROOT = 'D:/Xbox/avowed-ukr/.claude/worktrees/avowed-ukrainian-localization-20b324'
const catOf = (n) => (n.includes('D11') ? 'D11' : n.slice(4).replace('-', '/'))
const batches = args.batches || args.names.map(n => ({ batch: n, category: catOf(n) }))
const fix = !!args.fix
const HINTS = {
  'game/credits': 'CREDITS: translate ONLY job titles, departments, role names and generic credit phrases ("Lead Programmer", "Special Thanks"). Person names, company/studio names and trademarks stay UNCHANGED: for those rows answer {"id":<id>,"keep":1}.',
  'game/items': 'ITEMS: item names (short, noun phrases, keep Title/sentence case as the source), item types and descriptions/flavour text. Weapon/armour/consumable terms must follow the glossary consistently.',
  'game/characters': 'CHARACTERS: NPC/creature/role names. Personal names are transliterated by the rules in style_uk.md (use glossary entries exactly); descriptive roles/titles are translated, with uk_female/uk_extra where need says so.',
  'game/locations': 'LOCATIONS: place names - use glossary entries exactly; translate descriptive parts consistently with the glossary.',
  'game/waypoints': 'WAYPOINTS: place / fast-travel labels - glossary entries exactly.',
  'game/abilities': 'ABILITIES: ability/spell names and their descriptions - concise gameplay language, terms exactly as in the glossary.',
  'game/statuseffects': 'STATUS EFFECTS: short effect names and descriptions - glossary exactly.',
  'game/characterstats': 'STATS: attribute/stat names and their descriptions - glossary exactly, very concise.',
  'game/gui': 'UI: menu labels and settings - very short and idiomatic (see section 7 of the style guide), tooltips can be full sentences addressed to the player as "ти".',
  'game/inputbindings': 'INPUT BINDINGS: keyboard/mouse/gamepad button names - translate common names (Left Mouse Button, D-Pad Up, Left Trigger), keep brand/abbreviation names.',
  'game/tutorial': 'TUTORIAL: instructional text addressed to the player ("ти"), imperative mood; keep the input glyph tags exactly.',
  'game/lore': 'LORE: literary in-world texts - keep the tone and tags; names via glossary.',
  'game/oeiunreal': 'SYSTEM: engine/system strings (settings, errors, generic UI) - concise and neutral.',
}
const hint = (c) => HINTS[c] || ''
const model = args.model || 'haiku'
const SCHEMA = {
  type: 'object',
  properties: { batch: { type: 'string' }, rows: { type: 'number' }, ok: { type: 'number' }, still_failing: { type: 'number' }, note: { type: 'string' } },
  required: ['batch', 'ok', 'still_failing'],
}

const prompt = (b) => `You are a professional English->Ukrainian game localizer (RPG Avowed, world of Eora). Work ONLY on your batch.
Repo root: ${ROOT}  (always use absolute paths).

1. Read ${ROOT}/docs/style_uk.md - the binding style rules (address "ти", transliteration, gender variants uk_female/uk_extra, «», —, apostrophe U+0027, tags/variables/ICU untouched).
2. Read your batch ${ROOT}/logs/batches/in/${b.batch}.jsonl. Line 1 is a header {batch, category, rows, glossary}: "glossary" lists [en, uk, uk_female, uk_extra, note] - these Ukrainian equivalents are MANDATORY wherever the English term occurs (decline them as needed, keep the stem). Every other line is one row: id, en (source; if empty use pl), en_f/en_x (female / neutral English variants), pl/pl_f/pl_x (Polish: only to understand meaning and gender forms), ru (Russian hint, ignore its wording), need ("F" = uk_female required, "X" = uk_extra required). ${fix ? 'This is a FIX batch: rows also carry the previous attempt (uk/uf/ux) and "note" with the reason it was rejected - correct exactly that problem and keep what was fine.' : ''}Category "${b.category}" tells you the context. ${hint(b.category)} Keep labels short; keep the register of the source.
3. Write ${ROOT}/logs/batches/out/${b.batch}.jsonl with the Write tool (UTF-8, one JSON object per line, NO shell redirection): {"id":<id>,"uk":"...","uf":"...","ux":"..."} - "uf" only when need contains F, "ux" only when need contains X. For rows that are proper names / codes / brands that must stay unchanged use {"id":<id>,"keep":1}. JSON must be valid: escape " as \\" and line breaks as \\n (as \\r\\n if the source line breaks are \\r\\n). Include EVERY row of the batch exactly once. Never translate or change tags (<i>, </>, <img .../>, <imginputbind .../>, <StatsTitle> ...), variables ({Value}, {0}, {PlayerName} ...) or ICU constructs (only their text parts).
4. Import: run  python ${ROOT}/tools/batch_io.py import ${ROOT}/logs/batches/out/${b.batch}.jsonl . It validates and prints "FIX <id>: <reason>" lines for rejected rows. Put ONLY the corrected rows into ${ROOT}/logs/batches/out/${b.batch}_fix1.jsonl (then _fix2, _fix3) and import again. At most 3 rounds. If a row cannot pass, leave it.
5. NEVER write to the database any other way, never edit other files. Answer only with the JSON summary: batch, rows, ok (rows accepted in the end), still_failing, note (<=1 line).`

phase('Translate')
const res = await parallel(batches.map(b => () => agent(prompt(b), { label: b.batch, phase: 'Translate', model, schema: SCHEMA })))
const done = res.filter(Boolean)
return {
  agents: batches.length,
  returned: done.length,
  failed_agents: batches.filter((b, i) => !res[i]).map(b => b.batch),
  ok: done.reduce((s, r) => s + (r.ok || 0), 0),
  still_failing: done.reduce((s, r) => s + (r.still_failing || 0), 0),
  notes: done.filter(r => r.note).slice(0, 12).map(r => r.batch + ': ' + r.note),
}
