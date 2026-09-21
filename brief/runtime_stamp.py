""" +  loaded-source fingerprint, VENDORED from overwatch W67
(`overwatch/overwatch/runtime_stamp.py`, overwatch 6a57af9 = W67b, ), for
dispatch HS-1 (fable #1014;  by #1365) and HS-1c (, fable #1400/#1412).
Re-vendored 2026-09-14 when the drift test caught the donor moving to .

Everything below the imports is the donor's code, unchanged; only this docstring
is dispatch's. `tests/test_runtime_stamp.py` holds the copy to the donor function by
function (AST comparison) and requires both to hash this repo identically. It skips,
never passes, when the donor is not on disk, and repoints to the host monitor's
`runtime_stamp.py` once that gains `editable_roots` (morse M67's shape).

WHAT IT MEASURES (): the tracked .py files of each declared RUNTIME root, taken
at spawn and compared to disk now. Not the commit sha, and not tests/ or tooling.
Dispatch's roots and its one runtime entry file are declared in brief/version.py.

DISPATCH'S OWN SPECIMEN for why the sha was the wrong instrument: on 2026-09-13 the
engine ran 36fc552 and reported `source_stale: true` against HEAD 472197a, while
`git diff --name-only 36fc552..HEAD -- 'brief/**/*.py' 'scripts/*.py'` was empty. Only
docs had moved. Twice on 2026-09-08 the engine was restarted just to clear that flag.

Every function returns None on ANY failure, never a guessed hash; None is what makes
`source_hash: "unverifiable"` and `source_stale: null` reachable on /api/health.
Dispatch has no editable dependency today (the roots list is empty), and the 
half is proven by a test that plants one.
"""

from __future__ import annotations

import hashlib
import re
import site
import subprocess
import sys
from pathlib import Path

__all__ = ["editable_roots", "runtime_fingerprint", "source_fingerprint"]


def _tracked_py_files(root: Path) -> list[bytes] | None:
    """Tracked `.py` paths under `root`, relative to it. None when git fails.

    Tracked files only, via `git ls-files`: an untracked scratch .py in the tree
    is not code this service loads, and letting one flip the light red would be
    the cry-wolf failure from the other direction. `git ls-files` run from a
    subdirectory lists only that subtree, relative to it -- which is what makes an
    editable install's `src/` root hashable without hashing its whole repo.
    """
    try:
        listing = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--", "*.py"],
            capture_output=True,
            timeout=10,
            check=False,  # returncode is inspected below; a raise here would mask it
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if listing.returncode != 0:
        return None
    return [n for n in listing.stdout.split(b"\0") if n]


def _digest_tree(root: Path, names: list[bytes], digest: hashlib._Hash) -> bool:
    """Fold `names` (sorted, name then content) into `digest`. False if any file
    cannot be read -- a tracked file we cannot read is not a clean reading."""
    for name in sorted(names):
        digest.update(name)
        try:
            with open(root / name.decode(), "rb") as handle:
                digest.update(hashlib.sha256(handle.read()).digest())
        except (OSError, UnicodeDecodeError):
            return False
    return True


def source_fingerprint(repo_path: str) -> str | None:
    """A hash of the tracked source under `repo_path`. None on ANY failure.

    The name is hashed alongside the contents, so a RENAME changes the fingerprint,
    and the listing is sorted, so the digest does not depend on filesystem order.
    (Meters-born-guilty: two builds of identical input must hash identically.)
    """
    names = _tracked_py_files(Path(repo_path))
    if not names:
        return None
    digest = hashlib.sha256()
    if not _digest_tree(Path(repo_path), names, digest):
        return None
    return digest.hexdigest()[:16]


_MAPPING_RE = re.compile(r"MAPPING\s*[:=].*?(\{.*?\})", re.S)


def _site_dirs() -> list[Path]:
    dirs: list[str] = []
    try:
        dirs.extend(site.getsitepackages())
    except AttributeError:  # some embedded interpreters lack it
        pass
    try:
        dirs.append(site.getusersitepackages())
    except AttributeError:
        pass
    return [Path(d) for d in dirs if d]


def editable_roots(site_dirs: list[Path] | None = None) -> list[Path]:
    """The source roots of every editable (`pip install -e`) dependency this
    interpreter can import from, discovered from site-packages METADATA -- never
    from `sys.modules`, which would make the covered set depend on what happened
    to be imported before the question was asked.

    Three shapes of editable install exist and all three are read:
      - `*.pth` files whose lines are bare directories (setuptools legacy /
        `--config-settings editable_mode=compat`, and uv's `_pkg.pth`);
      - `__editable___*_finder.py` with a `MAPPING = {"pkg": "/abs/dir"}` (PEP 660
        setuptools default) -- the package directory's PARENT is the root;
      - `*.egg-link`, whose first line is the directory (setup.py develop).

    Directories inside the interpreter's own prefix are not editable roots (a .pth
    that adds a site-packages subdir is a normal install). Only directories that
    exist are returned; a dangling entry is not code this process can load.
    Sorted and de-duplicated, so the fingerprint is order-independent.
    """
    prefixes = {Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()}
    found: set[Path] = set()

    def _consider(raw: str) -> None:
        raw = raw.strip()
        if not raw or raw.startswith(("import ", "import\t", "#")):
            return
        p = Path(raw)
        if not p.is_absolute() or not p.is_dir():
            return
        p = p.resolve()
        if any(prefix == p or prefix in p.parents for prefix in prefixes):
            return
        found.add(p)

    for d in site_dirs if site_dirs is not None else _site_dirs():
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.suffix == ".pth" or entry.suffix == ".egg-link":
                    for line in entry.read_text(errors="replace").splitlines():
                        _consider(line)
                elif entry.name.startswith("__editable___") and entry.suffix == ".py":
                    m = _MAPPING_RE.search(entry.read_text(errors="replace"))
                    if m:
                        for target in re.findall(
                            r"""['"]([^'"]+)['"]\s*[,}]""", m.group(1)
                        ):
                            # MAPPING values are package dirs; the root is their parent.
                            _consider(str(Path(target).parent))
            except OSError:
                continue
    return sorted(found)


def runtime_fingerprint(roots: list[Path]) -> str | None:
    """ +  + : one hash over the tracked .py files of each RUNTIME root.

     (fable #1408, 2026-09-14): the roots are the package directories the live
    process imports from -- `overwatch/` here -- plus each editable dependency's
    package root, NEVER the whole repo. Hashing the repo root made a tests-only or
    tools-only commit read `source_stale: true` on unchanged runtime code: dispatch's
    day-one cry-wolf case, and this repo's own the same morning (a printed message in
    `tools/ferrite_verdict.py` turned the live light red). The caller publishes the
    roots it passed, so a reader sees what "stale" is about.

    The FIRST root must contain tracked code; an empty service package is not a
    clean reading. None if any root is unverifiable -- a fingerprint that silently
    covered less than it claims would report `fresh` for exactly the change it was
    built to catch (morse #1253).
    """
    if not roots:
        return None
    digest = hashlib.sha256()
    for i, root in enumerate(roots):
        digest.update(b"\0root:" + str(root).encode())
        names = _tracked_py_files(root)
        if (
            names is None
            or (i == 0 and not names)
            or not _digest_tree(root, names, digest)
        ):
            return None
    return digest.hexdigest()[:16]
