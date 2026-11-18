// Bounded plugin-state retention (issue #481, CS-010).
//
// Pure helpers only (node builtins; importable everywhere, incl. tests).
// Retention law (#481 AC4 / frozen check C12):
//   boundReceipts(receipts, cap): hard total bound; evict oldest TERMINAL
//     first; when only pending remain and over cap, evict oldest pending
//     (keep the newest up to cap) — pending is deprioritized, never exempt.
//   boundCaptureQueue(queue, cap, currentSession): hard total bound; evict
//     oldest NON-current-session first; when only current-session entries
//     remain and over cap, evict oldest current — the nudge-eligibility
//     signal becomes "current-session activity within the retained window"
//     (a recency window).
// Purity: both return NEW arrays, never mutate the input, preserve order,
// and map []/non-array and cap <= 0 to [].

export const CAPS = {
  // Hard bound on state.receipts (terminal-first eviction; pending kept
  // preferentially within the bound).
  receipts: 32,
  // Hard bound on state.captureQueue (other-session-first eviction; the
  // current session's entries kept preferentially within the bound).
  captureQueue: 32,
};

export function boundReceipts(receipts, cap) {
  if (!Array.isArray(receipts) || !Number.isFinite(cap) || cap <= 0) return [];
  const out = receipts.slice(); // never mutate the input
  const isPending = (r) => r && typeof r === "object" && r.status === "pending";
  // Oldest TERMINAL first; oldest pending only when nothing terminal remains.
  while (out.length > cap) {
    const idx = out.findIndex((r, i) => i < out.length && !isPending(r));
    const drop = idx >= 0 ? idx : 0;
    out.splice(drop, 1);
  }
  return out;
}

export function boundCaptureQueue(queue, cap, currentSession) {
  if (!Array.isArray(queue) || !Number.isFinite(cap) || cap <= 0) return [];
  const out = queue.slice(); // never mutate the input
  const isCurrent = (e) =>
    currentSession != null && e && typeof e === "object" &&
    e.session === currentSession;
  // Oldest NON-current first; oldest current only when every entry is
  // current-session (recency window keeps the newest current activity).
  while (out.length > cap) {
    const idx = out.findIndex((e) => !isCurrent(e));
    const drop = idx >= 0 ? idx : 0;
    out.splice(drop, 1);
  }
  return out;
}
