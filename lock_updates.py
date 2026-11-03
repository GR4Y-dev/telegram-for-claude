"""Hold the existing cross-project lock safely across an exec of update.sh."""
import fcntl
import os
import stat
import sys

path, script, *args = sys.argv[1:]
fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
info = os.fstat(fd)
if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
    raise SystemExit("Unsafe shared update lock; update stopped.")
fcntl.flock(fd, fcntl.LOCK_EX)
os.set_inheritable(fd, True)
os.environ["CONNECTOR_UPDATE_LOCKED"] = "1"
os.execv("/bin/bash", ["bash", script, *args])
