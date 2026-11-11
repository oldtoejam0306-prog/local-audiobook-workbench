/** Offline, bounded recovery for recognized premature character-profile delimiters. */
import {createHash} from 'node:crypto';
import {lstat, readFile, writeFile} from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const sha = raw => createHash('sha256').update(raw).digest('hex');
const ROOT_KEYS = new Set(['characters', 'relations', 'narration_design', 'scope_summary']);
const CHARACTER_KEYS = new Set(['name', 'name_evidence', 'aliases', 'identity', 'gender', 'age',
  'personality', 'behavior', 'speech_style', 'voice_design', 'uncertainties']);
const NEXT_FIELDS = new Set(['aliases', 'identity', 'gender', 'age', 'personality', 'behavior',
  'speech_style', 'voice_design', 'uncertainties']);

function tokens(text) {
  const out = [];
  for (let i = 0; i < text.length;) {
    const ch = text[i];
    if (/\s/.test(ch)) { i++; continue; }
    if ('{}[]:,'.includes(ch)) { out.push({type: ch, start: i, end: i + 1}); i++; continue; }
    if (ch === '"') {
      const start = i++;
      let escaped = false;
      for (; i < text.length; i++) {
        if (escaped) { escaped = false; continue; }
        if (text[i] === '\\') { escaped = true; continue; }
        if (text[i] === '"') { i++; break; }
      }
      if (text[i - 1] !== '"') throw Error('unterminated_json_string');
      const raw = text.slice(start, i);
      let value;
      try { value = JSON.parse(raw); } catch { throw Error('invalid_json_string'); }
      out.push({type: 'string', value, start, end: i});
      continue;
    }
    const start = i;
    while (i < text.length && !/\s/.test(text[i]) && !'{}[]:,'.includes(text[i])) i++;
    if (i === start) throw Error('invalid_json_token');
    out.push({type: 'atom', value: text.slice(start, i), start, end: i});
  }
  return out;
}

function matchingArrayEnd(list, start) {
  let depth = 0;
  for (let i = start; i < list.length; i++) {
    if (list[i].type === '[') depth++;
    else if (list[i].type === ']' && --depth === 0) return i;
  }
  return -1;
}

function strictShape(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
      || Object.keys(value).some(key => !ROOT_KEYS.has(key))
      || !Array.isArray(value.characters) || value.characters.length < 1 || value.characters.length > 100
      || typeof value.scope_summary !== 'string') throw Error('recovered_profile_root_schema_invalid');
  for (const person of value.characters) {
    if (!person || typeof person !== 'object' || Array.isArray(person)
        || Object.keys(person).some(key => !CHARACTER_KEYS.has(key))
        || typeof person.name !== 'string' || !person.name.trim()) throw Error('recovered_profile_character_schema_invalid');
  }
  if (value.relations !== undefined && !Array.isArray(value.relations)) throw Error('recovered_profile_relations_schema_invalid');
}

