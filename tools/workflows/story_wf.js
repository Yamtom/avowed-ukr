export const meta = {
  name: 'story-translate',
  description: 'Queue workers: each agent claims batches from the DB queue (tools/batch_worker.py), translates EN->UK, imports through tools/batch_io.py and fixes rejected rows',
  phases: [{ title: 'Translate', detail: 'N parallel workers, each does up to K claim->translate->import cycles' }],
}
// args: { groups: "quests,chatter", workers: 8, batchesPer: 3, model: "haiku", tag: "q", fix: false, tablePrefix: "", maxAttempts: 0 }
const ROOT = 'D:/Xbox/avowed-ukr'
const A = args
const OUT = A.out || 'D:/Xbox/avowed-ukr/.claude/worktrees/avowed-l10n-repacker-d811ff/scratch/batches/out'
const model = A.model || 'haiku'
const per = A.batchesPer || 3
const SCHEMA = {
  type: 'object',
  properties: { worker: { type: 'string' }, batches: { type: 'number' }, rows: { type: 'number' }, ok: { type: 'number' }, needs_fix: { type: 'number' }, note: { type: 'string' } },
  required: ['worker', 'batches', 'ok'],
}
const flags = `${A.fix ? ' --fix' : ''}${A.tablePrefix ? ' --table-prefix ' + A.tablePrefix : ''}${A.maxAttempts ? ' --max-attempts ' + A.maxAttempts : ''} --max-rows ${A.maxRows || 150} --max-chars ${A.maxChars || 18000}`

const prompt = (w) => `You are a professional English->Ukrainian game localizer (RPG Avowed, world of Eora). Worker id: ${w}. Repo root: ${ROOT} (absolute paths only). You work through a DB-backed queue; never read or write the database any other way.

First read D:/Xbox/avowed-ukr/.claude/worktrees/avowed-l10n-repacker-d811ff/docs/style_uk.md - the binding style guide (read section 8 for dialogue / quests / chatter / codex and the gender rules in section 4).

${A.voices ? 'Also read ' + A.voices + ' - extra voice profiles of local NPCs (each applies to tables whose name contains its "match:" string; some attributions are inferred, the batch text wins on conflict).' : ''}
Then repeat the following cycle up to ${per} times:
1. Claim: run  python ${ROOT}/tools/batch_worker.py claim --worker ${w} --groups ${A.groups}${flags}   -> prints ONE json line. If "batch" is null the queue is empty: stop and answer. Otherwise it gives "file" (batch to read) and the batch id. IGNORE the printed "out" path (a hook blocks the Write tool there): your answer file ("out file" below) is ${OUT}/batch_<batch id>.jsonl - write it with the Write tool, never with a script.
2. Read the batch file (JSON). Fields: "glossary" = mandatory Ukrainian equivalents [en, uk, uk_female, uk_extra, note] (decline them, keep the stem); "voices" = speaker profiles for these scenes - keep each character's voice and the ти/ви forms consistent; "ctx" = READ-ONLY lines that precede the first row of each table ({table index: [[en, uk-if-already-translated], ...]}) for continuity only, never translate them; "rows" = what you translate: id, en (source - if empty use pl), en_f / en_x (female / neutral English variants), pl / pl_f / pl_x (Polish: use only to resolve meaning and gender), ru (hint only, never copy its wording), need ("F" = uk_female required, "X" = uk_extra required), t (table index when the batch has several tables). Rows are in dialogue order within a table. ${A.fix ? 'This is a FIX batch: rows also carry the previous attempt uk/uf/ux and "note" = why it was rejected or flagged - correct exactly that problem and keep what was right.' : ''}
3. Translate EVERY row. Write the "out" file with the Write tool (UTF-8, one JSON object per line, valid JSON, no shell redirection): {"id":<id>,"uk":"...","uf":"...","ux":"..."} - "uf" only when need contains F, "ux" only when need contains X (FIX batches: include uf/ux whenever the previous attempt had them). For proper names, codes or sounds that must stay unchanged answer {"id":<id>,"keep":1}. Escape " as \\" and line breaks as \\n (\\r\\n when the source uses \\r\\n). Never change tags (<i>, </>, <img .../>, <imginputbind .../> ...), {variables} or ICU |plural(...) constructs - translate only the text around/inside them. Female / neutral variants are about the player character (the Envoy): see style guide section 4 and 8. MOST FREQUENT QA DEFECT: when "need" is empty the row has ONE text for every Envoy, so never put a gendered past-tense verb or adjective on the Envoy (as speaker "я прийшов / я певен" or as addressee "ти зробив / ти готовий") - rephrase without gender (present tense, impersonal, noun phrase: "мені вдалося", "тобі до снаги", "ти вже тут"); in ux use plural or "Посланцю/Посланнице", never "зробив(ла)". Do not invent a gender for unnamed NPCs either (follow pl). An NPC keeps one form of address (ти or ви) to the Envoy through the scene. Second most frequent: russisms and calques - write "спробуй / намагайся" (not "постарайся"), "радше / швидше" (not "скоріше"), "понад / більше ніж" (not "більш ніж"), "зробімо / ходімо" (not "давай(те) зробимо"), "треба / мусимо" (not "маємо" + infinitive), "що відбувається" (not "що діється"), no participles in -ючий/-ачий; keep every vocative ("Envoy" -> "Посланцю") and every piece of meaning when you rephrase. The MEANING always comes from "en" (pl often rewords or drops things - use it only for gender and ти/ви); keep the logic of "unless / but for / as long as" exactly. Female speakers (pl feminine forms, e.g. Lödwyn, Ryngrim) speak in the feminine. Natural spoken Ukrainian for dialogue, no russisms, no calques from English/Polish.
4. Import: python ${ROOT}/tools/batch_io.py import <the out file>. It prints "FIX <id>: <reason>" for rejected rows (they are already stored as needs_fix). Put ONLY the corrected rows into <out file without .jsonl>_fix1.jsonl (then _fix2, _fix3; keep the batch_<id> prefix of the name) and import again. At most 3 rounds.
5. Release leftovers: python ${ROOT}/tools/batch_worker.py done --batch <batch id>.
Then start the next cycle.

Answer ONLY with the JSON summary: worker, batches (cycles done), rows (rows claimed), ok (rows accepted), needs_fix (rows still rejected), note (<=1 line, mention any tool error).`

phase('Translate')
const ws = Array.from({ length: A.workers }, (_, i) => `${A.tag || 'w'}${String(i + 1).padStart(2, '0')}`)
const res = await parallel(ws.map(w => () => agent(prompt(w), { label: w, phase: 'Translate', model, schema: SCHEMA })))
const done = res.filter(Boolean)
return {
  workers: ws.length,
  returned: done.length,
  failed: ws.filter((w, i) => !res[i]),
  batches: done.reduce((s, r) => s + (r.batches || 0), 0),
  rows: done.reduce((s, r) => s + (r.rows || 0), 0),
  ok: done.reduce((s, r) => s + (r.ok || 0), 0),
  needs_fix: done.reduce((s, r) => s + (r.needs_fix || 0), 0),
  notes: done.filter(r => r.note).slice(0, 8).map(r => r.worker + ': ' + r.note),
}
