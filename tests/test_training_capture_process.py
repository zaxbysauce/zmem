"""Real-process falsification for correlated training-capture lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "skills" / "memory" / "scripts" / "store.py"
SCRIPTS = STORE.parent
ADAPTER = ROOT / "hooks" / "lib" / "zmem-training-capture.py"

ADAPTER_CHILD = r'''
import importlib.util
import json
import os
import sys

spec = importlib.util.spec_from_file_location("capture_adapter", os.environ["ZMEM_ADAPTER"])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(json.dumps(module.run_action(json.loads(sys.stdin.read()), env=os.environ)))
'''

BOOTSTRAP_AUDIT_CHILD = r'''
import importlib.util
import json
import os
import sys
import types
from pathlib import Path

adapter_path = Path(os.environ["ZMEM_ADAPTER"]).resolve()
init_path = (Path(os.environ["ZMEM_SCRIPTS"]) / "storelib" / "__init__.py").resolve()
executed = []
def audit(event, args):
    if event == "exec" and args:
        code = args[0]
        if Path(getattr(code, "co_filename", "")).resolve() == init_path:
            executed.append("storelib-init")
sys.addaudithook(audit)
spec = importlib.util.spec_from_file_location("capture_adapter", adapter_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
mode = os.environ["BOOTSTRAP_MODE"]
if mode == "orphan":
    sentinel = types.ModuleType("storelib.schema")
    sys.modules["storelib.schema"] = sentinel
    result = module._load_private_standalone_api()
    print(json.dumps({"private": result is None, "preserved": sys.modules["storelib.schema"] is sentinel}))
elif mode == "fallback":
    module._USE_PRIVATE_STANDALONE_BOOTSTRAP = True
    module._private_storelib_child = lambda *args: (_ for _ in ()).throw(ImportError("forced"))
    api = module._load_api()
    import storelib
    print(json.dumps({"init_exec": len(executed), "compat": hasattr(storelib, "add_memory"), "keys": sorted(api)}))
else:
    module._USE_PRIVATE_STANDALONE_BOOTSTRAP = mode == "private"
    api = module._load_api()
    package = sys.modules["storelib"]
    schema = sys.modules["storelib.schema"]
    capture = sys.modules["storelib.training_capture"]
    print(json.dumps({
        "init_exec": len(executed), "compat": hasattr(package, "add_memory"),
        "schema": str(Path(schema.__file__).resolve()),
        "capture": str(Path(capture.__file__).resolve()),
        "children": package.schema is schema and package.training_capture is capture,
        "keys": sorted(api),
    }))
'''

PARALLEL_ADAPTER_CHILD = r'''
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

payload = json.loads(sys.stdin.read())
spec = importlib.util.spec_from_file_location("capture_adapter", os.environ["ZMEM_ADAPTER"])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
api = module._load_api()
module._load_api = lambda: api
Path(os.environ["READY"]).write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 10
while not Path(os.environ["GO"]).exists():
    if time.monotonic() >= deadline:
        raise TimeoutError("adapter parallel barrier timed out")
    time.sleep(0.01)
print(json.dumps(module.run_action(payload, env=os.environ)))
'''

WARM_ADAPTER_CHILD = r'''
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

payload = json.loads(sys.stdin.read())
spec = importlib.util.spec_from_file_location("capture_adapter", os.environ["ZMEM_ADAPTER"])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
api = module._load_api()
module._load_api = lambda: api
Path(os.environ["READY"]).write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 15
while not Path(os.environ["GO"]).exists():
    if time.monotonic() >= deadline:
        raise TimeoutError("warm adapter barrier timed out")
    time.sleep(0.01)
started = time.monotonic()
print(json.dumps({"result": module.run_action(payload, env=os.environ), "elapsed": time.monotonic() - started}))
'''

DIRECT_CORE_CHILD = r'''
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

payload = json.loads(sys.stdin.read())
sys.path.insert(0, os.environ["ZMEM_SCRIPTS"])
from storelib.training_capture import start_correlated_training_capture

conn = sqlite3.connect(os.environ["ZMEM_STORE"])
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA busy_timeout=5000")
try:
    Path(os.environ["READY"]).write_text("ready", encoding="utf-8")
    deadline = time.monotonic() + 10
    while not Path(os.environ["GO"]).exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("core parallel barrier timed out")
        time.sleep(0.01)
    row = start_correlated_training_capture(
        conn,
        host=payload["host"],
        session_id=payload["session_id"],
        namespace=payload["namespace"],
        host_task_id=payload["host_task_id"],
        task_id=payload["host_task_id"],
        turn_id=payload["turn_id"],
        prompt=payload["prompt"],
        assistant_response=payload["assistant_response"],
        consent_scope=os.environ["ZMEM_CAPTURE_CONSENT_SCOPE"],
        content_license=os.environ["ZMEM_CAPTURE_CONTENT_LICENSE"],
        redaction_policy_version=os.environ["ZMEM_CAPTURE_REDACTION_POLICY_VERSION"],
    )
    print(json.dumps(row))
finally:
    conn.close()
'''

CRASH_BEFORE_MAPPING = r'''
import os
import sqlite3
import sys

sys.path.insert(0, os.environ["ZMEM_SCRIPTS"])
from storelib.training_capture import start_correlated_training_capture

class CrashBeforeMapping(sqlite3.Connection):
    def execute(self, sql, params=()):
        if "INSERT INTO training_capture_correlation" in sql:
            raise SystemExit(91)
        return super().execute(sql, params)

conn = sqlite3.connect(os.environ["ZMEM_STORE"], factory=CrashBeforeMapping)
conn.row_factory = sqlite3.Row
start_correlated_training_capture(
    conn,
    host="claude",
    session_id="pre-map-crash",
    namespace="project:process",
    host_task_id="task",
    task_id="task",
    turn_id="turn",
    prompt="prompt",
    assistant_response="response",
    consent_scope=os.environ["ZMEM_CAPTURE_CONSENT_SCOPE"],
    content_license=os.environ["ZMEM_CAPTURE_CONTENT_LICENSE"],
    redaction_policy_version=os.environ["ZMEM_CAPTURE_REDACTION_POLICY_VERSION"],
)
'''

CRASH_AFTER_COMMIT = r'''
import importlib.util
import json
import os
import sys

spec = importlib.util.spec_from_file_location("capture_adapter", os.environ["ZMEM_ADAPTER"])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module._write_state = lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit(92))
module.run_action(json.loads(sys.stdin.read()), env=os.environ)
'''

DELAYED_ADAPTER = r'''
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

spec = importlib.util.spec_from_file_location("capture_adapter", os.environ["ZMEM_ADAPTER"])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

def wait_for_release():
    Path(os.environ["READY"]).write_text("ready", encoding="utf-8")
    deadline = time.monotonic() + 15
    while not Path(os.environ["RELEASE"]).exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("delayed adapter barrier timed out")
        time.sleep(0.01)

if os.environ["DELAY_KIND"] == "start":
    original = module._write_state
    def delayed_write_state(*args, **kwargs):
        wait_for_release()
        return original(*args, **kwargs)
    module._write_state = delayed_write_state
else:
    api = module._load_api()
    original = api["snapshot_correlated"]
    def delayed_snapshot(*args, **kwargs):
        wait_for_release()
        return original(*args, **kwargs)
    api["snapshot_correlated"] = delayed_snapshot
    module._load_api = lambda: api

print(json.dumps(module.run_action(json.loads(sys.stdin.read()), env=os.environ)))
'''

LOCK_HOLDER = r'''
import hashlib
import importlib.util
import json
import os
import time
from pathlib import Path

spec = importlib.util.spec_from_file_location("capture_adapter", os.environ["ZMEM_ADAPTER"])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
key = hashlib.sha256(json.dumps(
    {"host": "claude", "namespace": "project:process", "session": "dead-lock"},
    sort_keys=True, separators=(",", ":")
).encode("utf-8")).hexdigest()
with module._SessionLock(key, os.environ):
    Path(os.environ["READY"]).write_text("ready", encoding="utf-8")
    time.sleep(30)
'''


class ProcessCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.store = self.root / "training.sqlite"
        self.env = {
            **os.environ,
            "ZMEM_STORE": str(self.store),
            "ZMEM_DATA": str(self.root / "data"),
            "ZMEM_HOME": str(ROOT),
            "ZMEM_ADAPTER": str(ADAPTER),
            "ZMEM_SCRIPTS": str(SCRIPTS),
            "ZMEM_EMBED_PROFILE": "fake",
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "ZMEM_MODELS_DIR": str(self.root / "models"),
            "ZMEM_CAPTURE_CONSENT_SCOPE": "local",
            "ZMEM_CAPTURE_CONTENT_LICENSE": "CC-BY-4.0",
            "ZMEM_CAPTURE_REDACTION_POLICY_VERSION": "v1",
        }
        initialized = subprocess.run(
            [sys.executable, str(STORE), "init"],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def payload(self, session: str, namespace: str = "project:process", action: str = "start") -> dict[str, object]:
        return {
            "action": action,
            "host": "claude",
            "session_id": session,
            "namespace": namespace,
            "host_task_id": "task",
            "turn_id": "turn",
            "prompt": "process prompt",
            "assistant_response": "process response",
        }

    def adapter(self, payload: dict[str, object]) -> dict[str, object]:
        completed = subprocess.run(
            [sys.executable, "-c", ADAPTER_CHILD],
            input=json.dumps(payload),
            env=self.env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.adapter_stderr = completed.stderr
        return json.loads(completed.stdout)

    def popen(self, code: str, env: dict[str, str]) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

    def send(self, process: subprocess.Popen[str], payload: dict[str, object]) -> None:
        stream = process.stdin
        assert stream is not None
        stream.write(json.dumps(payload))
        stream.close()
        process.stdin = None

    def collect(self, process: subprocess.Popen[str], timeout: float = 15) -> str:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            self.assertEqual(process.returncode, 0, stderr)
            return stdout
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=10)
            self.fail("child process timed out")
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
        raise AssertionError("unreachable")

    def wait_for(self, path: Path, timeout: float = 5) -> None:
        deadline = time.monotonic() + timeout
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(path.exists(), f"child did not signal {path.name}")

    def scalar(self, query: str, args: tuple[object, ...] = ()) -> int:
        conn = sqlite3.connect(self.store)
        try:
            return int(conn.execute(query, args).fetchone()[0])
        finally:
            conn.close()

    def mapped_count(self, session: str, namespace: str) -> int:
        return self.scalar(
            "SELECT count(*) FROM training_capture_correlation AS m "
            "JOIN training_capture AS c ON c.capture_id=m.capture_id "
            "WHERE c.session_id=? AND c.namespace=?",
            (session, namespace),
        )

    def capture_count(self, session: str, namespace: str) -> int:
        return self.scalar(
            "SELECT count(*) FROM training_capture WHERE session_id=? AND namespace=?",
            (session, namespace),
        )

    def sidecar_path(self, session: str, namespace: str) -> Path:
        conn = sqlite3.connect(self.store)
        try:
            correlation = conn.execute(
                "SELECT m.correlation_key FROM training_capture_correlation AS m "
                "JOIN training_capture AS c ON c.capture_id=m.capture_id "
                "WHERE c.session_id=? AND c.namespace=?",
                (session, namespace),
            ).fetchone()[0]
        finally:
            conn.close()
        return self.store.parent / "training-capture" / f"{correlation}.json"

    def test_private_bootstrap_preserves_identity_and_existing_prefixes(self) -> None:
        expected_keys = [
            "clear_correlated", "connect", "identity_keys", "observe",
            "observe_correlated", "prepare", "snapshot", "snapshot_correlated",
            "start", "start_correlated",
        ]

        def audit(mode: str) -> dict[str, object]:
            completed = subprocess.run(
                [sys.executable, "-c", BOOTSTRAP_AUDIT_CHILD],
                env={**self.env, "BOOTSTRAP_MODE": mode},
                capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return json.loads(completed.stdout)

        normal = audit("normal")
        self.assertEqual(normal["init_exec"], 1)
        self.assertTrue(normal["compat"])
        self.assertEqual(normal["keys"], expected_keys)

        private = audit("private")
        self.assertEqual(private["init_exec"], 0)
        self.assertFalse(private["compat"])
        self.assertEqual(Path(str(private["schema"])), SCRIPTS / "storelib" / "schema.py")
        self.assertEqual(Path(str(private["capture"])), SCRIPTS / "storelib" / "training_capture.py")
        self.assertTrue(private["children"])
        self.assertEqual(private["keys"], expected_keys)

        fallback = audit("fallback")
        self.assertEqual(fallback["init_exec"], 1)
        self.assertTrue(fallback["compat"])
        self.assertEqual(fallback["keys"], expected_keys)

        orphan = audit("orphan")
        self.assertTrue(orphan["private"])
        self.assertTrue(orphan["preserved"])

    def test_direct_core_parallel_same_identity_creates_one_pair(self) -> None:
        payload = self.payload("direct-parallel")
        go = self.root / "direct-go"
        ready_paths = [self.root / f"direct-ready-{number}" for number in range(2)]
        processes = [
            self.popen(DIRECT_CORE_CHILD, {**self.env, "READY": str(ready), "GO": str(go)})
            for ready in ready_paths
        ]
        try:
            for process in processes:
                self.send(process, payload)
            for ready in ready_paths:
                self.wait_for(ready)
            go.write_text("go", encoding="utf-8")
            rows = [json.loads(self.collect(process)) for process in processes]
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
        self.assertEqual(len({row["capture_id"] for row in rows}), 1)
        self.assertEqual(self.mapped_count("direct-parallel", "project:process"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM training_capture WHERE session_id=?", ("direct-parallel",)), 1)

    def test_adapter_parallel_start_and_crash_boundaries(self) -> None:
        payload = self.payload("adapter-parallel")
        go = self.root / "adapter-go"
        ready_paths = [self.root / f"adapter-ready-{number}" for number in range(2)]
        processes = [
            self.popen(PARALLEL_ADAPTER_CHILD, {**self.env, "READY": str(ready), "GO": str(go)})
            for ready in ready_paths
        ]
        try:
            for process in processes:
                self.send(process, payload)
            for ready in ready_paths:
                self.wait_for(ready)
            go.write_text("go", encoding="utf-8")
            responses = [json.loads(self.collect(process)) for process in processes]
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
        receipt_ids = {item.get("capture_id") for item in responses if item.get("capture_id")}
        self.assertGreaterEqual(len(receipt_ids), 1)
        self.assertEqual(len(receipt_ids), 1)
        self.assertEqual(self.mapped_count("adapter-parallel", "project:process"), 1)
        self.assertEqual(self.capture_count("adapter-parallel", "project:process"), 1)
        durable_id = next(iter(receipt_ids))
        self.assertEqual(self.adapter(payload).get("capture_id"), durable_id)

        pre_mapping = subprocess.run([sys.executable, "-c", CRASH_BEFORE_MAPPING], env=self.env, timeout=15)
        self.assertEqual(pre_mapping.returncode, 91)
        self.assertEqual(self.mapped_count("pre-map-crash", "project:process"), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM training_capture WHERE session_id=?", ("pre-map-crash",)), 0)
        self.assertTrue(self.adapter(self.payload("pre-map-crash")).get("capture_id"))
        self.assertEqual(self.mapped_count("pre-map-crash", "project:process"), 1)

        post_commit_payload = self.payload("post-commit-crash")
        post_commit = subprocess.run(
            [sys.executable, "-c", CRASH_AFTER_COMMIT],
            input=json.dumps(post_commit_payload),
            env=self.env,
            text=True,
            timeout=15,
        )
        self.assertEqual(post_commit.returncode, 92)
        self.assertEqual(self.mapped_count("post-commit-crash", "project:process"), 1)
        self.assertFalse(self.sidecar_path("post-commit-crash", "project:process").exists())
        conn = sqlite3.connect(self.store)
        try:
            durable_id = conn.execute(
                "SELECT capture_id FROM training_capture WHERE session_id=?",
                ("post-commit-crash",),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(self.adapter(post_commit_payload).get("capture_id"), durable_id)
        self.assertEqual(self.mapped_count("post-commit-crash", "project:process"), 1)
        self.assertEqual(self.capture_count("post-commit-crash", "project:process"), 1)

    def test_clear_serializes_delayed_start_and_snapshot(self) -> None:
        target = self.payload("serialized")
        preserved = self.payload("preserved", "project:other")
        self.assertTrue(self.adapter(preserved).get("capture_id"))
        start_ready = self.root / "start-ready"
        start_release = self.root / "start-release"
        clear_warm_ready = self.root / "clear-warm-ready"
        clear_go = self.root / "clear-go"
        clear_payload = {**target, "action": "clear"}
        warm_clear = self.popen(
            WARM_ADAPTER_CHILD,
            {**self.env, "READY": str(clear_warm_ready), "GO": str(clear_go)},
        )
        delayed_start = self.popen(
            DELAYED_ADAPTER,
            {**self.env, "READY": str(start_ready), "RELEASE": str(start_release), "DELAY_KIND": "start"},
        )
        try:
            self.send(warm_clear, clear_payload)
            self.wait_for(clear_warm_ready)
            self.send(delayed_start, target)
            self.wait_for(start_ready)
            clear_go.write_text("go", encoding="utf-8")
            busy_clear = json.loads(self.collect(warm_clear))
            self.assertEqual(
                busy_clear["result"],
                {"error": "capture_busy"},
            )
            self.assertLess(float(busy_clear["elapsed"]), 1.2)
            self.assertEqual(self.mapped_count("serialized", "project:process"), 1)
            start_release.write_text("release", encoding="utf-8")
            self.assertTrue(json.loads(self.collect(delayed_start)).get("capture_id"))
        finally:
            if delayed_start.poll() is None:
                delayed_start.kill()
            delayed_start.wait(timeout=10)
            if warm_clear.poll() is None:
                warm_clear.kill()
            warm_clear.wait(timeout=10)
        start_sidecar = self.sidecar_path("serialized", "project:process")
        self.assertTrue(start_sidecar.is_file())
        self.assertEqual(self.adapter({**target, "action": "clear"}), {})
        self.assertFalse(start_sidecar.exists())
        self.assertEqual(self.mapped_count("serialized", "project:process"), 0)
        self.assertEqual(self.capture_count("serialized", "project:process"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM training_capture_closed_session"), 1)
        self.assertEqual(self.adapter(target), {})
        self.assertEqual(self.mapped_count("preserved", "project:other"), 1)

        snapshot_target = self.payload("snapshot-serialized")
        self.assertTrue(self.adapter(snapshot_target).get("capture_id"))
        snapshot_ready = self.root / "snapshot-ready"
        snapshot_release = self.root / "snapshot-release"
        snapshot_clear_warm_ready = self.root / "snapshot-clear-warm-ready"
        snapshot_clear_go = self.root / "snapshot-clear-go"
        snapshot_payload = {
            **snapshot_target,
            "action": "snapshot",
            "rendered": "rendered process payload",
            "effective_ops": ["memory_search"],
        }
        delayed_snapshot = self.popen(
            DELAYED_ADAPTER,
            {**self.env, "READY": str(snapshot_ready), "RELEASE": str(snapshot_release), "DELAY_KIND": "snapshot"},
        )
        warm_snapshot_clear = self.popen(
            WARM_ADAPTER_CHILD,
            {**self.env, "READY": str(snapshot_clear_warm_ready), "GO": str(snapshot_clear_go)},
        )
        try:
            self.send(warm_snapshot_clear, {**snapshot_target, "action": "clear"})
            self.wait_for(snapshot_clear_warm_ready)
            self.send(delayed_snapshot, snapshot_payload)
            self.wait_for(snapshot_ready)
            snapshot_clear_go.write_text("go", encoding="utf-8")
            busy_snapshot_clear = json.loads(self.collect(warm_snapshot_clear))
            self.assertEqual(busy_snapshot_clear["result"], {"error": "capture_busy"})
            self.assertLess(float(busy_snapshot_clear["elapsed"]), 1.2)
            snapshot_release.write_text("release", encoding="utf-8")
            self.assertTrue(json.loads(self.collect(delayed_snapshot)).get("delivery_snapshot_id"))
        finally:
            if delayed_snapshot.poll() is None:
                delayed_snapshot.kill()
            delayed_snapshot.wait(timeout=10)
            if warm_snapshot_clear.poll() is None:
                warm_snapshot_clear.kill()
            warm_snapshot_clear.wait(timeout=10)
        snapshot_sidecar = self.sidecar_path("snapshot-serialized", "project:process")
        self.assertTrue(snapshot_sidecar.is_file())
        self.assertEqual(self.adapter({**snapshot_target, "action": "clear"}), {})
        self.assertFalse(snapshot_sidecar.exists())
        self.assertEqual(self.mapped_count("snapshot-serialized", "project:process"), 0)
        self.assertEqual(self.adapter(snapshot_target), {})
        self.assertEqual(self.adapter(snapshot_payload), {})
        self.assertEqual(self.mapped_count("preserved", "project:other"), 1)

    def test_lock_death_and_exact_scope_expiry(self) -> None:
        ready = self.root / "holder-ready"
        contender_ready = self.root / "contender-ready"
        contender_go = self.root / "contender-go"
        contender = self.popen(
            WARM_ADAPTER_CHILD,
            {**self.env, "READY": str(contender_ready), "GO": str(contender_go)},
        )
        holder = self.popen(LOCK_HOLDER, {**self.env, "READY": str(ready)})
        try:
            self.send(contender, self.payload("dead-lock"))
            self.wait_for(contender_ready)
            assert holder.stdin is not None
            holder.stdin.close()
            self.wait_for(ready)
            contender_go.write_text("go", encoding="utf-8")
            contention = json.loads(self.collect(contender))
            self.assertEqual(contention["result"], {})
            self.assertLess(float(contention["elapsed"]), 1.2)
            self.assertEqual(self.mapped_count("dead-lock", "project:process"), 0)
            holder.terminate()
            holder.wait(timeout=10)
        finally:
            if contender.poll() is None:
                contender.kill()
            contender.wait(timeout=10)
            for stream in (contender.stdin, contender.stdout, contender.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
            if holder.poll() is None:
                holder.kill()
            holder.wait(timeout=10)
            for stream in (holder.stdin, holder.stdout, holder.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
        self.assertTrue(self.adapter(self.payload("dead-lock")).get("capture_id"))
        key = hashlib.sha256(json.dumps(
            {"host": "claude", "namespace": "project:process", "session": "dead-lock"},
            sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        self.assertTrue((self.store.parent / "training-capture" / "locks" / f"{key}.lock").is_file())

        expired = self.payload("expired")
        current = self.payload("expired", "project:current")
        self.assertTrue(self.adapter(expired).get("capture_id"))
        self.assertTrue(self.adapter(current).get("capture_id"))
        self.assertEqual(self.adapter({**expired, "action": "clear"}), {})
        conn = sqlite3.connect(self.store)
        try:
            expired_key = conn.execute("SELECT session_key FROM training_capture_closed_session").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(self.adapter({**current, "action": "clear"}), {})
        conn = sqlite3.connect(self.store)
        try:
            conn.execute(
                "UPDATE training_capture_closed_session SET cleared_at='2000-01-01T00:00:00Z' WHERE session_key=?",
                (expired_key,),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertTrue(self.adapter(expired).get("capture_id"))
        self.assertEqual(self.adapter(current), {})
        self.assertEqual(self.scalar("SELECT count(*) FROM training_capture_closed_session"), 1)


if __name__ == "__main__":
    unittest.main()