/** Remove only a unique `}]` immediately after name_evidence and before another profile field. */
export function repairMalformedProfileJson(text) {
  let syntaxError = null;
  try {
    const parsed = JSON.parse(text);
    strictShape(parsed);
    return {text, value: parsed, repair: null};
  } catch (originalError) {
    try { JSON.parse(text); }
    catch (parseError) { syntaxError = parseError; }
    // A valid JSON value with an invalid schema is never a repair candidate.
    if (!syntaxError) throw originalError;
  }
  const marker = '},"scope_summary":';
  const rootClose = text.indexOf(marker);
  if (rootClose >= 0 && rootClose === text.lastIndexOf(marker)) {
    try {
      const first = JSON.parse(text.slice(0, rootClose + 1));
      const last = JSON.parse('{' + text.slice(rootClose + 2));
      if (first && typeof first === 'object' && !Array.isArray(first)
          && Object.keys(first).every(key => ROOT_KEYS.has(key) && key !== 'scope_summary')
          && Object.keys(last).join(',') === 'scope_summary' && typeof last.scope_summary === 'string') {
        const repaired = text.slice(0, rootClose) + text.slice(rootClose + 1);
        const value = JSON.parse(repaired);
        strictShape(value);
        return {text: repaired, value, repair: {kind: 'remove_premature_scope_summary_root_closer',
          removals: [{character_offset: rootClose, removed: '}', next_field: 'scope_summary'}]}};
      }
    } catch {
      // The character-boundary recovery below handles remaining recognized syntax.
    }
  }
  let repairedText = text;
  const originMap = Array.from({length: text.length}, (_unused, index) => index);
  const removed = [];
  while (removed.length < 8) {
    const list = tokens(repairedText), candidates = [];
    for (let i = 0; i < list.length; i++) {
      if (list[i].type !== 'string' || list[i].value !== 'name_evidence'
          || list[i + 1]?.type !== ':' || list[i + 2]?.type !== '[') continue;
      const evidenceEnd = matchingArrayEnd(list, i + 2);
      if (evidenceEnd < 0 || list[evidenceEnd + 1]?.type !== '}' || list[evidenceEnd + 2]?.type !== ']'
          || list[evidenceEnd + 3]?.type !== ',' || list[evidenceEnd + 4]?.type !== 'string'
          || !NEXT_FIELDS.has(list[evidenceEnd + 4].value) || list[evidenceEnd + 5]?.type !== ':') continue;
      candidates.push({start: list[evidenceEnd + 1].start, end: list[evidenceEnd + 2].end,
        next_field: list[evidenceEnd + 4].value});
    }
    if (!candidates.length) throw syntaxError;
    if (removed.length + candidates.length > 8) throw Error('too_many_profile_json_repairs');
    for (const candidate of candidates.slice().sort((a, b) => b.start - a.start)) {
      const start = originMap[candidate.start], end = originMap[candidate.end - 1] + 1;
      removed.push({character_offset: start, removed: '}]', next_field: candidate.next_field});
      repairedText = repairedText.slice(0, candidate.start) + repairedText.slice(candidate.end);
      originMap.splice(candidate.start, candidate.end - candidate.start);
    }
    try {
      const value = JSON.parse(repairedText);
      strictShape(value);
      if (!removed.length) throw syntaxError;
      return {text: repairedText, value, repair: {kind: 'remove_premature_name_evidence_closers',
        removals: removed.sort((a, b) => a.character_offset - b.character_offset)}};
    } catch (error) {
      // Continue only while another exact, recognized boundary remains.
      try { JSON.parse(repairedText); throw error; } catch (parseError) {
        if (parseError === error) throw error;
        syntaxError = parseError;
      }
    }
  }
  throw Error('too_many_profile_json_repairs');
}

async function readRegular(file) {
  const info = await lstat(file, {bigint: true});
  if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1n) throw Error('recovery_input_not_regular_single_link');
  return readFile(file);
}

async function writeExclusive(file, raw) {
  await writeFile(file, raw, {flag: 'wx'});
}

