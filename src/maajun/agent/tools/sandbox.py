"""Which paths a tool may touch.

Everything a tool reads is sent to the AI provider, and in the daemon's case
may end up quoted in a public issue or pull request. So the boundary is not
advice in a prompt — the registry refuses the call.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

# Files that exist to hold a credential. Refused even inside an allowed root:
# a .env in the project directory is exactly the thing not to hand to a model.
SECRET_NAMES = frozenset({
    ".env", ".netrc", ".npmrc", ".pypirc", ".git-credentials", ".htpasswd",
    "credentials", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
})

SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore")

# maajun's own record: every incident it has handled and every chat anyone has
# had with it, in one file.
PRIVATE_NAMES = frozenset({"incidents.db", "incidents.db-wal", "incidents.db-shm"})


def is_secret(path: Path) -> bool:
    name = path.name.lower()
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

    def contains(self, path: Path) -> bool:
        return any(path == root or path.is_relative_to(root) for root in self.roots)

    def readable(self, path: Path) -> bool:
        """Whether a file a tool found by itself may be opened.

        refusal() gates the path a tool was *handed*; this gates every path it
        then discovers. grep walks a directory it was allowed into and reads
        whatever it finds there, so without this a .env one level down comes
        back quoted verbatim — the exact thing refusal() exists to prevent.

        Judged on the resolved path, so a symlink planted in the workspace
        cannot be used to read its target outside.
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
            allowed = ", ".join(str(root) for root in self.roots) or "nothing"
            return (
                f"{path} is outside the directories maajun may touch. "
                f"Allowed: {allowed}. Do not try another path outside them — "
                "say what you needed and why."
            )
        return ""
