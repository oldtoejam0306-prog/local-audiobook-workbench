import hashlib
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import real_book_sequence as sequence
import real_book_sequence_callbacks as base_callbacks
import real_book_auto_callbacks as auto_callbacks
from unittest.mock import patch
import workbench_job_store as store


BOOK='book-'+'7'*20
INDEX='a'*64
VOICE='b'*64


def sha(raw):return hashlib.sha256(raw).hexdigest()


def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')


class WorkbenchJobStoreTests(unittest.TestCase):
    def _job(self,root,*,index=INDEX,source=None,voice='voice-one',chapter='c001'):
        source=source or sha(b'source')
        chapters=[{'id':chapter,'source_sha256':source}]
        voices={'narrator':voice}
        job=store.begin_job(root,BOOK,index,chapters,sequence.fingerprint(voices))
        plan=sequence.create_plan(Path(job['sequence_root']),BOOK,index,chapters,voices)
        return job,plan,chapters

    def _mark_rendered(self,job,plan,chapter='c001'):
        chapter_root=Path(job['sequence_root'])/BOOK/chapter
        chapter_root.mkdir(parents=True,exist_ok=True)
        artifact={'path':'synthetic-receipt.json','sha256':sha(b'receipt')}
        identity={'book_id':BOOK,'chapter_id':chapter,'stage':'render',
                  'plan_sha256':sequence.fingerprint(plan)}
        done={'status':'ok','human_reviewed':False,'identity':identity,'artifact':artifact,
              'artifact_sha256':sequence.fingerprint(artifact)}
        (chapter_root/'render.done.json').write_bytes(canonical(done))

    def test_completed_lookup_keeps_same_source_prior_audio_and_prefers_completed_current(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=sha(b'same source')
            first,first_plan,_=self._job(root,source=source,voice='voice-old')
            self._mark_rendered(first,first_plan)
            current,current_plan,_=self._job(root,source=source,voice='voice-new')
            self.assertEqual(current['sequence_root'],store.find_sequence_root(root,BOOK,'c001'))
            self.assertEqual(first['sequence_root'],store.find_sequence_root(
                root,BOOK,'c001',require_completed=True))
            self._mark_rendered(current,current_plan)
            self.assertEqual(current['sequence_root'],store.find_sequence_root(
                root,BOOK,'c001',require_completed=True))

    def test_completed_lookup_never_falls_back_across_index_or_source_change(self):
        for current_index,current_source in ((INDEX,sha(b'changed source')),
                                             ('c'*64,sha(b'original source'))):
            with self.subTest(index=current_index,source=current_source), tempfile.TemporaryDirectory() as td:
                root=Path(td);source=sha(b'original source')
                old,old_plan,_=self._job(root,index=INDEX,source=source,voice='voice-old')
                self._mark_rendered(old,old_plan)
                self._job(root,index=current_index,source=current_source,voice='voice-current')
                self.assertIsNone(store.find_sequence_root(root,BOOK,'c001',require_completed=True))

    def test_completed_lookup_fails_closed_for_corrupt_receipt_and_absent_store(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);self.assertIsNone(store.find_sequence_root(
                root,BOOK,'c001',require_completed=True))
            legacy_root=root/'sequence';legacy_voices={'narrator':'preset:legacy'}
            legacy_plan=sequence.create_plan(legacy_root,BOOK,INDEX,
                [{'id':'c001','source_sha256':sha(b'source')}],legacy_voices)
            legacy_chapter=legacy_root/BOOK/'c001';legacy_chapter.mkdir(parents=True)
            legacy_artifact={'path':'legacy-receipt','sha256':sha(b'legacy')}
            legacy_done={'status':'ok','human_reviewed':False,
                'identity':{'book_id':BOOK,'chapter_id':'c001','stage':'render',
                            'plan_sha256':sequence.fingerprint(legacy_plan)},
                'artifact':legacy_artifact,'artifact_sha256':sequence.fingerprint(legacy_artifact)}
            (legacy_chapter/'render.done.json').write_bytes(canonical(legacy_done))
            job,plan,_=self._job(root);self._mark_rendered(job,plan)
            done_path=Path(job['sequence_root'])/BOOK/'c001'/'render.done.json'
            done=json.loads(done_path.read_bytes());done['artifact_sha256']='0'*64
            done_path.write_bytes(canonical(done))
            with self.assertRaisesRegex(store.JobStoreError,'render receipt identity or artifact hash invalid'):
                store.find_sequence_root(root,BOOK,'c001',require_completed=True)

    def test_begin_job_is_idempotent_keeps_history_and_resolves_latest_render(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);chapters=[{'id':'c001','source_sha256':sha(b'one')}]
            voices={'narrator':'preset:one'};voice_hash=sequence.fingerprint(voices)
            first=store.begin_job(root,BOOK,INDEX,chapters,voice_hash)
            again=store.begin_job(root,BOOK,INDEX,chapters,voice_hash)
            self.assertEqual(first,again)
            plan=sequence.create_plan(Path(first['sequence_root']),BOOK,INDEX,chapters,voices)
            chapter_root=Path(first['sequence_root'])/BOOK/'c001';chapter_root.mkdir(parents=True)
            artifact={'path':'immutable/receipt.json','sha256':sha(b'receipt')}
            identity={'book_id':BOOK,'chapter_id':'c001','stage':'render',
                'plan_sha256':sequence.fingerprint(plan)}
            done={'status':'ok','human_reviewed':False,'identity':identity,'artifact':artifact,
                  'artifact_sha256':sequence.fingerprint(artifact)}
            (chapter_root/'render.done.json').write_bytes(canonical(done))
            second_voices={'narrator':'preset:two'}
            second=store.begin_job(root,BOOK,INDEX,chapters,sequence.fingerprint(second_voices))
            second_plan=sequence.create_plan(Path(second['sequence_root']),BOOK,INDEX,chapters,second_voices)
            second_chapter=Path(second['sequence_root'])/BOOK/'c001';second_chapter.mkdir(parents=True)
            second_identity={'book_id':BOOK,'chapter_id':'c001','stage':'render',
                'plan_sha256':sequence.fingerprint(second_plan)}
            second_done={'status':'ok','human_reviewed':False,'identity':second_identity,'artifact':artifact,
                'artifact_sha256':sequence.fingerprint(artifact)}
            (second_chapter/'render.done.json').write_bytes(canonical(second_done))
            third=store.begin_job(root,BOOK,INDEX,[{'id':'c002','source_sha256':sha(b'two')}],sha(b'third voice'))
            self.assertEqual(third['sequence_root'],store.find_sequence_root(root,BOOK,'c002'))
            self.assertEqual(second['sequence_root'],store.find_sequence_root(root,BOOK,'c001'))
            self.assertEqual(3,len(json.loads((root/'runs'/BOOK/'jobs.json').read_bytes())['jobs']))

    def test_absent_store_falls_back_to_legacy_sequence(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            self.assertEqual(str(root/'sequence'),store.find_sequence_root(root,BOOK,'c001'))

    def test_legacy_completed_lookup_requires_self_consistent_plan_and_render_done(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);sequence_root=root/'sequence';source=sha(b'legacy source')
            chapters=[{'id':'c001','source_sha256':source}];voices={'narrator':'preset:legacy'}
            plan=sequence.create_plan(sequence_root,BOOK,INDEX,chapters,voices)
            chapter_root=sequence_root/BOOK/'c001';chapter_root.mkdir(parents=True)
            artifact={'path':'legacy-render.json','sha256':sha(b'receipt')}
            done={'status':'ok','human_reviewed':False,
                  'identity':{'book_id':BOOK,'chapter_id':'c001','stage':'render',
                              'plan_sha256':sequence.fingerprint(plan)},
                  'artifact':artifact,'artifact_sha256':sequence.fingerprint(artifact)}
            (chapter_root/'render.done.json').write_bytes(canonical(done))
            self.assertEqual(str(sequence_root),store.find_sequence_root(
                root,BOOK,'c001',require_completed=True))
            (chapter_root/'render.done.json').unlink()
            self.assertIsNone(store.find_sequence_root(root,BOOK,'c001',require_completed=True))
            bad_plan=dict(plan,index_sha256='invalid')
            (sequence_root/BOOK/'plan.json').write_bytes(canonical(bad_plan))
            with self.assertRaisesRegex(store.JobStoreError,'legacy sequence plan identity'):
                store.find_sequence_root(root,BOOK,'c001',require_completed=True)

    def test_corrupt_existing_job_store_does_not_fall_back_to_legacy_root(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);created=store.begin_job(root,BOOK,INDEX,[{'id':'c001','source_sha256':sha(b'one')}],VOICE)
            pointer=root/'runs'/BOOK/'current-job.json';doc=json.loads(pointer.read_bytes());doc['job_sha256']='0'*64
            pointer.write_bytes(canonical(doc))
            with self.assertRaisesRegex(store.JobStoreError,'hash mismatch'):
                store.find_sequence_root(root,BOOK,'c001')
            self.assertNotEqual(str(root/'sequence'),created['sequence_root'])

    def test_publish_and_read_memory_verify_internal_pointers_and_report_versions(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source='林舟来了。';source_sha=sha(source.encode())
            store.begin_job(root,BOOK,INDEX,[{'id':'c001','source_sha256':source_sha}],VOICE)
            external=root/'artifacts'/BOOK/'c001';external.mkdir(parents=True)
            memory={'book_id':BOOK,'chapter_id':'c001','source_sha256':source_sha,
                'characters':[{'name':'林舟'}],'human_reviewed':False}
            auto={'book_id':BOOK,'chapter_id':'c001','bindings':{'e1':'custom:voice'},
                'voice_design_plan':{'path':'old/run/voice-plan.json'},'entities':[
                    {'entity_id':'entity-a','name':'林舟','labels':['林舟','阿舟'],'chapter_name':'阿舟'}],
                'unresolved':[],'blocked':False,'human_reviewed':False}
            memory_path=external/'memory.json';auto_path=external/'auto.json'
            memory_path.write_bytes(canonical(memory));auto_path.write_bytes(canonical(auto))
            pointer={'path':str(memory_path),'sha256':sha(memory_path.read_bytes()),
                'auto':{'path':str(auto_path),'sha256':sha(auto_path.read_bytes())}}
            chapter={'id':'c001','source_sha256':source_sha}
            first=store.publish_memory(root,BOOK,INDEX,chapter,pointer)
            reused=store.publish_memory(root,BOOK,INDEX,chapter,pointer)
            self.assertEqual(first['version_id'],reused['version_id']);self.assertFalse(reused['conflict'])
            auto2={'book_id':BOOK,'chapter_id':'c001','bindings':{'e1':'custom:different'},
                'voice_design_plan':{'path':'another/run/plan.json'},'entities':auto['entities'],
                'unresolved':[],'blocked':False,'human_reviewed':False}
            auto_path.write_bytes(canonical(auto2));pointer['auto']={'path':str(auto_path),'sha256':sha(auto_path.read_bytes())}
            voice_changed=store.publish_memory(root,BOOK,INDEX,chapter,pointer)
            self.assertEqual(first['version_id'],voice_changed['version_id'])
            self.assertFalse(voice_changed['conflict'])
            prior=store.prior_memories(root,BOOK,INDEX,'c002')
            self.assertEqual(1,len(prior));self.assertEqual(chapter,prior[0]['chapter'])
            self.assertEqual(memory, json.loads(Path(prior[0]['artifacts']['memory']['path']).read_bytes()))
            shared_auto=json.loads(Path(prior[0]['artifacts']['memory']['auto']['path']).read_bytes())
            self.assertEqual({'entity_id':'entity-a','name':'林舟','labels':['林舟','阿舟'],'chapter_name':'阿舟'},shared_auto['entities'][0])
            self.assertNotIn('bindings',shared_auto);self.assertNotIn('voice_design_plan',shared_auto)
            memory['chapter_summary']='different immutable later version'
            memory_path.write_bytes(canonical(memory));pointer['sha256']=sha(memory_path.read_bytes())
            conflict=store.publish_memory(root,BOOK,INDEX,chapter,pointer)
            self.assertTrue(conflict['conflict'])
            with self.assertRaises(store.MemoryConflictError):
                store.prior_memories(root,BOOK,INDEX,'c002')
            self.assertEqual([],store.prior_memories(root,'book-'+'8'*20,INDEX,'c002'))

    def test_auto_entity_semantics_change_creates_explicit_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source_sha=sha(b'text');chapter={'id':'c001','source_sha256':source_sha}
            store.begin_job(root,BOOK,INDEX,[chapter],VOICE)
            external=root/'artifacts';external.mkdir()
            memory={'book_id':BOOK,'chapter_id':'c001','source_sha256':source_sha,'human_reviewed':False}
            mp=external/'memory.json';mp.write_bytes(canonical(memory))
            auto_path=external/'auto.json'
            pointer={'path':str(mp),'sha256':sha(mp.read_bytes()),'auto':{'path':str(auto_path)}}
            for labels in (['甲'],['乙']):
                auto={'book_id':BOOK,'chapter_id':'c001','entities':[{'entity_id':'e1','name':labels[0],
                    'labels':labels}],'unresolved':[],'blocked':False,'human_reviewed':False}
                auto_path.write_bytes(canonical(auto));pointer['auto']['sha256']=sha(auto_path.read_bytes())
                result=store.publish_memory(root,BOOK,INDEX,chapter,pointer)
            self.assertTrue(result['version_conflict'])
            self.assertTrue(result['conflict'])
            with self.assertRaises(store.MemoryConflictError):
                store.prior_memories(root,BOOK,INDEX,'c002')

    def test_publish_rejects_external_pointer_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source_sha=sha(b'text')
            store.begin_job(root,BOOK,INDEX,[{'id':'c001','source_sha256':source_sha}],VOICE)
            path=root/'memory.json';path.write_bytes(canonical({'book_id':BOOK,'chapter_id':'c001',
                'source_sha256':source_sha,'human_reviewed':False}))
            auto=root/'auto.json';auto.write_bytes(canonical({'book_id':BOOK,'chapter_id':'c001','human_reviewed':False}))
            pointer={'path':str(path),'sha256':'0'*64,'auto':{'path':str(auto),'sha256':sha(auto.read_bytes())}}
            with self.assertRaisesRegex(store.JobStoreError,'hash mismatch'):
                store.publish_memory(root,BOOK,INDEX,{'id':'c001','source_sha256':source_sha},pointer)

    def test_blocked_memory_is_not_published_to_shared_store(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=sha(b'blocked source');chapter={'id':'c001','source_sha256':source}
            store.begin_job(root,BOOK,INDEX,[chapter],VOICE)
            external=root/'external';external.mkdir()
            memory={'book_id':BOOK,'chapter_id':'c001','source_sha256':source,'human_reviewed':False}
            auto={'book_id':BOOK,'chapter_id':'c001','entities':[],'unresolved':[],'blocked':True,'human_reviewed':False}
            mp=external/'memory.json';ap=external/'auto.json';mp.write_bytes(canonical(memory));ap.write_bytes(canonical(auto))
            pointer={'path':str(mp),'sha256':sha(mp.read_bytes()),'auto':{'path':str(ap),'sha256':sha(ap.read_bytes())}}
            self.assertTrue(store.memory_profile_blocked(root,BOOK,INDEX,chapter,pointer))
            result=store.publish_shared_memory(root,BOOK,INDEX,chapter,pointer)
            self.assertEqual({'skipped':True,'reason':'profile_blocked','human_reviewed':False},result)
            self.assertFalse((root/'memory'/BOOK/INDEX[:20]/'c001').exists())
            auto['blocked']=False;ap.write_bytes(canonical(auto));pointer['auto']['sha256']=sha(ap.read_bytes())
            self.assertFalse(store.memory_profile_blocked(root,BOOK,INDEX,chapter,pointer))
            self.assertTrue(store.publish_shared_memory(root,BOOK,INDEX,chapter,pointer)['path'])

    def test_operator_selected_memory_version_is_exact_and_stales_on_new_version(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=sha(b'source');chapter={'id':'c001','source_sha256':source}
            store.begin_job(root,BOOK,INDEX,[chapter],VOICE)
            external=root/'external';external.mkdir()
            memory={'book_id':BOOK,'chapter_id':'c001','source_sha256':source,'human_reviewed':False}
            mp=external/'memory.json';mp.write_bytes(canonical(memory))
            def publish(label,blocked):
                auto={'book_id':BOOK,'chapter_id':'c001','entities':[
                    {'entity_id':'e1','name':'synthetic','labels':[label]}],
                    'unresolved':[],'blocked':blocked,'human_reviewed':False}
                ap=external/(label+'.json');ap.write_bytes(canonical(auto))
                pointer={'path':str(mp),'sha256':sha(mp.read_bytes()),
                         'auto':{'path':str(ap),'sha256':sha(ap.read_bytes())}}
                return store.publish_memory(root,BOOK,INDEX,chapter,pointer)
            healthy=publish('healthy-choice',False)
            publish('other-healthy-choice',False)
            blocked=publish('blocked-choice',True)
            with self.assertRaisesRegex(store.MemoryConflictError,'divergent memory versions'):
                store.prior_memories(root,BOOK,INDEX,'c002')
            with self.assertRaisesRegex(store.MemoryConflictError,'blocked'):
                store.select_memory_version(root,BOOK,INDEX,'c001',source,blocked['version_id'])
            selected=store.select_memory_version(root,BOOK,INDEX,'c001',source,healthy['version_id'])
            self.assertFalse(selected['human_reviewed'])
            source_dir=root/'memory'/BOOK/INDEX[:20]/'c001'/source[:20]
            pointer=json.loads((source_dir/'current-selection.json').read_bytes())
            selection=json.loads(Path(pointer['path']).read_bytes())
            self.assertEqual(3,len(selection['versions']))
            self.assertEqual(healthy['version_id'],selection['selected_version_id'])
            self.assertEqual(sha(canonical(selection['versions'])),selection['versions_sha256'])
            prior=store.prior_memories(root,BOOK,INDEX,'c002')
            self.assertEqual(1,len(prior))
            selected_auto=json.loads(Path(prior[0]['artifacts']['memory']['auto']['path']).read_bytes())
            self.assertFalse(selected_auto['blocked'])
            publish('new-version-after-selection',False)
            with self.assertRaisesRegex(store.MemoryConflictError,'selection is stale'):
                store.prior_memories(root,BOOK,INDEX,'c002')
            selected=store.select_memory_version(root,BOOK,INDEX,'c001',source,healthy['version_id'])
            Path(selected['path']).write_bytes(b'tampered selection receipt')
            with self.assertRaisesRegex(store.JobStoreError,'pointer bytes/hash mismatch'):
                store.prior_memories(root,BOOK,INDEX,'c002')

    def test_prior_rejects_modified_internal_pointer_and_out_of_root_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source_sha=sha(b'text')
            store.begin_job(root,BOOK,INDEX,[{'id':'c001','source_sha256':source_sha}],VOICE)
            memory_path=root/'artifacts'/'memory.json';auto_path=root/'artifacts'/'auto.json';memory_path.parent.mkdir()
            memory_path.write_bytes(canonical({'book_id':BOOK,'chapter_id':'c001','source_sha256':source_sha,'human_reviewed':False}))
            auto_path.write_bytes(canonical({'book_id':BOOK,'chapter_id':'c001','human_reviewed':False}))
            pointer={'path':str(memory_path),'sha256':sha(memory_path.read_bytes()),
                'auto':{'path':str(auto_path),'sha256':sha(auto_path.read_bytes())}}
            stored=store.publish_memory(root,BOOK,INDEX,{'id':'c001','source_sha256':source_sha},pointer)
            internal=json.loads(Path(stored['version_path']).read_bytes())['memory']['path']
            Path(internal).write_bytes(b'tampered')
            with self.assertRaisesRegex(store.JobStoreError,'hash invalid'):
                store.prior_memories(root,BOOK,INDEX,'c002')

        with tempfile.TemporaryDirectory() as td:
            root=Path(td);outside=Path(td).parent/'outside-memory-pointer.json'
            try:
                outside.write_bytes(b'{}')
                with self.assertRaisesRegex(store.JobStoreError,'outside runtime root'):
                    store._pointer_bytes(root,{'path':str(outside),'sha256':sha(outside.read_bytes())},label='memory')
            finally:
                outside.unlink(missing_ok=True)

    def test_concurrent_job_and_memory_updates_are_serialized_per_book(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            specs=[({'id':f'c{i:03d}','source_sha256':sha(f'text{i}'.encode())},sha(f'voice{i}'.encode()))
                   for i in range(1,5)]
            with ThreadPoolExecutor(max_workers=4) as pool:
                jobs=list(pool.map(lambda pair:store.begin_job(root,BOOK,INDEX,[pair[0]],pair[1]),specs))
            registry=json.loads((root/'runs'/BOOK/'jobs.json').read_bytes())
            self.assertEqual(4,len(registry['jobs']))
            current=store.find_sequence_root(root,BOOK)
            self.assertIn(current,[job['sequence_root'] for job in jobs])

            def publish(pair):
                chapter,_=pair;cid=chapter['id'];external=root/'artifacts'/BOOK/cid;external.mkdir(parents=True)
                memory={'book_id':BOOK,'chapter_id':cid,'source_sha256':chapter['source_sha256'],'human_reviewed':False}
                auto={'book_id':BOOK,'chapter_id':cid,'human_reviewed':False}
                mp=external/'memory.json';ap=external/'auto.json';mp.write_bytes(canonical(memory));ap.write_bytes(canonical(auto))
                return store.publish_memory(root,BOOK,INDEX,chapter,{'path':str(mp),'sha256':sha(mp.read_bytes()),
                    'auto':{'path':str(ap),'sha256':sha(ap.read_bytes())}})
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(publish,specs))
            prior=store.prior_memories(root,BOOK,INDEX,'c010')
            self.assertEqual(4,len(prior))
            self.assertEqual({x[0]['id'] for x in specs},{item['chapter']['id'] for item in prior})

    def test_multiple_source_hashes_for_one_chapter_are_not_silently_selected(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);a=sha(b'version-a');b=sha(b'version-b')
            store.begin_job(root,BOOK,INDEX,[{'id':'c001','source_sha256':a}],VOICE)
            store.begin_job(root,BOOK,INDEX,[{'id':'c001','source_sha256':b}],sha(b'voice-b'))
            for source in (a,b):
                external=root/'artifacts'/source;external.mkdir(parents=True)
                memory={'book_id':BOOK,'chapter_id':'c001','source_sha256':source,'human_reviewed':False}
                auto={'book_id':BOOK,'chapter_id':'c001','human_reviewed':False}
                mp=external/'memory.json';ap=external/'auto.json';mp.write_bytes(canonical(memory));ap.write_bytes(canonical(auto))
                store.publish_memory(root,BOOK,INDEX,{'id':'c001','source_sha256':source},
                    {'path':str(mp),'sha256':sha(mp.read_bytes()),'auto':{'path':str(ap),'sha256':sha(ap.read_bytes())}})
            with self.assertRaises(store.MemoryConflictError):
                store.prior_memories(root,BOOK,INDEX,'c002')

    def test_prior_memory_auto_entities_drive_stable_alias_mapping_in_next_run(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);old_text='林舟又名阿舟。';new_text='阿舟又名林舟。'
            old_sha=sha(old_text.encode());new_sha=sha(new_text.encode())
            store.begin_job(root,BOOK,INDEX,[{'id':'c001','source_sha256':old_sha}],VOICE)
            store.begin_job(root,BOOK,INDEX,[{'id':'c002','source_sha256':new_sha}],sha(b'new voice profile'))
            ext=root/'artifacts'/BOOK/'c001';ext.mkdir(parents=True)
            memory_doc={'book_id':BOOK,'chapter_id':'c001','source_sha256':old_sha,
                'characters':[{'name':'林舟','aliases':['阿舟'],'evidence':old_text}],
                'human_reviewed':False}
            auto_doc={'book_id':BOOK,'chapter_id':'c001','entities':[
                {'entity_id':'stable-e1','name':'林舟','labels':['林舟','阿舟'],'chapter_name':'林舟'}],
                'unresolved':[],'blocked':False,'human_reviewed':False,
                'bindings':{'stable-e1':'custom:old-selected'}}
            memory_path=ext/'memory.json';auto_path=ext/'auto.json'
            memory_path.write_bytes(canonical(memory_doc));auto_path.write_bytes(canonical(auto_doc))
            published=store.publish_memory(root,BOOK,INDEX,{'id':'c001','source_sha256':old_sha},
                {'path':str(memory_path),'sha256':sha(memory_path.read_bytes()),
                 'auto':{'path':str(auto_path),'sha256':sha(auto_path.read_bytes())}})
            prior=store.prior_memories(root,BOOK,INDEX,'c002')
            self.assertEqual('stable-e1',json.loads(Path(prior[0]['artifacts']['memory']['auto']['path']).read_bytes())['entities'][0]['entity_id'])

            quote='林舟又名阿舟。'
            anchor={'chapter_id':'c001','quote':quote,'source_range':[0,len(quote)],'source_sha256':old_sha}
            unknown={'value':'','status':'unknown','evidence':[]}
            profile={'name':'林舟','aliases':[{'name':'阿舟','evidence':[anchor]}],
                'identity':unknown,'gender':unknown,'age':unknown,
                'personality':[{'value':'沉稳','status':'explicit','evidence':[anchor]}],
                'behavior':[],'speech_style':[],
                'voice_design':{'description':'清晰自然的中音','rationale':'源文表达','creative_choices':['语速平稳'],
                    'status':'candidate_needs_review'}}
            registry=root/'character-profiles'/BOOK/'current.json';registry.parent.mkdir(parents=True)
            registry.write_bytes(canonical({'version':1,'mode':'character_profiles_v1','book_id':BOOK,
                'source':{'chapters':[{'chapter_id':'c001','text_sha256':old_sha,'codepoint_length':len(old_text)}]},
                'characters':[profile],'human_reviewed':False}))
            current_memory=base_callbacks._save(root/'candidate-c002.json',{'book_id':BOOK,'chapter_id':'c002',
                'characters':[{'name':'阿舟','aliases':[],'evidence':new_text}]})
            payload={'book_id':BOOK,'chapter_id':'c002','text':new_text,'blocks':[]}
            input_path=root/'input-c002.json';input_path.write_bytes(canonical(payload))
            voices={'narrator':{'id':'n','voice':'preset:test'},'characters':{}}
            bridge=auto_callbacks.AutoBridge(root/'data',None,None,voices,voice_root=root/'auto-voices',
                narrator={},auto_renderer=lambda *x:None,auto_verify=lambda *x:True)
            captured=[]
            def allocate(_root,_book,profiles,_narrator,_generator):
                captured.extend(profiles);return {'status':'complete','bindings':{},'unresolved':[]}
            chapter={'book_id':BOOK,'id':'c002','source_sha256':new_sha,
                'voice_bindings_sha256':sequence.fingerprint(voices)}
            artifacts={'analyze':{'input_path':str(input_path),'input_sha256':sha(input_path.read_bytes())}}
            with patch.object(base_callbacks.Bridge,'memory',return_value={'artifact':current_memory}),\
                    patch.object(auto_callbacks.auto,'allocate',side_effect=allocate):
                result=bridge.memory(chapter,prior,artifacts)
            stored=base_callbacks._read(**result['artifact']['auto'])
            self.assertEqual('stable-e1',stored['entities'][0]['entity_id'])
            self.assertTrue(any(p['entity_id']=='stable-e1' and p['name']=='林舟' for p in captured))


if __name__=='__main__':unittest.main()
