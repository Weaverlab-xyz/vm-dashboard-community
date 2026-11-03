"""A pytest-free stand-in for the ``monkeypatch`` fixture, for the standalone runners.

CI runs each test file through its own ``if __name__ == "__main__"`` runner, without
pytest (scripts/ci/run_tests.sh says why), so a test that takes ``monkeypatch`` gets
nothing. :func:`run` passes this when the test asks for it and undoes it afterwards.
Only ``setattr`` is provided; add what a test needs, nothing more.
"""
import inspect

_MISSING = object()


class MonkeyPatch:
    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value):
        self._undo.append((target, name, getattr(target, name, _MISSING)))
        setattr(target, name, value)

    def undo(self):
        for target, name, old in reversed(self._undo):
            if old is _MISSING:
                delattr(target, name)
            else:
                setattr(target, name, old)
        self._undo.clear()


def run(fn):
    """Call ``fn``, passing a :class:`MonkeyPatch` when it takes ``monkeypatch``."""
    if "monkeypatch" not in inspect.signature(fn).parameters:
        return fn()
    mp = MonkeyPatch()
    try:
        return fn(monkeypatch=mp)
    finally:
        mp.undo()
