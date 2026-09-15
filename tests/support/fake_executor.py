"""FakeExecutor — the deterministic Scheduler + DeadlineExecutor seam for
the issue #96 lane unit tests.

Locally defined pending #160's transport abstraction (the issue #96 contract
cites this module path; the upstream protocol is specified in the issue text
and implemented identically here).

Test model: the virtual clock drives ``submit``/``advance``/``now``; the run
ordinal(s) listed in ``deadline_hits`` hit their deadline — ``run`` cancels
the call (the cancel hook fires exactly once) and returns None, and the
cancelled call can never run afterwards (a late write attempt raises
``FakeCall.Cancelled``), which is the no-late-write property the lane
contract demands of a timed-out child.
"""


class FakeCall:
    """A cancellable callable wrapper the test can observe."""

    class Cancelled(RuntimeError):
        pass

    def __init__(self, fn):
        self._fn = fn
        self.cancelled = False
        self.finished = False
        self.run_count = 0

    def cancel(self):
        self.cancelled = True

    def __call__(self, *args, **kwargs):
        if self.cancelled:
            raise FakeCall.Cancelled("cancelled at the deadline")
        try:
            return self._fn(*args, **kwargs)
        finally:
            self.finished = True
            self.run_count += 1


class FakeExecutor:
    """Deterministic in-process Scheduler + DeadlineExecutor."""

    def __init__(self, deadline_hits=()):
        self._now = 0.0
        self._scheduled = []  # (fire_at, seq, call)
        self._seq = 0
        self.cancellations = []
        self.deadline_hits = set(deadline_hits)
        self.deadline_all = False
        self._runs = 0

    # -- Scheduler surface -------------------------------------------------
    def submit(self, fn, delay_s=0.0):
        call = fn if isinstance(fn, FakeCall) else FakeCall(fn)
        self._seq += 1
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

    # -- DeadlineExecutor surface ------------------------------------------
    def run(self, fn, deadline_s):
        self._runs += 1
        call = fn if isinstance(fn, FakeCall) else FakeCall(fn)
        if self.deadline_all or self._runs in self.deadline_hits:
            call.cancel()
            self.cancellations.append(call)
            return None
        try:
            return call()
        except FakeCall.Cancelled:
            return None
