"""Prepare permissions and exec the unprivileged engine in the same container."""
from pathlib import Path
import os
import stat
import sys


def set_permissions(path: Path, *, uid: int, mode: int, directory: bool = False):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    if directory:
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise ValueError(f"A regular, unlinked configuration file is required: {path.name}")
        os.fchown(fd, uid, uid)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def initialize(app: Path = Path("/app"), locks: Path = Path("/tmp/danta-writers-10001"),
               config: Path | None = None):
    # These mount roots are created by Docker. Never traverse existing DB/auth data.
    for path in (app / "var", locks, app / "auth"):
        set_permissions(path, uid=10001, mode=0o700, directory=True)
    config = config or app / "config"
    set_permissions(config, uid=0, mode=0o755, directory=True)
    for name in ("app.yaml", "strategy.yaml", "schedules.yaml"):
        set_permissions(config / name, uid=0, mode=0o644)
    for name, uid, mode in (("secrets.yaml", 10001, 0o400), ("runtime.json", 0, 0o444),
                            ("runtime-manifest.json", 0, 0o444)):
        path = config / name
        if path.exists() or path.is_symlink():
            set_permissions(path, uid=uid, mode=mode)


def main():
    if os.getuid() == 0:
        initialize(config=Path("/root/danta-config"))
        os.setgroups([])
        os.setgid(10001)
        os.setuid(10001)
    args = sys.argv[1:] or ["doctor"]
    command = args if args[0] == "codex" else ["danta", *args]
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
