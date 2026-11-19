"""Exclusive advisory file lock on POSIX (fcntl) and Windows (msvcrt)."""
import os

if os.name == 'nt':
    import msvcrt

    def lock(file, *, blocking=True):
        """Raise BlockingIOError when non-blocking and the lock is held elsewhere."""
        file.seek(0)
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        while True:
            try:
                msvcrt.locking(file.fileno(), mode, 1)
                return
            except OSError:
                # LK_LOCK gives up after ~10 s; keep waiting like flock does.
                if not blocking:
                    raise BlockingIOError('file is locked') from None
else:
    import fcntl

    def lock(file, *, blocking=True):
        fcntl.flock(file, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
