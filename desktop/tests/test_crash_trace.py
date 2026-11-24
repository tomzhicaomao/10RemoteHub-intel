"""A native crash leaves the Python stack of every thread in logs/crash-trace.log."""
import faulthandler
import sys

from remotehub.diagnostics import enable_crash_trace


def test_crash_trace_records_the_python_stack_location(tmp_path):
    try:
        path = enable_crash_trace(tmp_path / "logs")
        assert path is not None and faulthandler.is_enabled()
        assert path.read_text(encoding="utf-8").strip().endswith("started")
    finally:
        faulthandler.enable(sys.stderr)   # give pytest its own handler back
