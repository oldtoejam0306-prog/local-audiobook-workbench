import test from 'node:test';
import assert from 'node:assert/strict';
import {repairMalformedProfileJson} from './profile_json_recovery.mjs';

const person = name => ({name, name_evidence:[{chapter_id:'c001',quote:'甲向乙点头。'}], aliases:[],
 identity:{value:'',status:'unknown',evidence:[]}, gender:{value:'',status:'unknown',evidence:[]},
 age:{value:'',status:'unknown',evidence:[]}, personality:[], behavior:[], speech_style:[],
 voice_design:{mode:'character_voice',description:'',rationale:'',creative_choices:[]}, uncertainties:[]});

function prematurelyClose(people) {
 let text=JSON.stringify({characters:people,relations:[],narration_design:null,scope_summary:'synthetic'});
 let from=0;
 for(const row of people){
  const marker='"name_evidence":'+JSON.stringify(row.name_evidence);
  const start=text.indexOf(marker,from);
  assert.notEqual(start,-1);
  const end=start+marker.length;
  text=text.slice(0,end)+'}]'+text.slice(end);
  from=end+2;
 }
 return text;
}

test('repairs all bounded recognized character/list closers and preserves parsed values',()=>{
 const expected={characters:[person('甲'),person('乙'),person('丙')],relations:[],narration_design:null,scope_summary:'synthetic'};
 const result=repairMalformedProfileJson(prematurelyClose(expected.characters));
 assert.deepEqual(result.value,expected);
 assert.equal(result.repair.removals.length,3);
 assert.ok(result.repair.removals.every(row=>row.removed==='}]'&&row.next_field==='aliases'));
});

test('does not match delimiter-like content inside quoted values or repair unmatched syntax',()=>{
 const valid={characters:[person('甲')],relations:[],narration_design:null,scope_summary:'literal }],"aliases": text'};
 const result=repairMalformedProfileJson(JSON.stringify(valid));
 assert.equal(result.repair,null);
 assert.throws(()=>repairMalformedProfileJson('{"characters":[{"name":"甲","name_evidence":[]}],"unexpected":'));
});

test('refuses more than eight structurally recognized repairs',()=>{
 const many=Array.from({length:9},(_unused,index)=>person('人'+index));
 assert.throws(()=>repairMalformedProfileJson(prematurelyClose(many)),/too_many_profile_json_repairs/);
});

test('recovers only the premature root before a final scope summary with relations',()=>{
 const expected={characters:[person('甲')],relations:[],narration_design:null,scope_summary:'synthetic'};
 const malformed=JSON.stringify(expected).replace(',"scope_summary":','},"scope_summary":');
 const result=repairMalformedProfileJson(malformed);
 assert.deepEqual(result.value,expected);
 assert.equal(result.repair.kind,'remove_premature_scope_summary_root_closer');
 assert.deepEqual(result.repair.removals,[{character_offset:malformed.indexOf('},"scope_summary":'),
  removed:'}',next_field:'scope_summary'}]);
 assert.equal(malformed.slice(0,result.repair.removals[0].character_offset)
  +malformed.slice(result.repair.removals[0].character_offset+1),result.text);
});

test('refuses other trailing fields, duplicate scope summaries and invalid recovered shapes',()=>{
 const first=JSON.stringify({characters:[person('甲')],relations:[],narration_design:null});
 assert.throws(()=>repairMalformedProfileJson(first+',"other":"value"}'));
 assert.throws(()=>repairMalformedProfileJson(first+',"scope_summary":"ok","other":"value"}'));
 assert.throws(()=>repairMalformedProfileJson('{"characters":"invalid"},"scope_summary":"ok"}'));
 assert.throws(()=>repairMalformedProfileJson(
  JSON.stringify({characters:[person('甲')],scope_summary:'first'})+',"scope_summary":"second"}'));
});
