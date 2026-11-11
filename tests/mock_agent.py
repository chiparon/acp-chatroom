"""A terminal peer that records what its editor actually receives."""
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        handle = wintypes.HANDLE(msvcrt.get_osfhandle(sys.stdin.fileno()))
        assert ctypes.windll.kernel32.SetConsoleMode(handle, 0x0200)

        def read():
            buf = ctypes.create_unicode_buffer(2048)
            count = wintypes.DWORD()
            assert ctypes.windll.kernel32.ReadConsoleW(handle, buf, len(buf), ctypes.byref(count), None)
            return buf[:count.value]
    else:
        import tty
        import codecs
        tty.setraw(sys.stdin.fileno())
        decoder = codecs.getincrementaldecoder("utf-8")()

        def read():
            return decoder.decode(os.read(sys.stdin.fileno(), 2048))

    root = Path(os.environ["ACP_TEST_DIR"])
    sys.stdout.write("\x1b[?2004h")
    sys.stdout.flush()
    (root / "ready").touch()
    received = ""
    while "END_CAPTURE" not in received:
        received += read()
        if "KEYS_DONE" in received:
            (root / "keys").touch()
        if "\x1b[201~\r" in received:
            (root / "pasted").touch()
    (root / "input.json").write_text(json.dumps([sys.argv[1:], received]), "utf-8")
    subprocess.run(["acp-chat", "send", "--to", "@tester", "--mode", "explicit", "received"], check=True)


if __name__ == "__main__":
    main()
