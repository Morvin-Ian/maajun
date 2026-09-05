from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

# Refused even inside an allowed root.
SECRET_NAMES = frozenset({
    ".env", ".netrc", ".npmrc", ".pypirc", ".git-credentials", ".htpasswd",
    "credentials", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
})

SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore")

# The committed template, not the file with the secrets in it — and exactly
# what fix mode edits when a finding is an undocumented environment variable.
ENV_TEMPLATE_SUFFIXES = ("example", "sample", "template", "dist", "defaults")

# Secret names that are also plausible directory names. Refusing the directory
# hid every file under it; the files inside are still screened one by one.
DIRECTORY_SAFE_NAMES = frozenset({"credentials"})

# maajun's own record of every incident and conversation.
PRIVATE_NAMES = frozenset({"incidents.db", "incidents.db-wal", "incidents.db-shm"})


def is_env_template(name: str) -> bool:
    """Whether a dotenv-ish name is the documented template, not the secret.

    Judged on the last component only, so `.env.production.example` is a
    template and `.env.production` is not.
    """
    return (
        name.startswith(".env")
        and name.rpartition(".")[2] in ENV_TEMPLATE_SUFFIXES
    )


def nearest_under(target: Path, root: Path) -> Path | None:
    """The file under `root` that a path from outside it most likely meant.

    A traceback names the deployment's path — `/app/apps/accounts/views.py` —
    and that is the path a model reaches for first. The checkout holds the
    same file at a shorter one, so the longest tail of the given path that
    exists under the root is the file it meant. Never the bare root: the
    shortest candidate still carries the filename.
    """
    parts = target.parts[1:] if target.is_absolute() else target.parts
    for start in range(len(parts)):
        candidate = root.joinpath(*parts[start:])
        if candidate.exists():
            return candidate
    return None


def is_secret(path: Path) -> bool:
    name = path.name.lower()
    if is_env_template(name):
        return False
    # is_dir() is False for a path that does not exist, so a guessed name is
    # still refused.
    if name in DIRECTORY_SAFE_NAMES and path.is_dir():
        return False
    return (
        name in SECRET_NAMES
        or name.startswith(".env.")
        or name.endswith(SECRET_SUFFIXES)
    )


class Sandbox:
    """The directories and files a tool may reach, and what it may never read."""

    def __init__(self, roots: Iterable[Path | str]):
        resolved = (Path(root).expanduser().resolve() for root in roots)
        self.roots = tuple(dict.fromkeys(resolved))

    def resolve(self, path: str) -> Path:
        """A handed path as an absolute one.

        Relative paths resolve against the first root, not the process
        directory: the model is told where the workspace is and then says
        "app/views.py", which used to resolve next to maajun itself and be
        refused as outside the sandbox.
        """
        candidate = Path(path).expanduser()
        if not candidate.is_absolute() and self.roots:
            candidate = self.roots[0] / candidate
        return candidate.resolve()

    def contains(self, path: Path) -> bool:
        return any(path == root or path.is_relative_to(root) for root in self.roots)

    def readable(self, path: Path) -> bool:
        """Whether a file a tool found by itself may be opened.

        refusal() gates the path a tool was *handed*; this gates what it then
        discovers, so a .env one level down cannot come back through grep.
        Judged on the resolved path, so a planted symlink cannot escape.
        """
        try:
            return not self.refusal(path.resolve())
        except OSError:
            return False

    def refusal(self, path: Path) -> str:
        """Why `path` is off limits, or "" if the tool may go ahead.

        The message is written for the model: it says what it may reach
        instead, so a refused call turns into a better one rather than the
        same one again.
        """
        if is_secret(path):
            return (
                f"{path.name} holds credentials and is never read. "
                "Tell the user you cannot open it."
            )
        if path.name in PRIVATE_NAMES:
            return (
                f"{path.name} is maajun's own database. Use search_incidents, "
                "get_incident, or search_conversations instead of reading it."
            )
        if ".git" in path.parts:
            return f"{path} is inside a .git directory, which is not readable."
        if not self.contains(path):
            hint = self.nearest(path)
            if hint:
                # The traceback's path is refused, but the file it names is in
                # the checkout under a shorter one.
                return (
                    f"{path} is outside the directories maajun may touch, but "
                    f"the same file is in the checkout at {hint}. Use that path."
                )
            allowed = ", ".join(str(root) for root in self.roots) or "nothing"
            return (
                f"{path} is outside the directories maajun may touch, and "
                f"nothing under {allowed} matches it. Do not try another path "
                "outside them — say what you needed and why."
            )
        return ""

    def nearest(self, path: Path) -> Path | None:
        """The file inside a root that an outside path most likely meant.

        Named in the refusal and never substituted: which file gets read, and
        then edited, is not a guess worth making silently.
        """
        for root in self.roots:
            found = nearest_under(path, root)
            if found:
                return found
        return None
