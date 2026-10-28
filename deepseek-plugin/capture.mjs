// Capture delegation (issues #375 AC1/AC2): classification gates, always-on
// redaction, dedup and queue/opqueue routing all live in the shared python
// entry hooks/lib/zmem-capture-entry.py — the "same contract as
// zmem-capture-correction.sh" clause of the interface freeze. This module
// only classifies WHAT to send (original user text vs plugin/context vs
// tool outcome) and forwards.
import * as backend from "./launcher.mjs";

export function classifyUserCorrection(message) {
  // Only ORIGINAL user input (source.kind === "user") is a correction
  // candidate; plugin-sourced/context/tool/replayed text never is.
  if (!message || !message.source) return null;
  if (message.source.kind !== "user") return null;
  const text = (message.content || [])
    .map((b) => (b && b.type === "text" ? b.text || "" : "")).join(" ");
  if (!text || text.length < 5) return null;
  return { kind: "correction", text };
}

export function classifyToolOutcome(exec, result) {
  // Only immutable FINAL failures/denials are evidence; success is not.
  if (!result || result.isError !== true) return null;
  const text = String(result.error?.message || "");
  if (!text) return null;
  return { kind: "failure", text };
}

