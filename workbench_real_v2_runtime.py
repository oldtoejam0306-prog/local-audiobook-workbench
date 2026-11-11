"""Opt-in GUI runtime configuration. Import never contacts network or executes work."""
import json,subprocess,hashlib,os,shlex
from pathlib import Path
import console_voice_ui as console
import workbench_real as existing
import workbench_real_flow as flow
import release_config
import workbench_real_v2_experiment as experiment
import real_book_auto_voice as auto
import auto_voice_planner
import real_book_sequence as seq
import controlled_book_chapter_v2 as protocol
import threading

ROOT=Path(os.environ['AUDIOBOOK_WORKBENCH_ROOT']).expanduser() if os.environ.get('AUDIOBOOK_WORKBENCH_ROOT') else release_config.local_workbench_root()

def registered_books():
    return {book['display_name']: book['book_id'] for book in existing.registry()}

def narrator_for(name,assignments=None):
 path=Path(assignments) if assignments else console.ROOT/'assignments.json'
 if name not in registered_books():raise ValueError('书籍未登记')
 try:db=json.loads(path.read_bytes());key=name+'\0旁白';record=db[key]
 except (OSError,KeyError,ValueError,TypeError):raise ValueError('未找到本书固定旁白：请先在声音工作台为该书的“旁白”固定声音。')
 binding={'console_key':key,'record':record}
 verify=experiment.fixed_record_verifier(path,{registered_books()[name]:name},{'narrator':'旁白'})
 # fixed_record_verifier also checks the current vector and current file record.
 voices={'narrator':{'id':'narrator','voice':'preset:test'},'characters':{}}
 if not verify(registered_books()[name],voices,{'narrator':binding}):raise ValueError('本书旁白记录或声音向量无效，请在声音工作台核对。')
 return binding,verify

