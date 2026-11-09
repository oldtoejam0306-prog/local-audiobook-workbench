import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {mkdtemp,mkdir,readFile,writeFile,rm} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {buildProfileRequest,normalizeProfileResult,parseProfileContent} from './character_profile_protocol.mjs';
import {build,run} from './real_pipeline_context_worker.mjs';

const hash=s=>createHash('sha256').update(s,'utf8').digest('hex');
const names=['甲','乙','丙','丁','戊'];
test('preserves all values when only the final scope field is outside the root',()=>{
 const first={characters:[{name:'甲'}],narration_design:{description:'自然叙述'}};
 const malformed=JSON.stringify(first)+',"scope_summary":"仅本次章节"}';
 const result=parseProfileContent(malformed);
 assert.deepEqual(result.value,{...first,scope_summary:'仅本次章节'});
 assert.equal(result.repair.kind,'remove_premature_root_close');
 assert.throws(()=>parseProfileContent('{"characters":[]},"other":"x"}'));
});
const x={mode:'character_profiles_v1',book_id:'book-0123456789abcdefabcd',chapter_id:'c003',target_names:names,
 chapters:[{chapter_id:'c002',text:'甲说：“好。”😀乙在门边。',text_sha256:hash('甲说：“好。”😀乙在门边。')},
 {chapter_id:'c003',text:'甲沉静地回答。丙快步走来。',text_sha256:hash('甲沉静地回答。丙快步走来。')}]};
const ev=(chapter_id,quote)=>[{chapter_id,quote}];
const person=(name,quote='甲沉静地回答。')=>({name,aliases:[],
 identity:{value:'同行者',status:'explicit',evidence:ev('c003','甲沉静地回答。')},
 gender:{value:'',status:'unknown',evidence:[]},age:{value:'',status:'unknown',evidence:[]},
 personality:[{value:'沉静',status:'inferred',evidence:ev('c003','甲沉静地回答。')}],behavior:[],speech_style:[],
 voice_design:{description:'低沉、语速平稳',rationale:'依据沉静表达作创作选择',creative_choices:['平缓节奏']},uncertainties:[]});

test('builds bounded profile-only request and declares model/token cap',()=>{
 const b=buildProfileRequest(x), req=JSON.parse(b.toString());
 assert.ok(Buffer.isBuffer(b)); assert.equal(req.model,'deepseek-flash');assert.equal(req.max_tokens,8000);
 assert.equal(req.response_format.type,'json_object');
 assert.match(req.messages[1].content,/只允许explicit或unknown/);assert.match(req.messages[1].content,/字符位置或哈希/);
});

test('discovery mode accepts an empty target list and requires a locatable name quote',()=>{
 const chapter={chapter_id:'c010',text:'林舟把伞递给同伴。',text_sha256:hash('林舟把伞递给同伴。')};
 const discovery={mode:'character_profiles_v1',discover_characters:true,book_id:x.book_id,chapter_id:'c010',target_names:[],chapters:[chapter]};
 const req=JSON.parse(buildProfileRequest(discovery).toString());
 assert.equal(req.max_tokens,8000);assert.match(req.messages[1].content,/发现有明确姓名/);
 const parsed={scope_summary:'本章只覆盖一名有明确称谓的人物。',narration_design:{description:'平实旁白',rationale:'创作选择',creative_choices:[]},
  characters:[{name:'林舟',name_evidence:ev('c010','林舟把伞递给同伴。'),aliases:[],identity:{value:'',status:'unknown',evidence:[]},
   gender:{value:'',status:'unknown',evidence:[]},age:{value:'',status:'unknown',evidence:[]},personality:[],behavior:[],speech_style:[],
   voice_design:{mode:'narrator_only',description:'',rationale:'本章只有叙述提及',creative_choices:[]},uncertainties:[]}]};
 const normalized=normalizeProfileResult(parsed,discovery);
 assert.equal(normalized.characters[0].name,'林舟');assert.equal(normalized.characters[0].voice_design.mode,'narrator_only');
 assert.equal(normalized.characters[0].name_evidence[0].source_sha256,chapter.text_sha256);
 parsed.characters[0].name_evidence=ev('c010','把伞递给同伴。');
 assert.equal(normalizeProfileResult(parsed,discovery).characters.length,0);
 assert.throws(()=>buildProfileRequest({...discovery,chapters:[chapter, {...chapter,chapter_id:'c011'}, {...chapter,chapter_id:'c012'}, {...chapter,chapter_id:'c013'}]}),/invalid_profile_scope/);
});

test('legacy dialogue builder retains its original request shape',()=>{
 const text='甲说：“你好。”', chars=Array.from(text), src=[0,chars.length];
 const legacy={book_id:x.book_id,chapter_id:'c004',text,blocks:[{id:'d1',type:'dialogue',text,src}],
  prior_context:{version:1,book_id:x.book_id,target_chapter_id:'c004',records:[]}};
 const out=build(Buffer.from(JSON.stringify(legacy)));
 const request=JSON.parse(out.request.toString());
 assert.equal(request.max_tokens,4000);assert.equal(out.rows.length,1);assert.match(request.messages[1].content,/dialogues/);
});

