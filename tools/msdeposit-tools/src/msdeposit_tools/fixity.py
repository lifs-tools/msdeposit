"""raw-fixity: SHA-256 of one raw data unit (vendor file or directory format) and its companions (private output).

Output `fixity.tsv` (private; it holds original member names):
  study, raw_id, kind (file|companion|tree), member, files, bytes, sha256
`member` is the path relative to the parent of the raw path (`x.d/AcqData/MSScan.bin`). A directory format gets one
`tree` row: SHA-256 over the sorted lines `<path relative to the directory>\t<sha256>\n` of its files, so the tree hash
does not depend on file creation order, timestamps or the directory's own name.

Symlinks are never followed out of the tree: the raw path itself may be a link (Nextflow staging), but a link inside a
directory format must point to a file inside the same tree, else the step fails.
"""

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .io import InputError, file_hashes, write_table

FIELDS = ["study", "raw_id", "kind", "member", "files", "bytes", "sha256"]


def add_arguments(p):
    p.add_argument("--raw", required=True, type=Path, help="vendor file or directory")
    p.add_argument("--companions", nargs="*", type=Path, default=[], help="companion files (e.g. .wiff.scan)")
    p.add_argument("--raw-id", required=True)
    p.add_argument("--study", required=True)
    p.add_argument("--jobs", type=int, default=4, help="parallel hashing threads")
    p.add_argument("--out", type=Path, default=Path("fixity.tsv"))


def tree_files(root: Path) -> list[tuple[str, Path]]:
    """Sorted (relative posix path, real file path) of all regular files below `root`, without leaving the tree."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise InputError(f"not a directory: {root.name}")
    out = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        for d in dirnames:
            if (here / d).is_symlink():
                raise InputError(f"symlinked directory inside a raw data tree: {(here / d).relative_to(root)}")
        for f in filenames:
            p = here / f
            rel = p.relative_to(root).as_posix()
            if p.is_symlink():
                target = p.resolve()
                if not target.is_relative_to(root) or not target.is_file():
                    raise InputError(f"symlink inside a raw data tree points outside it or is broken: {rel}")
                p = target
            elif not p.is_file():
                raise InputError(f"not a regular file inside a raw data tree: {rel}")
            out.append((rel, p))
    return sorted(out)


def tree_hash(entries: list[tuple[str, str]]) -> str:
    """SHA-256 over the sorted '<relpath>\\t<sha256>\\n' lines."""
    h = hashlib.sha256()
    for rel, digest in sorted(entries):
        h.update(f"{rel}\t{digest}\n".encode())
    return h.hexdigest()


def sha256(path: Path) -> str:
    return file_hashes(path, ("sha256",))["sha256"]


def hash_many(paths: list[Path], jobs: int = 4) -> list[str]:
    with ThreadPoolExecutor(max(1, jobs)) as ex:
        return list(ex.map(sha256, paths))


def fixity_rows(raw: Path, companions: list[Path], study: str, raw_id: str, jobs: int = 4) -> list[dict]:
    raw = Path(raw)
    name = raw.name  # the staged name (Nextflow links keep the original name)
    real = raw.resolve()
    if not real.exists():
        raise InputError(f"raw path does not exist: {name}")
    rows = []
    base = dict(study=study, raw_id=raw_id)
    if real.is_dir():
        files = tree_files(real)
        digests = hash_many([p for _, p in files], jobs)
        sizes = [p.stat().st_size for _, p in files]
        rows += [dict(base, kind="file", member=f"{name}/{rel}", files=1, bytes=n, sha256=d)
                 for (rel, _), n, d in zip(files, sizes, digests)]
        rows.append(dict(base, kind="tree", member=name, files=len(files), bytes=sum(sizes),
                         sha256=tree_hash([(rel, d) for (rel, _), d in zip(files, digests)])))
    elif real.is_file():
        rows.append(dict(base, kind="file", member=name, files=1, bytes=real.stat().st_size, sha256=sha256(real)))
    else:
        raise InputError(f"raw path is neither a file nor a directory: {name}")
    for c in companions:
        rc = Path(c).resolve()
        if not rc.is_file():
            raise InputError(f"companion is not a file: {Path(c).name}")
        rows.append(dict(base, kind="companion", member=Path(c).name, files=1, bytes=rc.stat().st_size,
                         sha256=sha256(rc)))
    return rows


def run(args) -> int:
    rows = fixity_rows(args.raw, args.companions, args.study, args.raw_id, args.jobs)
    write_table(args.out, rows, FIELDS)
    total = sum(r["bytes"] for r in rows if r["kind"] != "tree")
    print(f"{args.raw_id}: {sum(r['kind'] != 'tree' for r in rows)} files, {total} bytes")
    return 0
