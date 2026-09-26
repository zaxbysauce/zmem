"""FakeExecutor — the deterministic Scheduler + DeadlineExecutor seam.

Issue #160 final shape: ``submit(fn)`` stores the callable WITHOUT executing
it (a handle submitted with no ``delay_s`` has no due time and never fires);
``advance(seconds)`` drives the fake clock and completes due handles;
``now()`` reads that clock; ``cancel(handle)`` marks a pending handle
cancelled so it can never fire afterwards.  ``run(fn, deadline_s)`` is the
DeadlineExecutor surface: run ordinals listed in ``deadline_hits`` (or all,
with ``deadline_all``) hit their deadline — the call is cancelled (the cancel
hook fires exactly once) and ``run`` returns None, and the cancelled call can
never run afterwards (a late write attempt raises ``FakeCall.Cancelled``),
which is the no-late-write property the lane contract demands of a timed-out
child.  When the test declares ``pending_completion`` (fake seconds until the
NEXT operation completes), ``run`` schedules the operation on the fake clock,
advances by ``deadline_s``, and returns None when the operation was not due
yet — a deadline hit proven with zero wall-clock.

Issue #96 heritage: the lane unit tests construct ``FakeExecutor(deadline_hits={n})``
and call ``submit(fn, delay_s=...)``/``advance``/``run`` directly; those
semantics are unchanged.
"""


class FakeCall:
    """A cancellable callable wrapper the test can observe.

    ``cancel`` mirrors the production executor contract: cancelling the
    wrapper ALSO cancels the wrapped callable's own cancel hook (the
    ``_ChildCall`` the lane hands to ``run_command``), so a deadline hit
    really kills the child process instead of only marking a flag."""

    class Cancelled(RuntimeError):
        pass

    def __init__(self, fn):
        self._fn = fn
        self.cancelled = False
        self.finished = False
        self.run_count = 0
        self.result = None

    def cancel(self):
        self.cancelled = True
        inner = getattr(self._fn, "cancel", None)
        if inner is not None:
            inner()

    def __call__(self, *args, **kwargs):
        if self.cancelled:
            raise FakeCall.Cancelled("cancelled at the deadline")
        try:
            self.result = self._fn(*args, **kwargs)
            return self.result
        finally:
            self.finished = True
            self.run_count += 1


class FakeExecutor:
    """Deterministic in-process Scheduler + DeadlineExecutor."""

    def __init__(self, deadline_hits=()):
        self._now = 0.0
        self._scheduled = []  # (fire_at, seq, call); no entry = no due time
        self._seq = 0
        self.cancellations = []
        self.deadline_hits = set(deadline_hits)
        self.deadline_all = False
        self._runs = 0
        # Test-only knob (#160 DeadlineTest): fake seconds until the next
        # operation handed to ``run`` completes.  None keeps the direct-call
        # (issue #96) semantics.
        self.pending_completion = None

    # -- Scheduler surface -------------------------------------------------
    def submit(self, fn, delay_s=None):
        call = fn if isinstance(fn, FakeCall) else FakeCall(fn)
        self._seq += 1
        if delay_s is not None:
            self._scheduled.append((self._now + float(delay_s), self._seq, call))
            self._scheduled.sort(key=lambda item: (item[0], item[1]))
        return call

    def advance(self, seconds):
        target = self._now + float(seconds)
        while self._scheduled and self._scheduled[0][0] <= target:
            fire_at, _, call = self._scheduled.pop(0)
            self._now = max(self._now, fire_at)
            if not call.cancelled:
                call()
        self._now = target

    def now(self):
        return self._now

    def cancel(self, handle):
        handle.cancel()
        self._scheduled = [
            item for item in self._scheduled if item[2] is not handle]

    # -- DeadlineExecutor surface ------------------------------------------
    def run(self, fn, deadline_s):
        self._runs += 1
        call = fn if isinstance(fn, FakeCall) else FakeCall(fn)
        if self.deadline_all or self._runs in self.deadline_hits:
            call.cancel()
            self.cancellations.append(call)
            return None
        pending = self.pending_completion
        if pending is None:
            try:
                return call()
            except FakeCall.Cancelled:
                return None
        self.pending_completion = None
        handle = self.submit(call, delay_s=float(pending))
        self.advance(float(deadline_s))
        if handle.cancelled:
            self.cancellations.append(handle)
            return None
        if handle.run_count == 0:
            # Not due by the deadline: cancel so it can never fire late.
            self.cancel(handle)
            self.cancellations.append(handle)
            return None
        return handle.result