test('normalizes literal evidence to Unicode codepoint anchors and source hashes',()=>{
 const parsed={characters:names.map((n,i)=>person(n,i===0?'甲沉静地回答。':'甲沉静地回答。')),scope_summary:'仅基于两章样本。'};
 const out=normalizeProfileResult(parsed,x), c=out.characters[0], a=c.personality[0].evidence[0];
 assert.equal(out.status,'needs_review');assert.equal(out.human_reviewed,false);
 assert.deepEqual(a.source_range,[0,7]);assert.equal(a.source_sha256,x.chapters[1].text_sha256);
 assert.deepEqual(c.gender,{value:'',status:'unknown',evidence:[]});
 assert.equal(c.voice_design.status,'candidate_needs_review');assert.equal(out.characters.length,5);
});

test('bad quotes downgrade claims to unknown and add an issue without retry semantics',()=>{
 const parsed={characters:names.map(n=>person(n)),scope_summary:'范围有限'};
 parsed.characters[0].personality[0].evidence=ev('c003','并不存在的引文');
 parsed.characters[0].gender={value:'男',status:'inferred',evidence:ev('c003','甲沉静地回答。')};
 const out=normalizeProfileResult(parsed,x);
 assert.deepEqual(out.characters[0].personality,[]);
 assert.deepEqual(out.characters[0].gender,{value:'',status:'unknown',evidence:[]});
 assert.ok(out.issues.some(i=>i.reason==='quote_not_literal_source'));
 assert.ok(out.issues.some(i=>i.reason==='demographic_inference_disallowed'));
});

test('missing target is emitted as unknown profile and flagged for review',()=>{
 const parsed={characters:names.slice(0,4).map(n=>person(n)),scope_summary:'部分覆盖'};
 const out=normalizeProfileResult(parsed,x), last=out.characters.find(c=>c.name==='戊');
 assert.deepEqual(last.identity,{value:'',status:'unknown',evidence:[]});
 assert.equal(last.voice_design.status,'candidate_needs_review');
 assert.ok(out.issues.some(i=>i.name==='戊'&&i.reason==='target_missing_from_model_result'));
});

test('rejects mutated source hash, wrong scope, and oversized text',()=>{
 assert.throws(()=>buildProfileRequest({...x,chapters:[{...x.chapters[0],text_sha256:'0'.repeat(64)}]}),/profile_source_hash_mismatch/);
 assert.throws(()=>buildProfileRequest({...x,target_names:[names[0],names[0]]}),/invalid_profile_targets/);
 assert.doesNotThrow(()=>buildProfileRequest({...x,target_names:names.slice(0,1)}));
 const huge='你'.repeat(60001);assert.throws(()=>buildProfileRequest({...x,chapters:[{chapter_id:'c003',text:huge,text_sha256:hash(huge)}]}),/60000_codepoints/);
});

test('worker dispatches profile mode once into its independent receipt namespace',async()=>{
 const root=await mkdtemp(path.join(os.tmpdir(),'profile-worker-'));
 try{
  const input=path.join(root,'input.json'),output=path.join(root,'out'),jobs=path.join(root,'jobs');
  await mkdir(jobs);await writeFile(input,JSON.stringify(x));
  const body=Buffer.from(JSON.stringify({id:'synthetic-response',model:'deepseek-flash',usage:{prompt_tokens:123,completion_tokens:45,total_tokens:168},choices:[{finish_reason:'stop',message:{content:JSON.stringify({characters:names.map(n=>person(n)),scope_summary:'synthetic only'})}}]}));
  let request;
  const fakeFetch=async(_url,options)=>{request=JSON.parse(options.body.toString());return {ok:true,status:200,headers:new Headers({'content-type':'application/json'}),arrayBuffer:async()=>body.buffer.slice(body.byteOffset,body.byteOffset+body.byteLength)};};
  const result=await run({input,output,jobs,key:'synthetic-test-key',fetchImpl:fakeFetch});
  assert.equal(result.status,'needs_review');assert.equal(result.retry_allowed,false);assert.equal(result.usage.prompt_tokens,123);
  assert.equal(request.max_tokens,8000);assert.equal(request.model,'deepseek-flash');
  const receipt=path.join(output,'character_profiles_v1');
  const intent=JSON.parse(await readFile(path.join(receipt,'intent.json'),'utf8'));
  const saved=JSON.parse(await readFile(path.join(receipt,'result.json'),'utf8'));
  assert.equal(intent.purpose,'character_profiles_v1');assert.equal(intent.max_tokens,8000);
  assert.equal(saved.response_id,'synthetic-response');assert.equal(saved.result.mode,'character_profiles_v1');
  assert.equal('dialogues' in saved.result,false);assert.equal(saved.result.characters.length,5);
  assert.ok((await readFile(path.join(jobs,'.character-profiles-'+createHash('sha256').update(`${x.book_id}|${x.chapter_id}|${createHash('sha256').update(await readFile(input)).digest('hex')}`).digest('hex')+'.intent'))));
 }finally{await rm(root,{recursive:true,force:true});}
});
