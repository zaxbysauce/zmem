"""Real cached MiniLM vectors, exact cosine KNN, real add_memory; no network."""
from pathlib import Path
import contextlib
import hashlib
import io
import json
import math
import os
import sqlite3
import struct
import sys
import tempfile
from unittest.mock import patch

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
RESULTS=HERE/'results'
RESULTS.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(prefix='hy-real-semantic-',dir=RESULTS) as scratch:
    base=Path(scratch).resolve()
    for key in list(os.environ):
        if key.startswith(('ZMEM_','CLAUDE_PLUGIN_','ZCODE_PLUGIN_')):
            os.environ.pop(key)
    os.environ.update(ZMEM_STORE=str(base/'store.sqlite'),ZMEM_DATA=str(base),
        ZMEM_MODELS_DIR='C:/Users/Brett/.zmem/models',ZMEM_MODEL_AUTODOWNLOAD='0',
        TOKENIZERS_PARALLELISM='false',OMP_NUM_THREADS='2',OPENBLAS_NUM_THREADS='2')
    sys.path.insert(0,str(ROOT/'skills/memory/scripts'))
    import onnxruntime as ort
    original_session=ort.InferenceSession
    def limited_session(*args,**kwargs):
        options=ort.SessionOptions()
        options.intra_op_num_threads=2
        options.inter_op_num_threads=1
        kwargs['sess_options']=options
        return original_session(*args,**kwargs)
    # Limit thread/memory fanout; model bytes, tokenizer and embedding algorithm unchanged.
    ort.InferenceSession=limited_session
    import embeddings
    from storelib import schema,write
    assert embeddings.is_available(), 'cached verified real embedding model unavailable'
    pairs=[
        ('task-closeout','issue41','issue42',
         'Request timeout fix completed: deadline propagated, targeted checks passed, review approved.',
         'Request timeout fix completed: cancellation propagated, targeted checks passed, review approved.'),
        ('issue-number','issue41','issue42','Issue 41: deadline propagation fix passed targeted checks and review.',
         'Issue 42: deadline propagation fix passed targeted checks and review.'),
        ('same-task-paraphrase','issue41','issue41',
         'The request timeout fix propagates the deadline through the HTTP client.',
         'The HTTP client now receives the propagated request deadline.'),
        ('different-files','file:client.py','file:worker.py',
         'Fixed client.py timeout handling by propagating the request deadline; targeted tests passed.',
         'Fixed worker.py timeout handling by propagating the request deadline; targeted tests passed.'),
        ('same-general-lesson','lesson:deadline','lesson:deadline',
         'Always propagate request deadlines through nested network calls.',
         'Always propagate request deadlines through nested network calls.'),
        ('unknown-subject',None,'issue41','Use a bounded deadline for outbound network requests.',
         'Use a bounded deadline for outbound network requests.'),
    ]
    vectors={text:embeddings.embed_text(text) for pair in pairs for text in pair[3:]}
    assert all(v is not None for v in vectors.values())
    def unpack(blob): return struct.unpack('<'+'f'*(len(blob)//4),blob)
    def cosine(a,b):
        av,bv=unpack(a),unpack(b)
        return sum(x*y for x,y in zip(av,bv))/(math.sqrt(sum(x*x for x in av))*math.sqrt(sum(y*y for y in bv)))
    native_vec='--native-vec' in sys.argv
    assert Path(schema.STORE_PATH).resolve() == (base/'store.sqlite').resolve()
    conn=schema.connect() if native_vec else sqlite3.connect(base/'store.sqlite')
    conn.row_factory=sqlite3.Row
    try:
        schema.init_db(conn);schema.migrate(conn)
        if native_vec:
            assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='memory_vec'").fetchone()[0] == 1
        def exact_knn(c,embedding,*,namespaces,k,overfetch=None):
            rows=c.execute('SELECT id,embedding FROM memory WHERE namespace=? AND superseded_at IS NULL AND embedding IS NOT NULL',(namespaces[0],)).fetchall()
            return sorted([(r['id'],1-cosine(embedding,r['embedding'])) for r in rows],key=lambda x:x[1])[:k]
        original_detect=write._detect_duplicate
        observations=[]
        subject_by_id={}
        active_subject=[None]
        for guarded in (False,True):
            for label,sa,sb,a,b in pairs:
                ns='project:synthetic/'+label+('-guard' if guarded else '-baseline')
                def detect(c,content,namespace,dedup_cache=None):
                    hit,score,emb=original_detect(c,content,namespace,dedup_cache)
                    if guarded and hit and subject_by_id.get(hit['id']) and active_subject[0] and subject_by_id[hit['id']]!=active_subject[0]:
                        return None,0.0,emb
                    return hit,score,emb
                knn_context=contextlib.nullcontext() if native_vec else patch.object(write,'_vec_knn_in_namespace',exact_knn)
                with knn_context,patch.object(write,'_detect_duplicate',detect),contextlib.redirect_stdout(io.StringIO()):
                    active_subject[0]=sa
                    first=str(write.add_memory(conn,namespace=ns,type_='fact',content=a,tags='task:'+str(sa),signal='test',link_attr_propagate=False))
                    subject_by_id[first]=sa
                    active_subject[0]=sb
                    second=str(write.add_memory(conn,namespace=ns,type_='fact',content=b,tags='task:'+str(sb),signal='test',link_attr_propagate=False))
                observations.append(dict(case=label,guarded=guarded,cosine=round(cosine(vectors[a],vectors[b]),6),
                    same_id=first==second,live_rows=conn.execute('SELECT COUNT(*) FROM memory WHERE namespace=? AND superseded_at IS NULL',(ns,)).fetchone()[0]))
        report=dict(schema='hy-zmem-real-semantic-v1',model='cached-minilm-onnx',
            model_sha256=hashlib.file_digest(Path('C:/Users/Brett/.zmem/models/minilm.onnx').open('rb'),'sha256').hexdigest(),
            index='native sqlite-vec' if native_vec else 'exact-cosine-test-seam replaces sqlite-vec indexing only',threads=[2,1],
            synthetic_pairs=True,observations=observations,
            limitation='Small constructed corpus; not held-out quality or prevalence measurement. Guard uses explicit oracle subjects, not extraction heuristics.')
        destination='native-semantic-results.json' if native_vec else 'real-semantic-results.json'
        (RESULTS/destination).write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(report,indent=2))
    finally:
        conn.close()
