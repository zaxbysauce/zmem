// Identity resolution (issues #374 AC3 / plan D1): the T-05 tuple is
// principal + harness + native session (+ subagent lineage via the dsh
// CreateAgentOptions meta when the runtime exposes it). The native session
// id is agent.id — never a per-hook UUID, never the process cwd.
export function identity(agent, env = process.env) {
  const session = String(agent?.id ?? "");
  const header = agent?.session?.header;
  const meta = (header && typeof header === "object") ? header : {};
  return {
    session,
    lane: "deepseek",
    harness: "deepseek",
    principal: (env.ZMEM_PRINCIPAL || "").trim() || null,
    subagentOf: meta.parentSession ? String(meta.parentSession) : null,
    delegationDepth: typeof meta.delegationDepth === "number"
      ? meta.delegationDepth : null,
    origin: meta.origin || null,
    cwd: (env.ZMEM_CWD || process.cwd()),
    namespace: (env.ZMEM_NAMESPACE || "").trim() || "user:global",
  };
}

// The delivery-ledger key the backend derives (principal|harness|session —
// delivery_ledger._session_key parity); reported for observability only.
export function ledgerKey(ident) {
  if (!ident.principal) return ident.session;
  return [ident.principal, "deepseek", ident.session].join("|");
}
