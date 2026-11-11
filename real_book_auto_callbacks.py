"""Sequential chapter callbacks with isolated automatic fictional voices.

Requires trusted index/source pins, existing narrator, and a renderer/verifier.
Identity matching is conservative and all candidates remain unreviewed.
"""
import hashlib
import json
import os
import re
from pathlib import Path
import real_book_sequence as seq
import real_book_sequence_callbacks as base
import real_book_auto_voice as auto
import auto_voice_planner
import real_pipeline_workflow as workflow
import character_profile_jobs
from character_fusion import _linked


class AutoBridge(base.Bridge):
    def __init__(self, *args, voice_root, narrator, auto_renderer, auto_verify,
                 generator=auto.generate_local_fictional, profile_ensurer=None,
                 profile_dispatcher=None, **kwargs):
        super().__init__(*args, renderer=lambda *x: None, verify_renderer=lambda *x: False, **kwargs)
        self.voice_root = Path(voice_root)
        self.narrator = narrator
        self.auto_renderer = auto_renderer
        self.auto_verify = auto_verify
        self.generator = generator
        self.profile_ensurer = profile_ensurer or character_profile_jobs.ensure_profiles_for_chapter
        self.profile_dispatcher = profile_dispatcher
        if not callable(auto_renderer) or not callable(auto_verify): raise ValueError('verified renderer required')

    def memory(self, chapter, prior, artifacts):
        original = super().memory(chapter, prior, artifacts)
        memory = base._read(**original['artifact'])
        analyze = artifacts['analyze']
        payload = base._read(analyze['input_path'], analyze['input_sha256'])
        if (payload.get('book_id') != chapter['book_id'] or payload.get('chapter_id') != chapter['id']
                or hashlib.sha256(payload.get('text', '').encode('utf8')).hexdigest() != chapter['source_sha256']):
            raise ValueError('profile discovery source does not match verified chapter')
        profile_root = self.voice_root.parent
        registry_path = profile_root/'character-profiles'/chapter['book_id']/'current.json'
        profile_snapshot = None

        def load_profiles():
            nonlocal profile_snapshot
            if not registry_path.is_file():
                return None
            raw = registry_path.read_bytes()
            doc = json.loads(raw.decode('utf-8-sig'))
            if (doc.get('book_id') != chapter['book_id'] or doc.get('mode') != 'character_profiles_v1'
                    or not isinstance(doc.get('characters'), list)):
                raise ValueError('人物画像注册表协议或书籍身份不匹配')
            version_dir = registry_path.parent/'versions'
            if version_dir.is_symlink():
                raise ValueError('人物画像版本目录不能是符号链接')
            version_dir.mkdir(exist_ok=True)
            if version_dir.is_symlink() or not version_dir.is_dir():
                raise ValueError('人物画像版本目录无效')
            version_sha = hashlib.sha256(raw).hexdigest()
            version_path = version_dir/(version_sha+'.json')
            if version_path.is_symlink():
                raise ValueError('人物画像版本快照不能是符号链接')
            try:
                with version_path.open('xb') as stream:
                    stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            except FileExistsError:
                if version_path.is_symlink() or version_path.read_bytes() != raw:
                    raise ValueError('人物画像版本快照摘要冲突')
            profile_snapshot = {'path':str(version_path),'sha256':version_sha}
            return doc

        profile_document = load_profiles()
        known_profile_labels = set()
        if profile_document:
            for item in profile_document['characters']:
                if not isinstance(item, dict):
                    continue
                if isinstance(item.get('name'), str):
                    known_profile_labels.add(item['name'])
                for alias in item.get('aliases', []):
                    if isinstance(alias, dict) and isinstance(alias.get('name'), str):
                        known_profile_labels.add(alias['name'])
        candidates = memory.get('characters', [])
        new_names = [c['name'] for c in candidates if c.get('name') not in known_profile_labels]
        source_sha = chapter['source_sha256']
        chapter_is_current = bool(profile_document and any(
            row.get('chapter_id') == chapter['id'] and row.get('text_sha256') == source_sha
            for row in profile_document.get('source', {}).get('chapters', []) if isinstance(row, dict)))
        full_chapter = re.fullmatch(r'c(\d+)', chapter['id'])
        refresh_due = bool(full_chapter and int(full_chapter.group(1)) % 5 == 0 and not chapter_is_current)
        should_ensure = profile_document is None or bool(new_names) or refresh_due
        ensure_result = {'status': 'not_needed'}
        if should_ensure:
            try:
                ensure_payload = payload
                if new_names:
                    ensure_payload = dict(payload)
                    ensure_payload['profile_required_names'] = sorted(new_names)
                ensure_result = self.profile_ensurer(ensure_payload, root=profile_root, dispatcher=self.profile_dispatcher)
            except Exception as exc:
                failure=getattr(exc,'result',{})
                detail={'reason':'profile_ensure_failed','status':getattr(exc,'status','unknown'),
                    'retry_allowed':False,'artifact_path':getattr(exc,'artifact_path',None),
                    'usage':failure.get('usage') if isinstance(failure,dict) else None,
                    'failure_status':failure.get('failure_status') if isinstance(failure,dict) else None}
                blocked={'book_id':chapter['book_id'],'chapter_id':chapter['id'],'entities':[],'bindings':{},
                    'unresolved':[detail],'blocking_conflicts':[detail],'blocked':True,'human_reviewed':False,
                    'profile_ensure':detail}
                pointer=base._save(self._dir(chapter)/'auto-voices.json',blocked)
                return {'status':'ok','artifact':dict(original['artifact'],auto=pointer,
                    profile_blocked=True,blocking_conflicts=[detail])}
            if not isinstance(ensure_result, dict) or ensure_result.get('status') not in ('needs_review', 'already_current'):
                raise ValueError('人物画像补档未确认完成，阻止声音分配')
            profile_document = load_profiles()
        if profile_document is None:
            raise ValueError('缺少人物画像注册表，阻止声音分配')

        known = {}
        for item in prior:
            previous = item['artifacts']['memory']
            data = base._read(**previous['auto'])
            if data.get('book_id') != chapter['book_id']:
                raise ValueError('cross-book prior identity')
            for ent in data['entities']:
                for label in ent['labels']:
                    known.setdefault(label, set()).add(ent['entity_id'])
        entities=[]; profiles=[]; unresolved=[]
        blockers=[]
        profile_rows=[p for p in profile_document['characters'] if isinstance(p,dict) and isinstance(p.get('name'),str)]
        exact_names={}
        alias_owners={}
        for p in profile_rows:
            exact_names.setdefault(p['name'],[]).append(p)
            for alias in p.get('aliases',[]) if isinstance(p.get('aliases'),list) else []:
                if not isinstance(alias,dict) or not isinstance(alias.get('name'),str):
                    continue
                safe_evidence=[e.get('quote') for e in alias.get('evidence',[]) if isinstance(e,dict)
                               and isinstance(e.get('quote'),str)
                               and _linked(e.get('quote'),p['name'],alias['name'])]
                if safe_evidence:
                    alias_owners.setdefault(alias['name'],[]).append({'profile':p,'evidence':safe_evidence})
        ambiguous_aliases={row.get('alias') for row in profile_document.get('alias_conflicts',[])
                           if isinstance(row,dict) and isinstance(row.get('alias'),str)}
        ambiguous_aliases.update(name for name,owners in alias_owners.items()
                                 if len({o['profile']['name'] for o in owners})>1)

        def find_profile(name):
            if name in ambiguous_aliases:
                return None, 'profile_alias_conflict'
            exact=exact_names.get(name,[])
            if len(exact)==1:
                return exact[0], None
            if len(exact)>1:
                return None, 'duplicate_profile_name'
            owners=alias_owners.get(name,[])
            if len(owners)==1:
                return owners[0]['profile'], None
            if len(owners)>1:
                return None, 'profile_alias_conflict'
            return None, 'profile_missing_for_character'

        for c in candidates:
            linked = [alias for alias in c['aliases'] if _linked(c['evidence'], c['name'], alias)]
            unsupported = [alias for alias in c['aliases'] if alias not in linked]
            linked_conflicts=[alias for alias in linked if alias in ambiguous_aliases]
            if linked_conflicts:
                row={'name':c['name'],'reason':'profile_alias_conflict','aliases':linked_conflicts,'blocking':True}
                unresolved.append(row);blockers.append(row);continue
            profile, profile_error = find_profile(c['name'])
            if profile_error:
                row={'name':c['name'],'reason':profile_error,'blocking':True}
                unresolved.append(row);blockers.append(row);continue
            # An unsupported alias touching an existing label is a blocking
            # identity conflict, never evidence to merge or create a new voice.
            if any(alias in known for alias in unsupported):
                row={'name': c['name'], 'reason': 'unproven_existing_alias','blocking':True}
                unresolved.append(row);blockers.append(row);continue
            if unsupported:
                unresolved.append({'name': c['name'], 'reason': 'unproven_alias_ignored', 'aliases': unsupported})
            profile_name=profile['name']
            profile_aliases=[alias['name'] for alias in profile.get('aliases',[])
                if isinstance(alias,dict) and alias.get('name') in alias_owners
                and alias.get('name') not in ambiguous_aliases
                and any(o['profile']['name']==profile_name for o in alias_owners[alias['name']])]
            labels=sorted(set([c['name'],profile_name,*linked,*profile_aliases]))
            matches=set().union(*(known.get(name,set()) for name in labels))
            if len(matches)>1:
                row={'name':c['name'],'reason':'multiple_existing_entities','possible_entities':sorted(matches),'blocking':True}
                unresolved.append(row);blockers.append(row);continue
            entity=next(iter(matches)) if matches else 'e'+hashlib.sha256((chapter['book_id']+'\0'+profile_name).encode()).hexdigest()[:24]
            # Reuse existing canonical name for explicit, source-supported aliases.
            previous_entity = next((e for item in prior for e in base._read(**item['artifacts']['memory']['auto'])['entities'] if e['entity_id']==entity), None)
            canonical = previous_entity['name'] if previous_entity else profile_name
            names=set(labels)
            if previous_entity: names.update(previous_entity['labels'])
            entities.append({'entity_id':entity,'name':canonical,'labels':sorted(names), 'chapter_name':c['name']})
            design=profile.get('voice_design',{})
            if isinstance(design,dict) and design.get('mode')=='narrator_only':
                unresolved.append({'name':c['name'],'reason':'narrator_only_no_direct_character_voice'})
                continue
            if (not isinstance(design,dict) or not isinstance(design.get('description'),str)
                    or not design['description'].strip() or not isinstance(design.get('rationale'),str)
                    or not design['rationale'].strip() or not isinstance(design.get('creative_choices'),list)
                    or not design['creative_choices']):
                row={'name':c['name'],'reason':'profile_voice_design_missing','blocking':True}
                unresolved.append(row);blockers.append(row);continue
            profiles.append({'entity_id':entity,'name':profile_name,'evidence':c['evidence'],
                'aliases':[{'name':name,'evidence':c['evidence']} for name in labels if name!=profile_name and name in c['evidence']]})
        # Duplicate same-entity candidates are ambiguous rather than overwritten.
        counts={e['entity_id']:sum(x['entity_id']==e['entity_id'] for x in entities) for e in entities}
        bad={k for k,v in counts.items() if v>1}
        if bad:
            rows=[{'entity_id':i,'reason':'multiple_candidates_same_entity','blocking':True} for i in sorted(bad)]
            unresolved.extend(rows);blockers.extend(rows)
            entities=[e for e in entities if e['entity_id'] not in bad];profiles=[p for p in profiles if p['entity_id'] not in bad]
        if profile_snapshot is None:
            raise ValueError('人物画像版本快照不存在')
        if blockers:
            blocked={'book_id':chapter['book_id'],'chapter_id':chapter['id'],'entities':entities,'bindings':{},
                'unresolved':unresolved,'blocking_conflicts':blockers,'blocked':True,'human_reviewed':False,
                'profile_registry':dict(profile_snapshot),'profile_ensure':ensure_result}
            pointer=base._save(self._dir(chapter)/'auto-voices.json',blocked)
            return {'status':'ok','artifact':dict(original['artifact'],auto=pointer,
                profile_blocked=True,blocking_conflicts=blockers)}

        plan_document=auto_voice_planner.plans_from_character_profiles(profile_document)
        by_label={}
        for item in profile_document['characters']:
            labels={item.get('name')}
            for alias in item.get('aliases',[]):
                if isinstance(alias,dict) and alias.get('name') and alias.get('evidence'):
                    labels.add(alias['name'])
            for label in labels:
                by_label.setdefault(label,[]).append(item)
        plans_by_name={p['character']:p for p in plan_document['plans']}
        for profile in profiles:
            matches=[]
            for label in [profile['name'], *[a['name'] for a in profile['aliases']]]:
                for item in by_label.get(label,[]):
                    if item not in matches: matches.append(item)
            if len(matches)!=1:
                raise ValueError(f"{profile['name']}：人物画像缺失或匹配不唯一，阻止声音候选生成")
            source_profile=matches[0]; plan=plans_by_name[source_profile['name']]
            profile.update({'design_text':plan['design_text'],'spec_id':plan['spec_id'],
                'voicing':plan['voicing'],
                'profile_sha256':plan['profile_sha256'],'source_evidence':plan['profile_evidence'],
                'personality':';'.join(x['value'] for x in plan['personality_behavior_speech']['personality']),
                'design_rationale':plan['design_rationale']})
        plan_pointer=base._save(self._dir(chapter)/'voice-design-plan.json',plan_document)
        allocation=auto.allocate(self.voice_root,chapter['book_id'],profiles,self.narrator,self.generator)
        allocation_status=allocation.get('status')
        allocation_unresolved=allocation.get('unresolved',[])
        if not isinstance(allocation_unresolved,list):
            raise ValueError('voice allocator unresolved results invalid')
        if allocation_status!='complete' and not allocation_unresolved:
            allocation_unresolved=[{'reason':'allocator_'+str(allocation_status or 'unknown'),
                                    'retry_allowed':False}]
        # Every allocator failure means at least one identity has no safe voice
        # binding. Preserve the full entity and unresolved evidence, block this
        # chapter from staging, and never let it fall through to narrator.
        allocation_blockers=[dict(item,blocking=True) for item in allocation_unresolved if isinstance(item,dict)]
        if len(allocation_blockers)!=len(allocation_unresolved):
            raise ValueError('voice allocator unresolved row invalid')
        unresolved.extend(allocation_unresolved)
        blocked=allocation_status!='complete' or bool(allocation_unresolved)
        doc={'book_id':chapter['book_id'],'chapter_id':chapter['id'],'entities':entities,
             'bindings':allocation.get('bindings',{}),'unresolved':unresolved,'human_reviewed':False,
             'voice_design_plan':plan_pointer,'profile_registry':dict(profile_snapshot),
             'profile_ensure':ensure_result,'blocked':blocked,
             'blocking_conflicts':allocation_blockers}
        if memory.get('execution_kind') == 'derived_locally':
            doc.update({'execution_kind':'derived_locally',
                'original_response_sha256':memory['original_response_sha256'],
                'original_meta_sha256':memory['original_meta_sha256'],
                'recovery_binding_sha256':memory['recovery_binding_sha256'],
                'usage_source':'original_saved_provider_receipt'})
        pointer=base._save(self._dir(chapter)/'auto-voices.json',doc)
        return {'status':'ok','artifact':dict(original['artifact'],auto=pointer,profile_blocked=blocked,
            blocking_conflicts=allocation_blockers,
            profile_registry=dict(profile_snapshot),profile_ensure=ensure_result)}

    def stage(self, chapter, prior, artifacts):
        auto_doc=base._read(**artifacts['memory']['auto'])
        if auto_doc.get('blocked') is True:
            raise ValueError('人物画像或身份冲突未解决；章节未进入声音映射')
        a=artifacts['analyze']
        base._read(a['input_path'],a['input_sha256'])
        if base._hash((Path(a['receipt_dir'])/'response.body').read_bytes())!=a['response_sha256']: raise ValueError('response changed')
        characters={e['chapter_name']:e['entity_id'] for e in auto_doc['entities'] if e['entity_id'] in auto_doc['bindings']}
        voices={'narrator':'preset:test','characters':{name:'preset:test' for name in characters}}
        result=workflow.stage_receipt(a['input_path'],a['receipt_dir'],voices,characters,self._dir(chapter)/'stage',narrator_id='narrator')
        p=Path(result['path']); artifact={'path':str(p),'sha256':base._hash(p.read_bytes()),
            'auto_sha256':artifacts['memory']['auto']['sha256']}
        if auto_doc.get('execution_kind') == 'derived_locally':
            artifact.update({'execution_kind':'derived_locally',
                'original_response_sha256':auto_doc['original_response_sha256'],
                'original_meta_sha256':auto_doc['original_meta_sha256'],
                'recovery_binding_sha256':auto_doc['recovery_binding_sha256'],
                'usage_source':'original_saved_provider_receipt'})
        return {'status':'ok','artifact':artifact}

    def render(self, chapter, prior, artifacts):
        stage=artifacts['stage'];base._read(stage['path'],stage['sha256'])
        pointer=artifacts['memory']['auto']
        if pointer['sha256']!=stage['auto_sha256']: raise ValueError('stage voice freeze mismatch')
        doc=base._read(**pointer)
        for binding in doc['bindings'].values(): auto._validate_record(binding['record'])
        completed_path=self._dir(chapter)/'render-receipt.json'
        if completed_path.is_file():
            completed=json.loads(completed_path.read_bytes())
            if (completed.get('auto_sha256')!=pointer['sha256'] or completed.get('human_reviewed') is not False
                    or self.auto_verify(completed.get('receipt'),stage['path'],doc['bindings'],chapter) is not True):
                raise ValueError('existing render receipt does not match the frozen chapter')
            return {'status':'ok','artifact':{'path':str(completed_path),'sha256':base._hash(completed_path.read_bytes())}}
        receipt=self.auto_renderer(stage['path'],doc['bindings'],chapter)
        if self.auto_verify(receipt,stage['path'],doc['bindings'],chapter) is not True: raise ValueError('render verification failed')
        final={'receipt':receipt,'auto_sha256':pointer['sha256'],'human_reviewed':False}
        if doc.get('execution_kind') == 'derived_locally':
            final.update({'execution_kind':'derived_locally',
                'original_response_sha256':doc['original_response_sha256'],
                'original_meta_sha256':doc['original_meta_sha256'],
                'recovery_binding_sha256':doc['recovery_binding_sha256'],
                'usage_source':'original_saved_provider_receipt'})
        return {'status':'ok','artifact':base._save(self._dir(chapter)/'render-receipt.json',final)}
