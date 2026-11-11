"""Forward the host terminal's input stream, including paste and modified keys."""
import codecs
from contextlib import contextmanager, ExitStack
import os
import sys


@contextmanager
def _windows_mode(stream, enable, disable=0):
    import ctypes
    import msvcrt
    from ctypes import wintypes

    handle = wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno()))
    kernel = ctypes.windll.kernel32
    original = wintypes.DWORD()
    if not kernel.GetConsoleMode(handle, ctypes.byref(original)):
        raise ctypes.WinError()
    if not kernel.SetConsoleMode(handle, (original.value & ~disable) | enable):
        raise ctypes.WinError()
    try:
        yield handle
    finally:
        kernel.SetConsoleMode(handle, original.value)


@contextmanager
def keyboard_input():
    """Yield a chunk reader and restore console modes when the child exits."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        with ExitStack() as stack:
            if sys.stdout.isatty():
                stack.enter_context(_windows_mode(sys.stdout, 0x0004))  # VT output
            # VT input without the console's line editing, echo or Ctrl+C handling.
            handle = stack.enter_context(_windows_mode(sys.stdin, 0x0200, 0x0007))
            kernel = ctypes.windll.kernel32
            buffer = (ctypes.c_uint16 * 4096)()
            count = wintypes.DWORD()
            decoder = codecs.getincrementaldecoder("utf-16-le")()

            def read():
                if not kernel.ReadConsoleW(handle, buffer, len(buffer), ctypes.byref(count), None):
                    raise ctypes.WinError()
                return decoder.decode(ctypes.string_at(buffer, count.value * 2))

            try:
                yield read
            finally:
                kernel.CancelIoEx(handle, None)
    else:
        import select
        import termios
        import tty

        fd = sys.stdin.fileno()
        original = termios.tcgetattr(fd)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

        def read():
            if select.select([fd], [], [], 0.1)[0]:
                return decoder.decode(os.read(fd, 4096))
            return ""

        try:
            tty.setraw(fd)
            yield read
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, original)