def legacy_check(book,chapter,runner=subprocess.run):
 mode=release_config.legacy_chapter_mode()
 if mode=='none':return
 payload={'action':'check','book_id':book,'chapter_id':chapter,
          'legacy_root':str(release_config.remote_legacy_chapter_root())}
 p=runner(['ssh',*release_config.ssh_options(),release_config.ssh_target(),
           release_config.remote_command('legacy_chapter_state.py')],
          input=json.dumps(payload,ensure_ascii=False),capture_output=True,text=True,
          encoding='utf8',timeout=25,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
 try:response=json.loads(p.stdout)
 except (ValueError,TypeError):response={}
 if p.returncode or not isinstance(response,dict) or response.get('status')!='clear':
  raise ValueError('旧章节任务运行中或状态无法核实；请检查 Mac 旧工作台。')

def auto_narrator(book,root=ROOT,generator=auto.generate_local_fictional):
 """Use a versioned book-scoped narrator design without replacing old voices."""
 if book not in registered_books().values():raise ValueError('书籍未登记')
 root=Path(root)
 selected_path=root/'narrators'/book/'selected-voices.json'
 if selected_path.exists():
  selected=auto._read_selections(root/'narrators',book)['voices'].get('narrator')
  if selected:
   binding=selected['binding']
   if binding['console_key']!=book+'\0narrator':raise ValueError('旁白选中声音身份不匹配。')
   return binding
 profile_path=root/'character-profiles'/book/'current.json'
 try:
  document=json.loads(profile_path.read_text(encoding='utf-8'))
 except (OSError,ValueError) as exc:
  raise ValueError('请先生成本书人物档案及旁白声音设计。') from exc
 if document.get('book_id')!=book:
  raise ValueError('人物档案书籍身份不匹配。')
 auto_voice_planner.plans_from_character_profiles(document)
 design=document.get('narration_design')
 if (not isinstance(design,dict) or not isinstance(design.get('description'),str)
     or not design['description'].strip() or not isinstance(design.get('rationale'),str)
     or not design['rationale'].strip()):
  raise ValueError('本书旁白声音设计缺失，请先完成设计。')
 description=auto.creative_description('为中文有声书设计虚构旁白的表演声音。'
     +design['description'].strip()+'以上声音表演属于创作选择，不模仿真实人物。')
 specification={'description':description,'rationale':design['rationale'],
                'source':document.get('source')}
 spec_hash=hashlib.sha256(protocol.canonical(specification)).hexdigest()
 directory=root/'narrators'/book/'candidates'/spec_hash[:16]
 directory.mkdir(parents=True,exist_ok=True)
 record_path=directory/'narrator.json';intent=directory/'generation.reserved.json'
 if record_path.exists():
  result=json.loads(record_path.read_bytes());binding=result['binding']
  if (result.get('book_id')!=book or result.get('human_reviewed') is not False
      or result.get('design_sha256')!=spec_hash):raise ValueError('旁白记录身份或设计版本无效')
  auto._validate_record(binding['record'])
  if binding['console_key']!=book+'\0narrator' or binding['record'].get('book_role_key')!=binding['console_key']:raise ValueError('旁白记录身份无效')
  auto._save_recommendation(root/'narrators',book,'narrator',binding,specification,spec_hash[:16])
  return binding
 try:
  with intent.open('xb') as f:f.write(protocol.canonical({'book_id':book,'design_sha256':spec_hash,'status':'generation_may_have_occurred','human_reviewed':False}));f.flush();os.fsync(f.fileno())
 except FileExistsError:raise ValueError('本书旁白生成结果未知，请先核查私有记录；不会自动重试。')
 record=generator(description,directory)
 if not Path(record['directory']).resolve().is_relative_to(directory.resolve()):raise ValueError('旁白输出不在私有目录')
 auto._validate_record(record);key=book+'\0narrator';binding={'console_key':key,'record':dict(record,book_role_key=key)}
 with record_path.open('xb') as f:f.write(protocol.canonical({'book_id':book,'binding':binding,'design_sha256':spec_hash,'design':specification,'human_reviewed':False}));f.flush();os.fsync(f.fileno())
 auto._save_recommendation(root/'narrators',book,'narrator',binding,specification,spec_hash[:16])
 return binding

def make_executor(name, assignments=None, legacy_guard=legacy_check, *,
                  narrator_generator=auto.generate_local_fictional, root=None,
                  index_request=None, dispatch=None, profile_ensurer=None):
    from analysis_dispatch_cache import CachedAnalysisDispatcher
    import character_profile_jobs
    import workbench_job_store as store
    import correction_memory

    book = registered_books()[name]
    base = Path(ROOT if root is None else root).resolve()
    request = index_request or flow.index_request
    dispatcher = dispatch or CachedAnalysisDispatcher(base)
    ensure_profiles = profile_ensurer or character_profile_jobs.ensure_profiles_for_chapter
    local_legacy = base / 'local-legacy-snapshot'
    (local_legacy / 'jobs').mkdir(parents=True, exist_ok=True)
    voices = {'mode': 'automatic_fictional_narrator_v1', 'book_id': book, 'human_reviewed': False}
    stop = threading.Event()

    def execute_range(frozen, selected_voices):
        if not frozen or selected_voices != voices:
            raise ValueError('自动声音配置已变化，请重新预览。')
        stop.clear()
        index_sha = frozen[0][0]['index_sha256']
        for identity, payload in frozen:
            if (identity.get('book_id') != book or identity.get('index_sha256') != index_sha
                    or identity.get('voice_mapping_sha256') != seq.fingerprint(voices)):
                raise ValueError('章节身份不一致')
            legacy_guard(book, identity['chapter_id'])
            fresh = request('prepare', book, identity['chapter_id'])
            if (fresh.get('index_sha256') != index_sha
                    or fresh.get('payload_sha256') != identity['source_sha256']
                    or experiment.workflow._bytes(fresh.get('payload')) != experiment.workflow._bytes(payload)):
                raise ValueError('章节索引已变化，请重新预览。')
        with auto._selection_lock(base / 'execution-locks', book):
            profile_path = base / 'character-profiles' / book / 'current.json'
            if not profile_path.is_file():
                ensure_profiles(frozen[0][1], root=base)
            narrator = auto_narrator(book, root=base, generator=narrator_generator)

            def verify(b, v, bindings):
                try:
                    return (b == book and bindings == {'narrator': narrator}
                            and auto_narrator(book, root=base) == narrator)
                except (OSError, ValueError, KeyError):
                    return False

            actual_voices = {'narrator': {'id': 'narrator', 'voice': 'preset:test'},
                             'characters': {}, 'auto_narrator_sha256': seq.fingerprint(narrator)}
            selected = auto._read_selections(base / 'auto-voices', book)
            code_files = ['real_book_renderer.py', 'console_chapter_render_mac.py', 'native_unit_cache.py', 'natural_pacing.py', 'delivery_policy.py',
                          'real_book_auto_callbacks.py', 'real_pipeline_context_worker.mjs',
                          'auto_voice_planner.py', 'character_profile_jobs.py', 'character_profile_protocol.mjs',
                          'profile_json_recovery.mjs',
                          'real_book_auto_voice.py', 'console_voice_ui.py',
                          'real_pipeline_analysis.py','analysis_dispatch_cache.py','attribution_result_validation.py',
                          'context_compaction.py','correction_memory.py','real_book_sequence_callbacks.py',
                          'workbench_real_v2_experiment.py','real_book_sequence.py','workbench_real_v2_runtime.py',
                          'workbench_job_store.py']
            code_pins = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                         for name in code_files}
            configuration = {'selected_characters': selected,
                             'profile_sha256': hashlib.sha256(profile_path.read_bytes()).hexdigest(),
                             'code_pins': code_pins}
            notes=correction_memory.read_notes(base,book,frozen[-1][0]['chapter_id'],include_current=True)
            configuration['correction_notes_sha256']=seq.fingerprint(notes)
            actual_voices['execution_configuration'] = configuration
            chapters = [{'id': identity['chapter_id'],
                         'source_sha256': hashlib.sha256(payload['text'].encode('utf-8')).hexdigest()}
                        for identity, payload in frozen]
            job = store.begin_job(base, book, index_sha, chapters, seq.fingerprint(actual_voices))
            result = experiment.run_experiment(job['job_root'], local_legacy, book,
                [c['id'] for c in chapters], actual_voices, {'narrator': narrator}, verify,
                single_user_ack=True, index_request=request, dispatch=dispatcher,
                auto_narrator=narrator, shared_root=base, stop_requested=stop.is_set)
            return dict(result, job_id=job['job_id'], job_root=str(job['job_root']),
                        sequence_root=str(job['sequence_root']))

    def execute(identity, payload, selected_voices):
        return execute_range([(identity, payload)], selected_voices)

    execute.execute_range = execute_range
    execute.request_stop = stop.set
    return voices, execute
