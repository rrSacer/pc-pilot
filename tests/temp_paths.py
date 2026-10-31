"""Short, private test roots whose socket inodes agree with unix_diag.

Btrfs subvolumes expose a different stat().st_dev than unix_diag's superblock
device. Use tmpfs/ramfs for the compositor authentication fixture, without
changing the production authentication check or the caller's TMPDIR.
"""
import os
import re
import tempfile


def filesystem_type(path):
    path = os.path.realpath(path)
    match = ""
    filesystem = None
    with open("/proc/self/mountinfo", encoding="utf-8") as mounts:
        for line in mounts:
            before, _, after = line.partition(" - ")
            fields = before.split()
            if len(fields) < 5 or not after:
                continue
            mount = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4])
            if (path == mount or path.startswith(mount.rstrip("/") + "/")) and len(mount) > len(match):
                match, filesystem = mount, after.split()[0]
    return filesystem


def socket_temp_base():
    """Return an existing writable memory filesystem; no shared private parent.

    Individual mkdtemp/TemporaryDirectory roots are 0700 and their owners clean
    them up. A headless shell can chown its own root to the desktop test user.
    Keeping the parent short also leaves room for AF_UNIX fixture suffixes.
    """
    for base in dict.fromkeys((tempfile.gettempdir(), "/run", "/dev/shm")):
        if (len(os.fsencode(base)) <= 16 and filesystem_type(base) in {"tmpfs", "ramfs"}
                and os.access(base, os.W_OK | os.X_OK)):
            return base
    raise RuntimeError("tests need a writable tmpfs/ramfs for private UNIX sockets")


if __name__ == "__main__":
    print(tempfile.mkdtemp(prefix=f"pcp-{os.getuid()}-", dir=socket_temp_base()))