/** Recover one frozen batch without network access, credentials, or original-file replacement. */
export async function recoverProfileBatch(batchDir) {
  const dir = path.resolve(batchDir);
  const receipt = path.join(dir, 'receipt', 'character_profiles_v1');
  const inputPath = path.join(dir, 'input.json');
  const protocolPath = path.join(dir, 'protocol-source.mjs');
  const helperArchive = path.join(dir, 'profile-json-recovery-source.mjs');
  const bodyPath = path.join(receipt, 'response.body');
  const metaPath = path.join(receipt, 'response.meta.json');
  const intentPath = path.join(receipt, 'intent.json');
  const usagePath = path.join(receipt, 'usage.json');
  const originalResultPath = path.join(receipt, 'result.json');
  const recoveredResultPath = path.join(receipt, 'result.local-format-recovered.json');
  const sidecarPath = path.join(receipt, 'local-format-recovery.json');
  for (const file of [inputPath, protocolPath, bodyPath, metaPath, intentPath, usagePath])
    if (path.isAbsolute(file) && !file.startsWith(dir + path.sep)) throw Error('recovery_path_escape');
  const [inputRaw, protocolRaw, bodyRaw, metaRaw, intentRaw, usageRaw] = await Promise.all(
    [inputPath, protocolPath, bodyPath, metaPath, intentPath, usagePath].map(readRegular));
  const originalResultRaw = await readRegular(originalResultPath).catch(error => {
    if (error.code === 'ENOENT') return null;
    throw error;
  });
  const input = JSON.parse(inputRaw), meta = JSON.parse(metaRaw), intent = JSON.parse(intentRaw), usageFile = JSON.parse(usageRaw);
  const provider = JSON.parse(bodyRaw);
  if (intent.purpose !== 'character_profiles_v1' || intent.input_sha256 !== sha(inputRaw)
      || intent.protocol_source_sha256 !== sha(protocolRaw) || meta.http_status !== 200
      || meta.body_sha256 !== sha(bodyRaw) || !provider || !Array.isArray(provider.choices)
      || !provider.choices[0]?.message || typeof provider.choices[0].message.content !== 'string'
      || typeof provider.id !== 'string' || !provider.id || !provider.usage
      || JSON.stringify(usageFile.usage) !== JSON.stringify(provider.usage)) throw Error('recovery_receipt_binding_invalid');
  if (provider.choices[0].finish_reason !== 'stop') throw Error('recovery_provider_finish_reason_invalid');
  for (const key of ['prompt_tokens', 'completion_tokens'])
    if (!Number.isSafeInteger(provider.usage[key]) || provider.usage[key] < 0) throw Error('recovery_provider_usage_invalid');
  if (await Promise.all([sidecarPath, recoveredResultPath, helperArchive].map(async file => {
      try { await lstat(file); return true; } catch (error) { if (error.code === 'ENOENT') return false; throw error; }
    })).then(values => values.some(Boolean))) throw Error('recovery_artifact_already_exists');
  const repaired = repairMalformedProfileJson(provider.choices[0].message.content);
  if (!repaired.repair) throw Error('profile_response_does_not_need_local_repair');
  const protocolUrl = pathToFileURL(protocolPath).href + '?recovery=' + sha(protocolRaw);
  const protocol = await import(protocolUrl);
  const parsed = protocol.parseProfileContent(repaired.text).value;
  const normalized = protocol.normalizeProfileResult(parsed, input);
  const result = {status: 'needs_review', retry_allowed: false, response_id: provider.id,
    usage: provider.usage, result: normalized};
  const recoveredRaw = Buffer.from(JSON.stringify(result), 'utf8');
  const helperRaw = await readFile(new URL(import.meta.url));
  await writeExclusive(helperArchive, helperRaw);
  await writeExclusive(recoveredResultPath, recoveredRaw);
  const sidecar = {version: 1, kind: 'offline_profile_json_boundary_recovery',
    input_sha256: sha(inputRaw), response_body_sha256: sha(bodyRaw), response_meta_sha256: sha(metaRaw),
    original_result_sha256: originalResultRaw ? sha(originalResultRaw) : null,
    protocol_source_sha256: sha(protocolRaw), helper_source_sha256: sha(helperRaw),
    recovered_result_sha256: sha(recoveredRaw), response_id: provider.id, usage: provider.usage,
    repair: repaired.repair};
  await writeExclusive(sidecarPath, Buffer.from(JSON.stringify(sidecar), 'utf8'));
  return {status: 'recovered_offline', sidecar: sidecarPath, recovered_result: recoveredResultPath,
    repair: repaired.repair, input_sha256: sidecar.input_sha256,
    response_body_sha256: sidecar.response_body_sha256, response_id: provider.id, usage: provider.usage};
}

if (process.argv[2] === '--recover') {
  try { process.stdout.write(JSON.stringify(await recoverProfileBatch(process.argv[3])) + '\n'); }
  catch (error) { process.stderr.write(JSON.stringify({status: 'recovery_failed', reason: error.message}) + '\n'); process.exitCode = 2; }
}
