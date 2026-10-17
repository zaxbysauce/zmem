"""Bounded synthetic policy experiments on real ZMem writers, never the live store."""
from pathlib import Path
import contextlib
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
RESULTS = HERE / 'results'
RESULTS.mkdir(exist_ok=True)
# Pin before the FIRST storelib import. No ambient host routing or model downloads.
SANDBOX = tempfile.TemporaryDirectory(prefix='hy-zmem-experiments-', dir=RESULTS)
BASE = Path(SANDBOX.name).resolve()
for key in list(os.environ):
    if key.startswith(('ZMEM_', 'CLAUDE_PLUGIN_', 'ZCODE_PLUGIN_')):
        os.environ.pop(key)
os.environ.update(ZMEM_STORE=str(BASE / 'import.sqlite'), ZMEM_DATA=str(BASE),
                  ZMEM_MODELS_DIR=str(BASE / 'missing-models'), ZMEM_MODEL_AUTODOWNLOAD='0')
sys.path.insert(0, str(ROOT / 'skills/memory/scripts'))
# This experiment deliberately exercises the supported model-absent route.
# Deny optional native ML imports as well as model downloads; no inference is needed.
for optional in ('onnxruntime', 'tokenizers', 'numpy', 'torch', 'transformers'):
    sys.modules[optional] = None
from storelib import schema, write, evidence, episodes, beliefs

def connection(label):
    path = BASE / (label + '.sqlite')
    assert path.is_relative_to(BASE)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        schema.init_db(conn)
        schema.migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn

def add(conn, text, subject=None, namespace='project:synthetic/engine', kind='fact', eids=None):
    return str(write.add_memory(conn, namespace=namespace, type_=kind, content=text,
        tags=('task:' + subject) if subject else '', source_ref='session:synthetic-' + (subject or 'unknown'),
        signal='test', capture_mode='auto', link_attr_propagate=False, evidence_ids=eids))

def ev(conn, label):
    eid = evidence.write_evidence(conn, session_id='synthetic-lineage', lane='codex',
        moment='pretool', kind='tool_call', ts='2026-10-17T00:00:00Z', excerpt=label,
        ref_path='synthetic/test-run.txt', ref_offset=0)
    conn.commit()
    return eid

def row(conn, mid):
    return dict(conn.execute('SELECT * FROM memory WHERE id=?', (mid,)).fetchone())

def counts(conn):
    return conn.execute('SELECT COUNT(*) FROM memory WHERE superseded_at IS NULL').fetchone()[0]

observations = []
original_detect = write._detect_duplicate

def pair_case(label, a, b, *, subjects=('issue41', 'issue42'), guarded=False,
              namespaces=('project:synthetic/engine', 'project:synthetic/engine'), controlled_semantic=False):
    conn = connection(label)
    subject_by_id = {}
    incoming = [None]
    guard_rejections = []
    def detect(c, content, ns, dedup_cache=None):
        if controlled_semantic:
            # A deterministic ABOVE-threshold candidate, not a measured embedding.
            candidate = c.execute('SELECT id, content FROM memory WHERE namespace=? AND superseded_at IS NULL LIMIT 1', (ns,)).fetchone()
            hit, score, emb = candidate, 0.97 if candidate else 0.0, None
        else:
            hit, score, emb = original_detect(c, content, ns, dedup_cache)
        if guarded and hit:
            existing_subject = subject_by_id.get(hit['id'])
            if existing_subject and incoming[0] and existing_subject != incoming[0]:
                guard_rejections.append([existing_subject, incoming[0]])
                return None, 0.0, emb
        return hit, score, emb
    try:
        ea, eb = ev(conn, label + ' evidence A'), ev(conn, label + ' evidence B')
        with patch.object(write, '_detect_duplicate', detect):
            incoming[0] = subjects[0]
            first = add(conn, a, subjects[0], namespaces[0], eids=[ea])
            subject_by_id[first] = subjects[0]
            incoming[0] = subjects[1]
            second = add(conn, b, subjects[1], namespaces[1], eids=[eb])
        target = row(conn, second)
        observation = dict(case=label, mode='controlled-semantic-candidate' if controlled_semantic else 'model-absent-exact',
            guarded=guarded, live_rows=counts(conn), same_id=first == second,
            evidence_count_on_returned_row=len(evidence.evidence_ids_for_memory(conn, second)),
            returned_tags=target['tags'], guard_rejections=len(guard_rejections),
            original_source_retained=target['source_ref'] == 'session:synthetic-' + (subjects[0] or 'unknown'))
        observations.append(observation)
        return observation
    finally:
        conn.close()

