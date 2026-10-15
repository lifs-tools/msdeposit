"""canary-scan (check S6): post-run scan of everything under the published outdir (upload sets, pipeline_info,
reports) and of the captured console log for denylist tokens (design §8.2, §9; run from workflow.onComplete).

Uses the scanner of identifier-scan (mzML, tables, text, file and directory names) and first runs its self-test
on copies of one file per type (inside --quarantine-dir). On hits, the affected files, or the topmost directory
whose name holds a token, are moved to --quarantine-dir (same relative path); the console log is only reported.

Outputs in --check-dir (default: --quarantine-dir; it must be private, so neither directory may lie under --root):
check_S6.json (public counts), check_S6.private.json and canary_hits.tsv (private, with tokens and contexts).
Exit 0 with the status in the check file, or 3 on a failed check with --fail-exit.
"""

import shutil
from pathlib import Path

from ..io import InputError, write_check, write_table
from .denylist import Matcher, read_denylist
from .scan import HIT_FIELDS, Hits, iter_files, scan_each, scan_file, self_test


def add_arguments(p):
    p.add_argument("--denylist", required=True, type=Path)
    p.add_argument("--root", required=True, type=Path, help="published outdir")
    p.add_argument("--console-log", type=Path, help="captured console output (scanned, not moved)")
    p.add_argument("--quarantine-dir", required=True, type=Path, help="private directory for affected outputs")
    p.add_argument("--check-dir", type=Path, help="private directory for the check files (default: quarantine dir)")
    p.add_argument("--study", default="", help="study key for the check file (default: global)")
    p.add_argument("--allow-regions", choices=("contacts", "none"), default="contacts")
    p.add_argument("--jobs", type=int, default=1, help="files scanned in parallel (default 1)")
    p.add_argument("--fail-exit", action="store_true", help="exit 3 when the check fails")


def quarantine_target(root: Path, display: str, matcher: Matcher, content_hit: bool) -> Path | None:
    """The topmost path component whose name holds a token, else the file itself when its content does."""
    parts = display.split("/")
    for i in range(len(parts)):
        if matcher.search(parts[i]):
            return root.joinpath(*parts[: i + 1])
    return root.joinpath(*parts) if content_hit else None


def move(src: Path, root: Path, qdir: Path) -> None:
    dst = qdir / src.relative_to(root)
    k = 1
    while dst.exists():
        k += 1
        dst = dst.with_name(f"{src.name}.{k}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))


def run(args) -> int:
    root, qdir = args.root, args.quarantine_dir
    check_dir = args.check_dir or qdir
    if not root.is_dir():
        raise InputError("--root is not a directory")
    for d in (qdir, check_dir):
        if d.resolve() == root.resolve() or d.resolve().is_relative_to(root.resolve()):
            raise InputError("the quarantine and check directories must not lie inside --root")
    qdir.mkdir(parents=True, exist_ok=True)
    entries = read_denylist(args.denylist)
    matcher = Matcher(entries)
    allow = args.allow_regions == "contacts"
    files = iter_files([root])
    st_ok, plants, st_counts = self_test(files, entries, qdir, allow) if files else (True, [], {"planted": 0})

    hits = Hits()
    targets: set[Path] = set()
    for (_, display), h in zip(files, scan_each(files, matcher, allow, args.jobs)):
        hits.rows += h.rows
        hits.counts.update(h.counts)
        if h.total:
            t = quarantine_target(root, display, matcher, any(loc != "name" for _, loc, _ in h.counts))
            if t is not None:
                targets.add(t)
    console_hits = 0
    if args.console_log:
        if not args.console_log.is_file():
            raise InputError("--console-log not found")
        before = hits.total
        scan_file(args.console_log, "console log", matcher, hits, False)
        console_hits = hits.total - before
    # move the topmost targets only (a quarantined directory takes its files with it)
    tops = [t for t in sorted(targets, key=lambda p: len(p.parts))
            if not any(t != o and t.is_relative_to(o) for o in targets)]
    for t in tops:
        if t.exists() or t.is_symlink():
            move(t, root, qdir)
    write_table(check_dir / "canary_hits.tsv", hits.rows, HIT_FIELDS)
    problems = ([] if st_ok else ["scanner self-test failed"]) + ([] if entries else ["denylist is empty"])
    status = "fail" if hits.total or problems else "pass"
    counts = dict(files=len(files), hits=hits.total, files_with_hits=len({f for f, _, _ in hits.counts} -
                                                                          {"console log"}),
                  console_hits=console_hits, quarantined=len(tops), self_test_planted=st_counts.get("planted", 0),
                  self_test_found=st_counts.get("found", 0))
    summary = (f"{len(files)} published files scanned; {hits.total} hits ({console_hits} in the console log); "
               f"{len(tops)} outputs quarantined") + ("; " + "; ".join(problems) if problems else "")
    details = [dict(file=f, location=loc, token=t, count=n) for (f, loc, t), n in sorted(hits.counts.items())]
    details += [dict(quarantined=t.relative_to(root).as_posix()) for t in tops]
    if not st_ok:
        details += [dict(self_test=p) for p in plants]
    write_check(check_dir, args.study, "S6", status, counts=counts, summary=summary, details=details,
                scope="study" if args.study else "global")
    print(f"S6: {status} ({summary})")
    return 3 if args.fail_exit and status == "fail" else 0
