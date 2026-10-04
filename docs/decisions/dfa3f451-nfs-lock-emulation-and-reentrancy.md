# NFS lock emulation needs a process-local RLock too

`bot/anistore.py` — the module-level per-lock-path `threading.RLock` registry, and its
reentrancy-depth tracking.

The homelab's `/config` is NFS without `local_lock`, so `flock()`/`fcntl` locks are emulated
with POSIX byte-range locks there — and those are owned per PROCESS, not per fd: two threads
in this same process can each "acquire" `LOCK_EX` at once, and closing ANY fd for the file
drops every one of this process's locks on it. The dashboard is multithreaded
(ThreadingHTTPServer request threads, the resolver thread, the mover thread), so the OS-level
lock alone does not serialize concurrent `update_ani` calls within this process on NFS — this
module-level `RLock` registry is what does.

A thread already holding `locked(path)` that calls it again (directly or via a helper) must
not open a second fd or flock/close again — an inner close would drop the outer POSIX lock on
NFS. Only the outermost call for a given thread+path actually opens/locks/closes; a nested
call just extends the RLock hold. This is tracked via a per-thread reentrancy depth, keyed by
`lock_path`.

## EACCES on the write-open must not fall back to O_RDONLY

`bot/anistore.py` — `_open_and_flock_posix()`'s write-open of the lock file. If the O_RDWR
open fails with `EACCES`/`EPERM` (the caller's user can't write the lock file), this raises a
clear, actionable `PermissionError` naming the lock path and the `chmod 666` fix, instead of
retrying with `O_RDONLY` — a silent fallback there would just reproduce the same EBADF once
`flock()` tried to take an exclusive lock on that read-only fd.

## Do not

Don't rely on the OS-level `flock`/POSIX lock alone to serialize concurrent `update_ani`
calls within this process — on NFS without `local_lock`, those locks are per-process, not
per-thread, so two threads here can both believe they hold the lock. Keep the module-level
`RLock` registry. And don't let a nested `locked(path)` call open a second fd or
flock/close again — an inner close drops the outer POSIX lock on NFS; only the outermost call
for a thread+path may actually open/lock/close. And don't retry the write-open with
`O_RDONLY` after an `EACCES` — that reproduces the same EBADF instead of surfacing a clear
error.
