# DeepSeek runtime preparation probes

Run npm ci --ignore-scripts --no-audit --no-fund, then node probe.mjs from this directory. The lock pins production DeepSeek 0.2.1-rc.2 packages and Cordis 4.0.5. The adapter is scripted in-process; no provider call or ZMem store access occurs.

On this Windows host, invoke agent commands through the mandatory run-bounded.py guard (512 MiB/process, 1 GiB/job, 60 seconds for the probe). Installation used a finite 1 GiB/process, 2 GiB/job, 180-second allowance.

The committed results/probe-results.json is a point-in-time execution result. Re-running replaces it and fixtures/runtime-delivery.json. No concurrent subagents, persisted resume/fork, compaction, desktop/Web installation, broker, or real store selector was exercised.
