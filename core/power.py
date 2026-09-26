"""
Power: keep Windows from going to sleep ONLY while the company is working.

Uses the Windows API SetThreadExecutionState from the thread that does the work. It is a
request, not a setting: nothing in Windows is changed, the display may still turn off,
the owner can still put the PC to sleep by hand, and the request ends when the work
stops or the program exits. When there is nothing to do (all tasks done, or waiting
for the owner), normal sleep comes back and the PC can save electricity.
"""

import sys

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _windows_setter(flags: int) -> None:
    import ctypes
    ctypes.windll.kernel32.SetThreadExecutionState(flags)


class KeepAwake:
    """One per thread. set(True) = don't sleep while idle; set(False) = normal again."""

    def __init__(self, setter=None):
        self.setter = setter or (_windows_setter if sys.platform == "win32" else None)
        self.active = False

    def set(self, on: bool) -> None:
        if on == self.active or self.setter is None:
            return
        self.setter(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
        self.active = on