def lineage_case():
    conn = connection('lineage')
    try:
        original_test = ev(conn, 'Original deadline propagation test passed')
        revised_test = ev(conn, 'Follow-up cancellation behavior test passed')
        old = add(conn, 'Issue 41: propagated the request deadline through the HTTP client.', 'issue41', eids=[original_test])
        lesson = add(conn, 'Always carry a bounded deadline through nested network calls; verify cancellation separately.',
                     'lesson-deadline', kind='lesson', eids=[original_test])
        ep = episodes.episode_open(conn, namespace='project:synthetic/engine')['id']
        episodes.episode_add(conn, episode_id=ep, memory_id=old)
        episodes.episode_add(conn, episode_id=ep, memory_id=lesson)
        new, created = write.update_memory(conn, mid=old,
            content='Issue 41 follow-up: cancellation now reaches the transport and pending work drains.',
            signal='test', capture_mode='auto', link_attr_propagate=False, evidence_ids=[revised_test])
        new = str(new)
        assert created and new != old
        before = row(conn, lesson)
        old_evidence = evidence.evidence_ids_for_memory(conn, old)
        new_evidence = evidence.evidence_ids_for_memory(conn, new)
        lesson_evidence = evidence.evidence_ids_for_memory(conn, lesson)
        membership_before = {r[0] for r in conn.execute('SELECT memory_id FROM episode_memory WHERE episode_id=?', (ep,))}
        # Existing API can explicitly join the replacement to the same task episode.
        episodes.episode_add(conn, episode_id=ep, memory_id=new)
        reached = [r[0] for r in conn.execute('SELECT em2.memory_id FROM episode_memory em1 JOIN episode_memory em2 ON em1.episode_id=em2.episode_id JOIN memory m ON m.id=em2.memory_id WHERE em1.memory_id=? AND m.type="lesson" AND m.superseded_at IS NULL', (new,))]
        closed = episodes.episode_close(conn, episode_id=ep)
        after = row(conn, lesson)
        observations.append(dict(case='evidence-to-lesson-lineage', old_evidence_preserved=old_evidence == [original_test],
            replacement_evidence_separate=new_evidence == [revised_test], lesson_original_evidence=lesson_evidence == [original_test],
            replacement_update_of=row(conn,new)['update_of'] == old,
            old_episode_membership_retained=old in membership_before,
            replacement_automatically_in_episode=new in membership_before,
            existing_episode_add_restores_current_task_join=lesson in reached,
            lesson_signal_unchanged=before['signal'] == after['signal'],
            lesson_confidence_unchanged=before['confidence'] == after['confidence']))
        # Retraction of a source memory does not by itself adjudicate a separately reviewed lesson.
        write.supersede_memory(conn, new, reason='synthetic task evidence later challenged')
        observations.append(dict(case='task-retraction', lesson_still_live=row(conn,lesson)['superseded_at'] is None,
            lesson_original_evidence_preserved=evidence.evidence_ids_for_memory(conn,lesson) == [original_test],
            original_evidence_still_resolvable=conn.execute('SELECT COUNT(*) FROM evidence WHERE id=?',(original_test,)).fetchone()[0] == 1))
    finally:
        conn.close()

def curator_seam():
    conn=connection('curator-seam')
    try:
        eid=ev(conn,'Synthetic source for curator')
        mid=add(conn,'Use a bounded deadline for outbound requests.', 'issue41',kind='lesson',eids=[eid])
        ep=episodes.episode_open(conn,namespace='project:synthetic/engine')['id']
        episodes.episode_add(conn,episode_id=ep,memory_id=mid)
        episodes.episode_close(conn,episode_id=ep)
        calls=[]
        def adapter(payload):
            calls.append(dict(keys=sorted(payload), source_rows=len(payload['source_rows'])))
            return {'actions': []}
        result=beliefs.run_belief_maintenance(conn,llm_local=True,adapter=adapter,now='2026-10-17T01:00:00Z')
        observations.append(dict(case='existing-opt-in-curator-seam', report=result,adapter_calls=calls,
            real_model_called=False, memory_rows=counts(conn)))
    finally:
        conn.close()

try:
    # Model-absent exact policy is fully exercised; controlled semantic cases are separately labelled.
    with patch.object(write, '_embeddings', None), patch.object(schema, '_embeddings', None):
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            common='Request timeout fix completed: deadline propagated, targeted checks passed, review approved.'
            for guarded in (False, True):
                suffix='guard' if guarded else 'baseline'
                pair_case('distinct-task-identical-'+suffix,common,common,guarded=guarded)
                pair_case('same-task-identical-'+suffix,common,common,subjects=('issue41','issue41'),guarded=guarded)
                pair_case('unknown-subject-'+suffix,common,common,subjects=(None,'issue41'),guarded=guarded)
                pair_case('different-namespaces-'+suffix,common,common,guarded=guarded,namespaces=('project:synthetic/a','project:synthetic/b'))
                pair_case('distinct-task-semantic-policy-'+suffix,
                    'Issue 41: deadline propagation fix passed review.', 'Issue 42: deadline propagation fix passed review.',
                    guarded=guarded,controlled_semantic=True)
                pair_case('same-task-semantic-policy-'+suffix,
                    'Issue 41: deadline propagation fix passed review.', 'The HTTP deadline propagation for issue 41 passed review.',
                    subjects=('issue41','issue41'),guarded=guarded,controlled_semantic=True)
            lineage_case()
            curator_seam()
        assert len(output.getvalue()) < 200000
    report={'schema':'hy-zmem-experiments-v1','synthetic_only':True,
        'production_code_modified':False,'model_benchmark':False,'observations':observations,
        'probe_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'source_sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [ROOT/'skills/memory/scripts/storelib'/n for n in ['write.py','evidence.py','episodes.py','beliefs.py']]}}
    (RESULTS/'experiment-results.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2))
finally:
    SANDBOX.cleanup()
