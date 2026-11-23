"""Reading a subprocess pipe with a timeout, also on Windows, where select() takes only sockets."""

import queue
import threading


def readline(stream, timeout):
    """The next line of stream, or None after timeout seconds."""
    lines = queue.Queue()
    threading.Thread(target=lambda: lines.put(stream.readline()), daemon=True).start()
    try:
        return lines.get(timeout=timeout)
    except queue.Empty:
        return None
