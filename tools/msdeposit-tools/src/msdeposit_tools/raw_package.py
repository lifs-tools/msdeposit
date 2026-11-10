"""raw-package: package one raw data unit for upload (standard mode), as planned in plan.json `raw_files`.

  copy  the vendor file and its companions are hard-linked (copied across filesystems) under their deposited names
  zip   a directory format becomes a deterministic ZIP64 archive: ZIP_STORED, members sorted, fixed timestamps
        (1980-01-01 00:00) and permissions, archive root = the deposited stem (`<name>.zip` holds `<name>/...`).
        Member names below the root are kept as they are (vendor readers rely on them), with `/` separators.
        The archive is verified by re-reading: member count, names, sizes and SHA-256 of every member against the
        tree, and the CRCs (checked by zipfile while reading).
  none  error (raw data is not deposited for this study)

Outputs in --outdir: the deposited files (base names of the planned paths) and `<raw_id>.raw_package.json`
(`msdeposit/raw_package/1`: study, raw_id, packaging, files [{deposited, bytes, sha256}], members, member_bytes,
verified). Symlinks are never followed out of a directory tree.
"""

import hashlib
import os
import shutil
import zipfile
from pathlib import Path

from .fixity import tree_files
from .io import CHUNK, InputError, file_hashes, read_json, safe_name, write_json

ZIP_DATE = (1980, 1, 1, 0, 0, 0)
FILE_MODE = 0o100644
DIR_MODE = 0o040755


def add_arguments(p):
    p.add_argument("--raw", required=True, type=Path, help="vendor file or directory")
    p.add_argument("--companions", nargs="*", type=Path, default=[])
    p.add_argument("--plan", required=True, type=Path)
    p.add_argument("--raw-id", required=True)
    p.add_argument("--outdir", type=Path, default=Path("."))


def link_or_copy(src: Path, dst: Path) -> None:
    """Hard link `src` (resolved) to `dst`, or copy when linking is not possible. Never a symlink."""
    src = Path(src).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def _zinfo(name: str, directory: bool = False) -> zipfile.ZipInfo:
    zi = zipfile.ZipInfo(name, date_time=ZIP_DATE)
    zi.compress_type = zipfile.ZIP_STORED
    zi.create_system = 3
    zi.external_attr = ((DIR_MODE if directory else FILE_MODE) << 16) | (0x10 if directory else 0)
    return zi


def build_zip(src_dir: Path, out: Path, root: str) -> dict[str, tuple[int, str]]:
    """Write a deterministic ZIP64 archive of `src_dir` under `root/`; returns {member: (bytes, sha256)}."""
    files = tree_files(src_dir)
    dirs = {root}
    for rel, _ in files:
        parts = rel.split("/")[:-1]
        dirs.update(f"{root}/{'/'.join(parts[:i])}" for i in range(1, len(parts) + 1))
    entries = sorted([(d + "/", None) for d in dirs] + [(f"{root}/{rel}", p) for rel, p in files])
    written = {}
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for name, path in entries:
            if path is None:
                zf.writestr(_zinfo(name, directory=True), b"")
                continue
            h, n = hashlib.sha256(), 0
            zi = _zinfo(name)
            zi.file_size = path.stat().st_size
            with open(path, "rb") as fh, zf.open(zi, "w", force_zip64=True) as dst:
                while chunk := fh.read(CHUNK):
                    h.update(chunk)
                    n += len(chunk)
                    dst.write(chunk)
            written[name] = (n, h.hexdigest())
    return written


def verify_zip(path: Path, expected: dict[str, tuple[int, str]]) -> list[str]:
    """Re-read the archive; returns a list of problems (empty = verified)."""
    problems = []
    with zipfile.ZipFile(path) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        names = [i.filename for i in infos]
        if len(names) != len(set(names)):
            problems.append("duplicate member names")
        if set(names) != set(expected):
            problems.append(f"member list differs ({len(set(names) ^ set(expected))} names)")
        for info in infos:
            if info.compress_type != zipfile.ZIP_STORED:
                problems.append(f"member not stored: {info.filename}")
            h, n = hashlib.sha256(), 0
            try:
                with zf.open(info) as fh:  # raises BadZipFile on a CRC mismatch
                    while chunk := fh.read(CHUNK):
                        h.update(chunk)
                        n += len(chunk)
            except zipfile.BadZipFile as e:
                problems.append(f"{info.filename}: {e}")
                continue
            want = expected.get(info.filename)
            if want and (n, h.hexdigest()) != want:
                problems.append(f"content differs: {info.filename}")
    return problems


