"""Keep Windows awake (no sleep, no screensaver) for the duration of a scrape.

Uses SetThreadExecutionState — the same API media players use during playback.
No mouse movement, no simulated input: we simply tell Windows the machine is
busy so it never idles into sleep or the screensaver. Released automatically
when `keep_awake()` context exits or clear() is called.
"""

import ctypes
import contextlib

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


def _set(flags):
    try:
        return ctypes.windll.kernel32.SetThreadExecutionState(flags) != 0
    except Exception:
        return False  # non-Windows or blocked — scrape still runs, just no guard


def prevent_idle():
    """Block sleep AND keep the display on (stops the screensaver)."""
    return _set(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)


def clear():
    """Release the guard — Windows resumes its normal idle/sleep timers."""
    return _set(ES_CONTINUOUS)


@contextlib.contextmanager
def keep_awake():
    prevent_idle()
    try:
        yield
    finally:
        clear()
