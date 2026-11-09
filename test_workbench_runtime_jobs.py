import hashlib
import json
import tempfile
import unittest
import wave
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

import analysis_dispatch_cache
import correction_memory
import real_book_sequence as sequence
import real_pipeline_workflow as workflow
import workbench_job_store as job_store
import workbench_real_v2_runtime as runtime


INDEX_SHA='a'*64
SYNTHETIC_BOOK_ID='book-0123456789abcdefabcd'
runtime.registered_books=lambda:{'Synthetic Book':SYNTHETIC_BOOK_ID}
runtime.experiment.index_bridge.books=lambda:{'Synthetic Book':SYNTHETIC_BOOK_ID}


def sha(raw):return hashlib.sha256(raw).hexdigest()


def canonical(value):return workflow._bytes(value)


def create_narrator(root,book,suffix):
    directory=Path(root)/('narrator-'+suffix);directory.mkdir(parents=True)
    vector=directory/'speaker-vector.npy';vector.write_bytes(('vector-'+suffix).encode())
    key=book+'\0narrator'
    record={'book_role_key':key,'directory':str(directory),'request':{'stage':'synthetic',
        'vector_sha256':sha(vector.read_bytes())}}
    return {'console_key':key,'record':record}


class RuntimeJobIntegrationTests(unittest.TestCase):
    def setup_runtime(self,root,*,ids=('c001','c002'),raw_dispatch=None):
        book=SYNTHETIC_BOOK_ID
        payloads={cid:{'book_id':book,'chapter_id':cid,'text':'合成章节'+cid,'blocks':[]} for cid in ids}
        def index_request(action,request_book,chapter=None):
            index_calls.append((action,request_book,chapter))
            if request_book!=book:raise AssertionError('cross-book request')
            if action=='list':
                return {'book_id':book,'human_reviewed':False,'index_sha256':INDEX_SHA,'chapters':list(ids)}
            payload=payloads[chapter]
            return {'book_id':book,'chapter_id':chapter,'human_reviewed':False,'index_sha256':INDEX_SHA,
                'payload':payload,'payload_sha256':sha(canonical(payload))}
        index_calls=[]
        profiles=Path(root)/'character-profiles'/book/'current.json';profiles.parent.mkdir(parents=True)
        profiles.write_bytes(canonical({'version':1,'mode':'character_profiles_v1','book_id':book,
            'characters':[],'source':{'chapters':[]},'human_reviewed':False}))
        cache=analysis_dispatch_cache.CachedAnalysisDispatcher(root,
            dispatch=raw_dispatch or (lambda *args:None),worker_sha256=sha(b'synthetic-worker'))
        voices,executor=runtime.make_executor('Synthetic Book',root=root,index_request=index_request,
            dispatch=cache,legacy_guard=lambda *_:None)
        frozen={}
        for cid,payload in payloads.items():
            identity={'book_id':book,'chapter_id':cid,'index_sha256':INDEX_SHA,
                'voice_mapping_sha256':sequence.fingerprint(voices),
                'source_sha256':sha(canonical(payload))}
            frozen[cid]=(identity,payload)
        index_request.calls=index_calls
        return book,payloads,index_request,cache,voices,executor,frozen

    def test_legacy_guard_uses_configured_remote_worker_and_none_requires_ack(self):
        with patch.object(runtime.release_config,'legacy_chapter_mode',return_value='none'),\
             patch('subprocess.run',side_effect=AssertionError('legacy_mode=none must not use SSH')):
            runtime.legacy_check(SYNTHETIC_BOOK_ID,'c001',runner=lambda *args,**kwargs:None)
        result=SimpleNamespace(returncode=0,stdout='{"status":"clear"}')
        run=Mock(return_value=result)
        with patch.object(runtime.release_config,'legacy_chapter_mode',return_value='root'),\
             patch.object(runtime.release_config,'remote_legacy_chapter_root',return_value='/srv/synthetic/legacy'),\
             patch.object(runtime.release_config,'ssh_options',return_value=['-o','BatchMode=yes']),\
             patch.object(runtime.release_config,'ssh_target',return_value='synthetic-host'),\
             patch.object(runtime.release_config,'remote_command',return_value='env python3 /srv/app/legacy_chapter_state.py'),\
             patch('subprocess.run',side_effect=AssertionError('injected runner expected')):
            runtime.legacy_check(SYNTHETIC_BOOK_ID,'c001',runner=run)
        args=run.call_args.args[0]
        self.assertEqual(args[:4],['ssh','-o','BatchMode=yes','synthetic-host'])
        self.assertIn('legacy_chapter_state.py',args[-1])
        self.assertIn('"legacy_root": "/srv/synthetic/legacy"',run.call_args.kwargs['input'])

    def run_analysis_only(self,root,*,ids,target,raw_calls):
        book,payloads,index,cache,voices,executor,frozen=self.setup_runtime(root,ids=ids,
            raw_dispatch=lambda payload,private_root,jobs_root:(raw_calls.append(json.loads(canonical(payload))),
                self.write_cache_receipt(payload,private_root)))
        narrator=create_narrator(root,book,'correction-test')
        def fake_sequence_run(sequence_root,plan,callbacks,**_kwargs):
            chapter=dict(plan['chapters'][0],book_id=book,index_sha256=plan['index_sha256'],
                voice_bindings_sha256=plan['voice_bindings_sha256'])
            result=callbacks['analyze'](chapter,[],{})
            return {'status':'complete','analysis_result':result}
        with patch.object(runtime,'auto_narrator',return_value=narrator),\
                patch.object(runtime.experiment.seq,'run',side_effect=fake_sequence_run),\
                patch.object(runtime.experiment.callbacks.analysis,'build',side_effect=self.fake_analysis_build),\
                patch('subprocess.run',side_effect=AssertionError('SSH/API must not be called')):
            result=executor.execute_range([frozen[target]],voices)
        return result,book,payloads,index,executor,voices,frozen

    def test_runtime_creates_new_ranges_and_voice_jobs_with_matching_plan_fingerprints_and_cache_reuse(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'short-runtime';root.mkdir();book=SYNTHETIC_BOOK_ID;raw_calls=[]
            def raw_dispatch(payload,private_root,jobs_root):
                raw_calls.append(payload)
                self.write_cache_receipt(payload,private_root)
            book,payloads,index,cache,voices,executor,frozen=self.setup_runtime(root,raw_dispatch=raw_dispatch)
            n1=create_narrator(root,book,'one');n2=create_narrator(root,book,'two');runs=[]
            def fake_run(job_root,legacy_root,run_book,chapter_ids,actual_voices,role_bindings,verify,**kwargs):
                self.assertEqual(run_book,book)
                self.assertEqual(str(root),str(Path(job_root).parents[2]))
                specs=[{'id':cid,'source_sha256':sha(payloads[cid]['text'].encode())} for cid in chapter_ids]
                plan=sequence.create_plan(Path(job_root)/'sequence',book,INDEX_SHA,specs,actual_voices)
                self.assertEqual(plan['voice_bindings_sha256'],sequence.fingerprint(actual_voices))
                dispatcher=kwargs['dispatch']
                for cid in chapter_ids:dispatcher(payloads[cid])
                runs.append({'job_root':str(job_root),'chapters':chapter_ids,'voices':actual_voices,'plan':plan})
                return {'status':'complete','chapters':len(chapter_ids)}
            with patch.object(runtime,'auto_narrator',side_effect=[n1,n2,n2]),\
                    patch.object(runtime.experiment,'run_experiment',side_effect=fake_run),\
                    patch.object(runtime.experiment.callbacks.analysis,'build',side_effect=self.fake_analysis_build):
                first=executor.execute_range([frozen['c001']],voices)
                changed_voice=executor.execute_range([frozen['c001']],voices)
                changed_range=executor.execute_range([frozen['c002']],voices)
            self.assertEqual('complete',first['status']);self.assertEqual('complete',changed_voice['status']);self.assertEqual('complete',changed_range['status'])
            self.assertNotEqual(first['job_id'],changed_voice['job_id'])
            self.assertNotEqual(changed_voice['job_id'],changed_range['job_id'])
            self.assertEqual(2,len(raw_calls)) # same c001 source reused when only narrator changed
            self.assertEqual(['c001','c002'],[x['chapter_id'] for x in raw_calls])
            for run in runs:
                manifest=json.loads((Path(run['job_root'])/'job.json').read_bytes())
                plan=json.loads((Path(run['job_root'])/'sequence'/book/'plan.json').read_bytes())
                self.assertEqual(manifest['voice_fingerprint'],sequence.fingerprint(run['voices']))
                self.assertEqual(plan['voice_bindings_sha256'],manifest['voice_fingerprint'])
                self.assertEqual(Path(run['job_root']).name,manifest['job_id'][:20])
                self.assertLess(len(str(Path(run['job_root']))),220)
            self.assertEqual(changed_range['sequence_root'],job_store.find_sequence_root(root,book,'c002'))

    def test_missing_credentials_pause_resume_and_shared_prior_do_not_redispatch(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'short-runtime';root.mkdir();book=SYNTHETIC_BOOK_ID
            raw_calls=[];dispatch_holder={};stop_on_next=[False]
            def raw_dispatch(payload,private_root,jobs_root):
                raw_calls.append(payload)
                self.write_cache_receipt(payload,private_root)
                if stop_on_next[0]:
                    stop_on_next[0]=False
                    dispatch_holder['executor'].request_stop()
            book,payloads,index,cache,voices,executor,frozen=self.setup_runtime(root,raw_dispatch=raw_dispatch)
            dispatch_holder['executor']=executor
            narrator=create_narrator(root,book,'pause')
            missing=workflow.CredentialUnavailableError('missing test credential')
            cache.preflight=lambda: (_ for _ in ()).throw(missing)
            original_render=runtime.experiment.renderer.render
            def offline_render(*args,**kwargs):
                kwargs['render_segment']=self.fake_render_segment
                return original_render(*args,**kwargs)
            with patch.object(runtime,'auto_narrator',return_value=narrator),\
                    patch.object(runtime.experiment.callbacks.analysis,'build',side_effect=self.fake_analysis_build),\
                    patch.object(runtime.experiment.callbacks.workflow,'stage_receipt',side_effect=self.fake_stage_receipt),\
                    patch.object(runtime.experiment.renderer,'render',side_effect=offline_render),\
                    patch.object(sequence,'_safe_exception_type',side_effect=lambda exc:f'{type(exc).__name__}: {exc}'):
                no_key=executor.execute_range([frozen['c001']],voices)
                self.assertEqual('configuration_required',no_key['status'])
                self.assertEqual([],raw_calls)
                cache_entries=list((root/'provider-cache').rglob('binding.json')) if (root/'provider-cache').exists() else []
                self.assertEqual([],cache_entries)
                analyze_reservation=Path(no_key['sequence_root'])/book/'c001'/'analyze.reserved.json'
                self.assertFalse(analyze_reservation.exists())

                cache.preflight=lambda:None
                stop_on_next[0]=True
                paused=executor.execute_range([frozen['c001']],voices)
                self.assertEqual('paused',paused['status'])
                self.assertEqual(1,len(raw_calls))
                analyze_done=Path(paused['sequence_root'])/book/'c001'/'analyze.done.json'
                self.assertTrue(analyze_done.is_file())
                resumed=executor.execute_range([frozen['c001']],voices)
                self.assertEqual('complete',resumed['status'],resumed)
                self.assertEqual(1,len(raw_calls))

                next_chapter=executor.execute_range([frozen['c002']],voices)
                self.assertEqual('complete',next_chapter['status'])
                self.assertEqual(['c001','c002'],[p['chapter_id'] for p in raw_calls])
                c002_payload=next(p for p in raw_calls if p['chapter_id']=='c002')
                self.assertEqual(['c001'],[r['chapter_id'] for r in c002_payload['prior_context']['records']])
                shared=job_store.prior_memories(root,book,INDEX_SHA,'c003')
                self.assertEqual(['c001','c002'],[row['chapter']['id'] for row in shared])

    def test_current_and_prior_corrections_enter_fake_dispatch_after_trusted_restore(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);root.mkdir(exist_ok=True);raw_calls=[]
            book=SYNTHETIC_BOOK_ID
            # The target c003 is run alone: the c001 note has never had prior memory.
            correction_memory.save_note(root,{'book_id':book,'chapter_id':'c001','text':'合成章节c001'},
                '前文应称为甲角',kind='reader_correction',character_name='甲')
            correction_memory.save_note(root,{'book_id':book,'chapter_id':'c003','text':'合成章节c003'},
                '本章读音为 jiǎ',kind='pronunciation',character_name='甲')
            result,book,payloads,index,_executor,_voices,_frozen=self.run_analysis_only(
                root,ids=('c001','c003'),target='c003',raw_calls=raw_calls)
            self.assertEqual('complete',result['status'])
            self.assertIn(('prepare',book,'c001'),index.calls,
                'correction without prior memory must be authenticated through the trusted index restore')
            self.assertEqual(1,len(raw_calls))
            payload=raw_calls[0]
            corrections=payload['prior_context']['corrections']
            by_chapter={row['chapter_id']:row for row in corrections}
            self.assertEqual({'c001','c003'},set(by_chapter))
            self.assertEqual('前文应称为甲角',by_chapter['c001']['note'])
            self.assertEqual('本章读音为 jiǎ',by_chapter['c003']['note'])
            self.assertEqual(sha(payloads['c001']['text'].encode()),by_chapter['c001']['source_sha256'])
            self.assertEqual(sha(payloads['c003']['text'].encode()),by_chapter['c003']['source_sha256'])
            self.assertEqual('pronunciation',by_chapter['c003']['kind'])

    def test_changed_prior_note_source_blocks_before_fake_dispatch(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);book=SYNTHETIC_BOOK_ID;raw_calls=[]
            correction_memory.save_note(root,{'book_id':book,'chapter_id':'c001','text':'合成章节c001'},
                '旧来源说明',kind='reader_correction')
            # Change the trusted index's source after the note was recorded.
            _result,_book,payloads,index,executor,voices,frozen=self.run_analysis_only(
                root,ids=('c001','c003'),target='c003',raw_calls=raw_calls)
            raw_calls.clear()
            payloads['c001']['text']='来源已变化'
            narrator=create_narrator(root,book,'correction-mismatch')
            # Rebuild against the changed index while retaining the saved old-source note.
            def fake_sequence_run(_sequence_root,plan,callbacks,**_kwargs):
                chapter=dict(plan['chapters'][0],book_id=book,index_sha256=plan['index_sha256'],
                    voice_bindings_sha256=plan['voice_bindings_sha256'])
                return callbacks['analyze'](chapter,[],{})
            with patch.object(runtime,'auto_narrator',return_value=narrator),\
                    patch.object(runtime.experiment.seq,'run',side_effect=fake_sequence_run),\
                    patch.object(runtime.experiment.callbacks.analysis,'build',side_effect=self.fake_analysis_build),\
                    patch('subprocess.run',side_effect=AssertionError('SSH/API must not be called')):
                with self.assertRaisesRegex(ValueError,'correction note source differs'):
                    executor.execute_range([frozen['c003']],voices)
            self.assertEqual([],raw_calls,'source mismatch must stop before dispatch')
            self.assertIn(('prepare',book,'c001'),index.calls)

    def test_note_edit_changes_job_and_context_while_unchanged_context_reuses_fake_payload(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);book=SYNTHETIC_BOOK_ID;raw_calls=[]
            payload={'book_id':book,'chapter_id':'c001','text':'合成章节c001'}
            correction_memory.save_note(root,payload,'第一版说明',kind='reader_correction')
            first,book,_payloads,index,executor,voices,frozen=self.run_analysis_only(
                root,ids=('c001',),target='c001',raw_calls=raw_calls)
            correction_memory.save_note(root,payload,'第二版说明',kind='reader_correction')
            narrator=create_narrator(root,book,'correction-edit')
            # Same verified chapter, revised note; real runtime must bind a new job/context.
            def fake_sequence_run(_sequence_root,plan,callbacks,**_kwargs):
                chapter=dict(plan['chapters'][0],book_id=book,index_sha256=plan['index_sha256'],
                    voice_bindings_sha256=plan['voice_bindings_sha256'])
                callbacks['analyze'](chapter,[],{})
                return {'status':'complete'}
            with patch.object(runtime,'auto_narrator',return_value=narrator),\
                    patch.object(runtime.experiment.seq,'run',side_effect=fake_sequence_run),\
                    patch.object(runtime.experiment.callbacks.analysis,'build',side_effect=self.fake_analysis_build),\
                    patch('subprocess.run',side_effect=AssertionError('SSH/API must not be called')):
                second=executor.execute_range([frozen['c001']],voices)
            self.assertNotEqual(first['job_id'],second['job_id'])
            self.assertEqual(2,len(raw_calls))
            self.assertNotEqual(raw_calls[0],raw_calls[1])
            note_texts=[row['note'] for row in (call['prior_context']['corrections'][0] for call in raw_calls)]
            self.assertEqual(['第一版说明','第二版说明'],note_texts)

    def test_no_notes_keep_prior_payload_stable_and_fake_cache_reuses_it(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);raw_calls=[]
            first,book,_payloads,index,executor,voices,frozen=self.run_analysis_only(
                root,ids=('c001',),target='c001',raw_calls=raw_calls)
            narrator=create_narrator(root,book,'no-note-repeat')
            def fake_sequence_run(_sequence_root,plan,callbacks,**_kwargs):
                chapter=dict(plan['chapters'][0],book_id=book,index_sha256=plan['index_sha256'],
                    voice_bindings_sha256=plan['voice_bindings_sha256'])
                callbacks['analyze'](chapter,[],{})
                return {'status':'complete'}
            with patch.object(runtime,'auto_narrator',return_value=narrator),\
                    patch.object(runtime.experiment.seq,'run',side_effect=fake_sequence_run),\
                    patch.object(runtime.experiment.callbacks.analysis,'build',side_effect=self.fake_analysis_build),\
                    patch('subprocess.run',side_effect=AssertionError('SSH/API must not be called')):
                second=executor.execute_range([frozen['c001']],voices)
            self.assertNotEqual(first['job_id'],second['job_id'])
            self.assertEqual(1,len(raw_calls),'same source and empty correction context must reuse the fake cache')
            self.assertEqual([],raw_calls[0]['prior_context']['corrections'])
            self.assertIn(('list',book,None),index.calls)

    @staticmethod
    def fake_analysis_build(input_path,receipt_dir,voices):
        body=(Path(receipt_dir)/'response.body').read_bytes()
        return {'provenance':{'response_sha256':sha(body)}}

    @staticmethod
    def fake_stage_receipt(input_path,receipt_dir,voices,role_ids,output_dir,*,narrator_id='narrator'):
        payload=json.loads(Path(input_path).read_bytes());text=payload['text'];out=Path(output_dir);out.mkdir()
        path=out/'render.json';path.write_bytes(canonical({'chapters':[{'id':payload['chapter_id'],
            'segments':[{'speaker_id':narrator_id,'text':text}]}],
            'audit':{'source_text_sha256':sha(text.encode())}}))
        return {'path':str(path),'chapter_id':payload['chapter_id'],'human_reviewed':False}

    @staticmethod
    def fake_render_segment(voice,text,folder):
        path=Path(folder)/'rendered.wav'
        with wave.open(str(path),'wb') as handle:
            handle.setnchannels(1);handle.setsampwidth(2);handle.setframerate(24000)
            handle.writeframes(b'\0\0'*10)
        return path

    @staticmethod
    def write_cache_receipt(payload,private_root):
        book=payload['book_id'];cid=payload['chapter_id'];task_root=Path(private_root)
        task=task_root/(book+'-'+cid);task.mkdir(parents=True,exist_ok=True)
        source=task/'input.json';source.write_bytes(canonical(payload))
        receipt=task/'receipt';receipt.mkdir(exist_ok=True)
        parsed={'characters':[],'dialogues':[],'chapter_summary':'synthetic summary '+cid}
        body=canonical({'id':'response-'+cid,'model':'deepseek-flash',
            'usage':{'prompt_tokens':7,'completion_tokens':3,'total_tokens':10},
            'choices':[{'finish_reason':'stop','message':{'content':json.dumps(parsed,ensure_ascii=False)}}]})
        (receipt/'response.body').write_bytes(body)
        (receipt/'intent.json').write_bytes(canonical({'book_id':book,'chapter_id':cid,
            'input_sha256':sha(source.read_bytes()),'request_sha256':sha(b'request')}))
        (receipt/'response.meta.json').write_bytes(canonical({'http_status':200,'body_sha256':sha(body)}))
        (receipt/'result.json').write_bytes(canonical({'status':'needs_review','retry_allowed':False}))


if __name__=='__main__':
    unittest.main()
