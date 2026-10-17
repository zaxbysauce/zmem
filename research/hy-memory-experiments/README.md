# HY Memory-inspired ZMem experiments

See [findings and proposed scope](../../docs/plans/hy-memory-experiment-results.md).

- `run_experiments.py`: model-absent exact dedup, separately labelled controlled-candidate policy ablations, evidence/lesson lineage and the existing curator seam.
- `real_semantic.py`: cached verified MiniLM model with exact-cosine indexing; `--native-vec` uses ZMem's ordinary connection and sqlite-vec path.
- `validate_results.py`: compare both real-model routes and verify expected observations and production source hashes.

On this Windows host, every command must use `C:/Python311/python.exe C:/Users/Brett/.codex/scripts/run-bounded.py --cwd <absolute repo> --receipt <fresh absolute path> -- <absolute python> -X utf8 <driver>`. Use default 512 MiB/process, 1 GiB/job, 60 seconds for policy/validation. Real ONNX inference uses a finite 1024 MiB/process, 1536 MiB/job, 60-second allowance and two intra-op threads. Run heavy drivers serially. `real_semantic.py` explicitly reads the existing model at `C:/Users/Brett/.zmem/models`; it never opens the canonical store or downloads a model.

All temporary SQLite stores are owned under ignored `results/`. The oracle-subject compatibility wrapper exists only in these experiments. The Tencent writer model has not been run. Generated results preserve the distinction between a real model measurement, an injected candidate and an untested architectural proposal.
