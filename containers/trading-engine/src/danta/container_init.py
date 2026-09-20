"""Prepare mounted directories once; the engine itself remains unprivileged."""
from pathlib import Path
import os
import stat


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


def initialize(app: Path = Path("/app"), locks: Path = Path("/tmp/danta-writers-10001")):
    # These mount roots are created by Docker. Never traverse existing DB/auth data.
    for path in (app / "var", locks, app / "auth"):
        set_permissions(path, uid=10001, mode=0o700, directory=True)
    config = app / "config"
    set_permissions(config, uid=0, mode=0o755, directory=True)
    for name in ("app.yaml", "strategy.yaml", "schedules.yaml"):
        set_permissions(config / name, uid=0, mode=0o644)
    for name, uid, mode in (("secrets.yaml", 10001, 0o400), ("runtime.json", 0, 0o444),
                            ("runtime-manifest.json", 0, 0o444)):
        path = config / name
        if path.exists() or path.is_symlink():
            set_permissions(path, uid=uid, mode=mode)


if __name__ == "__main__":
    initialize()
