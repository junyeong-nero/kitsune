"""Exclusive, process-lifetime locks that leave PID file contents readable."""

import errno
import os
import time
from typing import TextIO

if os.name == "nt":
    import msvcrt
else:
    import fcntl


# Windows byte-range locks are mandatory, unlike flock. Reserve a byte beyond
# the data so readers can inspect the PID while its owner holds the lock. Locking
# beyond EOF neither extends nor replaces the file; closing releases the lock.
_LOCK_OFFSET = 0x7FFFFFFF


def lock_exclusive(file: TextIO, *, blocking: bool = True) -> None:
    if os.name != "nt":
        fcntl.flock(file, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return
    descriptor = file.fileno()
    while True:
        position = os.lseek(descriptor, 0, os.SEEK_CUR)
        try:
            os.lseek(descriptor, _LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            return
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            if not blocking:
                raise BlockingIOError(error.errno, "Bridge file is locked") from error
        finally:
            os.lseek(descriptor, position, os.SEEK_SET)
        # LK_LOCK stops retrying after ten seconds; lifecycle_lock must wait
        # until the other client exits, just as blocking flock does on POSIX.
        time.sleep(0.05)
