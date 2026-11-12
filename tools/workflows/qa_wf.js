export const meta = {
  name: 'qa-uk',
  description: 'QA of the Ukrainian translation: 2 editors review a stratified 2.7% sample; 1 agent resolves the glossary-audit worklist',
  phases: [{ title: 'QA', detail: 'sample reviews + glossary audit fixes, all via tools/batch_io.py' }],
}
const ROOT = 'D:/Xbox/avowed-ukr/.claude/worktrees/avowed-ukrainian-localization-20b324'
const R_SCHEMA = { type: 'object', properties: { batch: { type: 'string' }, reviewed: { type: 'number' }, ok: { type: 'number' }, fixed: { type: 'number' }, unfixed: { type: 'number' }, issues: { type: 'string' } }, required: ['batch', 'reviewed', 'ok'] }
const review = (q) => `You are a senior English->Ukrainian game-localization editor reviewing a translation of the RPG Avowed. Repo root: ${ROOT} (absolute paths only).
1. Read ${ROOT}/docs/style_uk.md (binding style rules).
2. Read ${ROOT}/logs/qa/in/${q}.jsonl. Line 1: header with "glossary" ([en, uk, uk_female, uk_extra, note] - mandatory equivalents). Other lines: a row with id, en (source), en_f/en_x, pl/pl_f/pl_x (Polish, for meaning and gender forms), ru (hint), need (F/X = female / neutral variant required) and the CURRENT translation uk / uf / ux.
3. Judge every row: faithful meaning (compare EN and PL), natural idiomatic Ukrainian (no russisms/polonisms/calques), glossary compliance, style rules (ти, «», —, apostrophe U+0027), correct feminine uf and neutral ux per the style guide, UI brevity, untouched tags/variables/ICU. Minor taste differences are NOT a reason to reject; real errors are (wrong meaning, wrong term, wrong gender form, stilted/unnatural phrasing, broken style rule).
4. Write ${ROOT}/logs/qa/out/${q}.jsonl with the Write tool (UTF-8, one JSON per line, valid JSON): {"id":<id>,"ok":true} for good rows; for bad rows {"id":<id>,"ok":false,"issue":"<short reason>","uk":"<corrected default>","uf":"<corrected female if the row has one>","ux":"<corrected neutral if the row has one>"} - always give the full corrected text of every variant the row has. One line per row, every row exactly once.
5. Run: python ${ROOT}/tools/batch_io.py review ${ROOT}/logs/qa/out/${q}.jsonl  (it re-validates your corrections; rows whose correction fails validation become needs_fix). Do not touch the database any other way or edit other files.
6. Answer with the JSON summary: batch, reviewed, ok, fixed (rejected with a correction), unfixed, issues (the up-to-3 most frequent recurring problems, one line).`

const audit = `You are the terminology editor of the Ukrainian localization of Avowed. Repo root: ${ROOT} (absolute paths only).
1. Read ${ROOT}/docs/style_uk.md.
2. Read ${ROOT}/logs/batches/aud/aud001_audit.jsonl. Line 1: header with "glossary". Other lines: rows flagged by an automatic glossary audit: "note" says "glossary: <English term> -> <approved Ukrainian>", "uk/uf/ux" is the current translation, en/pl/ru are the sources.
3. For each row decide: is it a REAL violation (the term is rendered differently / missing, in any case form)? Many flags are FALSE POSITIVES: the approved term is present in a declined form (Боєць/бійця, Мисливець/мисливця, Посланець/посланцю), the English word is a different homonym (e.g. "Snap" inside "Frost Snap" is an ability name, not the creature), or part of a longer name that the glossary lists separately. Only fix real violations; leave false positives alone (write no line for them).
4. Write the corrected rows to ${ROOT}/logs/batches/out/aud001_audit.jsonl (Write tool, UTF-8, JSON per line): {"id":<id>,"uk":"...","uf":"...","ux":"..."} - include uf/ux exactly when the row has them (need F/X or previous uf/ux), full text of each variant. Then run python ${ROOT}/tools/batch_io.py import ${ROOT}/logs/batches/out/aud001_audit.jsonl and fix any FIX lines in a second file aud001_audit_fix1.jsonl (max 3 rounds).
5. Answer with the JSON summary: batch="aud001_audit", reviewed (flagged rows), ok (false positives left untouched), fixed, unfixed, issues (one line).`

phase('QA')
const res = await parallel([
  () => agent(review('Q01'), { label: 'qa:Q01', phase: 'QA', model: 'sonnet', schema: R_SCHEMA }),
  () => agent(review('Q02'), { label: 'qa:Q02', phase: 'QA', model: 'sonnet', schema: R_SCHEMA }),
  () => agent(audit, { label: 'glossary-audit-fix', phase: 'QA', model: 'sonnet', schema: R_SCHEMA }),
])
return res.filter(Boolean)
