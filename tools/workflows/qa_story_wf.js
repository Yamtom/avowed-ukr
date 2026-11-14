export const meta = {
  name: 'qa-story',
  description: 'Semantic QA of story translations by strong-model editors on stratified samples (logs/qa/<dir>/in/Q*.jsonl); corrections go through tools/batch_io.py review',
  phases: [{ title: 'QA', detail: 'one editor per sample file' }],
}
// args: { dir: "p1", names: ["Q01","Q02"], model: "sonnet" }   (files: logs/qa/<dir>/in/<name>.jsonl, answers: logs/qa/<dir>/out/<name>.jsonl)
const ROOT = 'D:/Xbox/avowed-ukr/.claude/worktrees/avowed-ukrainian-localization-20b324'
const A = args
const SCHEMA = { type: 'object', properties: { batch: { type: 'string' }, reviewed: { type: 'number' }, ok: { type: 'number' }, fixed: { type: 'number' }, unfixed: { type: 'number' }, issues: { type: 'string' } }, required: ['batch', 'reviewed', 'ok'] }
const prompt = (q) => `You are a senior English->Ukrainian game-localization editor reviewing story text of the RPG Avowed (dialogues, quests, chatter barks, codex lore). Repo root: ${ROOT} (absolute paths only).
1. Read ${ROOT}/docs/style_uk.md (binding; sections 4 and 8 matter most: ти/ви, quests without "ви", gender/neutral variants of the Envoy, chatter, codex genres).
2. Read ${ROOT}/logs/qa/${A.dir}/in/${q}.jsonl. Line 1 = header: "glossary" ([en, uk, uk_female, uk_extra, note] mandatory terms) and "voices" (speaker profiles of these scenes). Other lines = one row each: id, t (table tail), en (source), en_f/en_x, pl/pl_f/pl_x (Polish, for meaning and gender), ru (hint only), need (F/X), prev = [EN, UK] of the preceding line of the same table, and the CURRENT translation uk / uf / ux; "note" may carry a soft flag ("flag:glossary:X", "flag:formal-you", "flag:rusism") - check exactly that, but a flag alone is not a defect.
3. Judge every row: faithful meaning (EN first, PL for disambiguation), natural spoken/literary Ukrainian (no russisms, no calques), consistency with prev and with the character voice, correct ти/ви, glossary terms, correct feminine uf and neutral ux about the Envoy (not another person), preserved tags/variables, brevity of short barks/quest titles. Taste differences are NOT defects; wrong meaning, wrong term, wrong address form, wrong gender form, stilted or russianised phrasing, broken voice are.
4. Write ${ROOT}/logs/qa/${A.dir}/out/${q}.jsonl (Write tool, UTF-8, valid JSON per line, every row exactly once): {"id":<id>,"ok":true} or {"id":<id>,"ok":false,"issue":"<short reason>","uk":"<full corrected default>","uf":"<corrected female if the row has one>","ux":"<corrected neutral if the row has one>"}.
5. Run: python ${ROOT}/tools/batch_io.py review ${ROOT}/logs/qa/${A.dir}/out/${q}.jsonl  (re-validates corrections; failed ones become needs_fix). Touch nothing else.
6. Answer ONLY with JSON: batch, reviewed, ok, fixed, unfixed, issues (<=3 most frequent recurring problems, one line - they become new style rules).`
phase('QA')
const res = await parallel(A.names.map(q => () => agent(prompt(q), { label: 'qa:' + q, phase: 'QA', model: A.model || 'sonnet', schema: SCHEMA })))
const done = res.filter(Boolean)
return {
  files: A.names.length, returned: done.length,
  reviewed: done.reduce((s, r) => s + (r.reviewed || 0), 0), ok: done.reduce((s, r) => s + (r.ok || 0), 0),
  fixed: done.reduce((s, r) => s + (r.fixed || 0), 0), unfixed: done.reduce((s, r) => s + (r.unfixed || 0), 0),
  issues: done.filter(r => r.issues).map(r => r.batch + ': ' + r.issues),
}