def plan_entry(plan: dict, raw_id: str) -> dict:
    entry = next((r for r in plan.get("raw_files", []) if r["raw_id"] == raw_id), None)
    if entry is None:
        raise InputError(f"raw id {raw_id} is not in the plan")
    if entry["packaging"] == "none" or not entry.get("deposited"):
        raise InputError(f"raw id {raw_id}: raw data is not deposited for this study (packaging 'none')")
    return entry


def match_companions(deposited: list[str], companions: list[Path]) -> list[tuple[Path, str]]:
    """Pair companion files with the planned deposited names (by sanitised name, else by order)."""
    wanted = [Path(d).name for d in deposited]
    by_name = {safe_name(Path(c).name): c for c in companions}
    if all(w in by_name for w in wanted):
        return [(by_name[w], w) for w in wanted]
    if len(wanted) == len(companions):
        return list(zip(companions, wanted))
    raise InputError(f"{len(companions)} companion files given, {len(wanted)} planned")


def package(raw: Path, companions: list[Path], plan: dict, raw_id: str, outdir: Path) -> dict:
    entry = plan_entry(plan, raw_id)
    real = Path(raw).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    result = dict(schema="msdeposit/raw_package/1", study=plan["study"], raw_id=raw_id,
                  packaging=entry["packaging"], files=[], members=0, member_bytes=0, verified=False)
    if entry["packaging"] == "zip":
        if not real.is_dir():
            raise InputError(f"raw id {raw_id}: packaging 'zip' needs a directory")
        name = Path(entry["deposited"][0]).name
        if not name.endswith(".zip") or len(entry["deposited"]) != 1:
            raise InputError(f"raw id {raw_id}: zip packaging expects one '<name>.zip'")
        out = outdir / name
        members = build_zip(real, out, safe_name(name[:-4]))
        problems = verify_zip(out, members)
        if problems:
            raise RuntimeError(f"{raw_id}: zip verification failed: " + "; ".join(problems[:5]))
        result.update(members=len(members), member_bytes=sum(n for n, _ in members.values()), verified=True)
        result["files"].append(dict(deposited=entry["deposited"][0], bytes=out.stat().st_size,
                                    sha256=file_hashes(out, ("sha256",))["sha256"]))
        return result
    if entry["packaging"] != "copy":
        raise InputError(f"raw id {raw_id}: unknown packaging {entry['packaging']!r}")
    if not real.is_file():
        raise InputError(f"raw id {raw_id}: packaging 'copy' needs a file")
    pairs = [(Path(raw), Path(entry["deposited"][0]).name)]
    pairs += match_companions(entry["deposited"][1:], companions)
    for (src, name), deposited in zip(pairs, entry["deposited"]):
        dst = outdir / name
        link_or_copy(src, dst)
        h_src = file_hashes(Path(src).resolve(), ("sha256",))["sha256"]
        h_dst = h_src if os.path.samefile(Path(src).resolve(), dst) else file_hashes(dst, ("sha256",))["sha256"]
        if h_src != h_dst:
            raise RuntimeError(f"{raw_id}: copy of {name} differs from its source")
        n = dst.stat().st_size
        result["files"].append(dict(deposited=deposited, bytes=n, sha256=h_dst))
        result["members"] += 1
        result["member_bytes"] += n
    result["verified"] = True
    return result


def run(args) -> int:
    plan = read_json(args.plan, "msdeposit/plan/1")
    result = package(args.raw, args.companions, plan, args.raw_id, args.outdir)
    write_json(args.outdir / f"{safe_name(args.raw_id)}.raw_package.json", result)
    print(f"{args.raw_id}: {result['packaging']} -> {', '.join(Path(f['deposited']).name for f in result['files'])}")
    return 0
