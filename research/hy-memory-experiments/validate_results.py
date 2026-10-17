"""Small deterministic receipt check; no model loads or store writes."""
from pathlib import Path
import hashlib
import json
import shutil

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
RESULTS=HERE/'results'
def read(name):
    p=RESULTS/name
    assert p.stat().st_size < 100000
    return json.loads(p.read_text(encoding='utf-8'))
policy=read('experiment-results.json')
native=read('native-semantic-results.json')
exact=read('real-semantic-results.json')
assert native['index']=='native sqlite-vec'
assert native['observations']==exact['observations']
assert len(native['observations'])==12
rows={(r['case'],r['guarded']):r for r in native['observations']}
for case in ('task-closeout','issue-number','different-files'):
    assert rows[case,False]['cosine'] >= .85
    assert rows[case,False]['live_rows']==1 and rows[case,True]['live_rows']==2
for case in ('same-general-lesson','unknown-subject'):
    assert rows[case,False]['live_rows']==rows[case,True]['live_rows']==1
assert rows['same-task-paraphrase',False]['live_rows']==rows['same-task-paraphrase',True]['live_rows']==2
p={r['case']:r for r in policy['observations']}
lineage=p['evidence-to-lesson-lineage']
assert all(v for k,v in lineage.items() if k not in ('case','replacement_automatically_in_episode'))
assert lineage['replacement_automatically_in_episode'] is False
assert p['existing-opt-in-curator-seam']['report']['actions_applied']==0
assert len(p['existing-opt-in-curator-seam']['adapter_calls'])==1
assert policy['probe_sha256']==hashlib.sha256((HERE/'run_experiments.py').read_bytes()).hexdigest()
for name,digest in policy['source_sha256'].items():
    assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest
# The first failed process is terminal and its Job cleanup is verified in its receipt.
# Remove only that owned scratch directory, after resolving it inside the expected root.
failed=RESULTS/'hy-zmem-experiments-wy5qphr3'
if failed.exists():
    assert failed.resolve().is_relative_to(RESULTS.resolve()) and not failed.is_symlink()
    shutil.rmtree(failed)
summary=dict(validated=True,native_pairs=6,policy_observations=len(policy['observations']),
    native_and_exact_decisions_equal=True,production_source_hashes_verified=True,
    probe_sha256=policy['probe_sha256'],real_semantic_driver_sha256=hashlib.sha256((HERE/'real_semantic.py').read_bytes()).hexdigest(),
    independent_critic='HOST_MEMORY_GUARD-no-verdict')
(RESULTS/'validation.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
print(json.dumps(summary,indent=2))
