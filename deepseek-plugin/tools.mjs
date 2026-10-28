// Explicit zmem_* tools (issues #374 AC2 / #375 AC3). One native surface
// routed through the SAME backend as passive recall: reads via the store
// CLI (remote_gate routes in remote mode), writes via the typed op queue —
// pending receipt in remote mode, committed receipt locally after
// ops-flush. No second store is possible by construction. Failures return
// isError with the backend's actionable error; a failed save is never
// reported as success.
import * as backend from "./launcher.mjs";

const STRING = { type: "string" };
const TEXT_PARAMS = {
  namespace: { ...STRING, required: true },
  type: { type: "string", required: false },
  content: { ...STRING, required: true },
  source_ref: { type: "string", required: false },
  tags: { type: "string", required: false },
};
const ID_PARAMS = { id: { ...STRING, required: true } };

function render(args, value) {
  return [{ type: "text", text: JSON.stringify(value) }];
}

export const TOOL_SPECS = {
  zmem_add: {
    description: "Add a memory to the shared ZMem store (reviewed capture; "
      + "queued with a pending receipt in remote mode, committed locally).",
    parameters: TEXT_PARAMS, write: true, op: "add",
  },
  zmem_update: {
    description: "Update an existing memory by id.",
    parameters: { ...ID_PARAMS, content: STRING, namespace: STRING },
    write: true, op: "update",
  },
  zmem_supersede: {
    description: "Supersede a memory with a replacement.",
    parameters: { ...ID_PARAMS, content: STRING, namespace: STRING },
    write: true, op: "supersede",
  },
  zmem_invalidate: {
    description: "Tombstone a memory with a reason.",
    parameters: { ...ID_PARAMS, reason: STRING },
    write: true, op: "invalidate",
  },
  zmem_recall: {
    description: "Query-aware memory recall.",
    parameters: { query: { ...STRING, required: true } },
    write: false, cli: (a) => ["recall", "--query", a.query, "--json"],
  },
  zmem_search: {
    description: "Keyword memory search.",
    parameters: { query: { ...STRING, required: true } },
    write: false, cli: (a) => ["search", "--query", a.query, "--json"],
  },
  zmem_get: {
    description: "Fetch one memory by id.",
    parameters: ID_PARAMS,
    write: false, cli: (a) => ["get", "--id", a.id, "--json"],
  },
  zmem_list: {
    description: "List memories in a namespace.",
    parameters: { namespace: STRING },
    write: false, cli: (a) => ["list", "--namespace", a.namespace || "user:global", "--json"],
  },
  zmem_recent: {
    description: "Recent passive memories.",
    parameters: {},
    write: false, cli: () => ["recent", "--json"],
  },
};

export function registerTools(ctx, hooks = {}) {
  const register = (...a) => (ctx.tools ? ctx.tools.register(...a) : null);
  for (const [name, spec] of Object.entries(TOOL_SPECS)) {
    register({
      name,
      description: spec.description,
      parameters: spec.parameters,
      output: {
        schema: { type: "object" },
        render,
      },
      timeoutMs: 30000,
      async execute(args) {
        if (spec.write) {
          const cleaned = { ...(args || {}) };
          // The namespace default rides the resolved session namespace
          // (identity()), never an implicit per-tool choice.
          if (!cleaned.namespace) {
            cleaned.namespace = process.env.ZMEM_NAMESPACE
              || "user:global";
          }
          if (!cleaned.signal) {
            // An explicit tool invocation is a user-signal write (the
            // same semantics the CLI closeout adds carry); without it the
            // applied row lands at signal none/confidence 0.2 and is
            // invisible to the recall lanes by design.
            cleaned.signal = "user";
          }
          const argsJson = cleaned;
          const enq = await backend.enqueueOp(spec.op, argsJson);
          if (enq.code !== 0 || !enq.receipt || enq.receipt.ok !== true) {
            return {
              isError: true,
              error: {
                message: "zmem enqueue failed: "
                  + ((enq.raw && enq.raw.err) || "unknown").slice(0, 200),
                info: { name, code: "enqueue_failed" },
              },
              content: [],
            };
          }
          hooks.onReceipt && hooks.onReceipt(enq.receipt);
          const remote = (process.env.ZMEM_REMOTE_URL || "").trim();
          if (remote) {
            if (name === "zmem_add") {
              hooks.onCloseout && hooks.onCloseout(enq.receipt);
            }
            return { value: enq.receipt, content: render(args, enq.receipt) };
          }
          const flush = await backend.flushLocal();
          const receipts = (flush.receipts && flush.receipts.receipts) || [];
          const mine = receipts.find(
            (x) => x.op_id === enq.receipt.op_id) || receipts[0] || null;
          if (flush.code !== 0 || !mine) {
            return {
              isError: true,
              error: {
                message: "zmem local apply failed (op stays pending): "
                  + ((flush.raw && flush.raw.err) || "unknown").slice(0, 200),
                info: { name, code: "apply_failed" },
              },
              content: [],
            };
          }
          hooks.onReceipt && hooks.onReceipt(mine);
          if (name === "zmem_add") hooks.onCloseout && hooks.onCloseout(mine);
          return { value: mine, content: render(args, mine) };
        }
        const cli = spec.cli(args || {});
        const r = await backend.runTool(cli);
        if (r.code !== 0 || !r.result) {
          return {
            isError: true,
            error: {
              message: "zmem read failed: "
                + ((r.raw && (r.raw.err || r.raw.out)) || "unknown")
                  .slice(0, 200),
              info: { name, code: "read_failed" },
            },
            content: [],
          };
        }
        return { value: r.result, content: render(args, r.result) };
      },
    });
  }
}
