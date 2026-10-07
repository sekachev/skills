#!/usr/bin/env python3
"""Read-only context tools for tracked Markdown in a Git checkout."""

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Optional

DEFAULT_MAX = 12000
TICK = re.escape(chr(96))
FENCE = re.compile("^ {0,3}(" + TICK + "{3,}|~{3,})(.*)$")
ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+|$)(.*?)\s*$")


class DocsError(Exception):
    pass


def git(root, *args):
    try:
        return subprocess.run(
            ["git", "--no-optional-locks", *args], cwd=str(root), check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace",
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise DocsError(f"git {' '.join(args)} failed: {getattr(exc, 'stderr', '') or exc}".strip()) from exc


def repository(root_arg):
    try:
        root = Path(root_arg).expanduser().resolve(strict=True)
    except OSError as exc:
        raise DocsError(f"project root does not exist: {root_arg}") from exc
    if not root.is_dir():
        raise DocsError(f"project root is not a directory: {root}")
    top = Path(git(root, "rev-parse", "--show-toplevel").strip()).resolve()
    if root != top:
        raise DocsError(f"--root must be the Git checkout top level ({top})")
    revision = git(root, "rev-parse", "--verify", "--short", "HEAD").strip()
    dirty = bool(git(root, "status", "--porcelain", "--untracked-files=normal"))
    paths = sorted(p for p in git(root, "ls-files", "--cached", "-z").split("\0")
                   if p and PurePosixPath(p).suffix.casefold() == ".md")
    return root, {"revision": revision, "dirty": dirty}, paths


def safe_file(root, rel, tracked):
    pure = PurePosixPath(rel)
    if pure.is_absolute() or not pure.parts or any(p in ("", ".", "..") for p in pure.parts):
        raise DocsError(f"file must be a relative path inside the project: {rel}")
    key = pure.as_posix()
    if PurePosixPath(key).suffix.casefold() != ".md" or key not in tracked:
        raise DocsError(f"file is not tracked Markdown in this checkout: {rel}")
    path = root.joinpath(*pure.parts)
    current = root
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise DocsError(f"symlink path is not allowed: {rel}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise DocsError(f"file is missing from the working tree: {rel}") from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise DocsError(f"file is not a regular path inside the project: {rel}")
    return resolved


def read_file(path):
    try:
        size = path.stat().st_size
        with path.open("r", encoding="utf-8", errors="replace", newline=None) as stream:
            return stream.read().splitlines(), size
    except OSError as exc:
        raise DocsError(f"cannot read {path}: {exc}") from exc


def front_matter_end(lines):
    if not lines or lines[0].removeprefix("\ufeff") != "---":
        return 0
    return next((i + 1 for i in range(1, len(lines)) if lines[i] in ("---", "...")), 0)


def parse_headings(lines):
    fm_end, found, fence = front_matter_end(lines), [], None
    for number, line in enumerate(lines[fm_end:], fm_end + 1):
        match = FENCE.match(line)
        if fence:
            if match:
                marker = match.group(1)
                if marker[0] == fence[0] and len(marker) >= fence[1] and not match.group(2).strip():
                    fence = None
            continue
        if match:
            marker, info = match.groups()
            if marker[0] != chr(96) or chr(96) not in info:
                fence = (marker[0], len(marker))
            continue
        match = ATX.match(line)
        if match:
            title = re.sub(r"[ \t]+#+[ \t]*$", "", match.group(2)).strip()
            found.append({"level": len(match.group(1)), "title": title, "start_line": number})
    stack = []
    for heading in found:
        while stack and stack[-1]["level"] >= heading["level"]:
            stack.pop()["end_line"] = heading["start_line"] - 1
        heading["ancestors"] = [h["title"] for h in stack]
        heading["ancestor_lines"] = [h["start_line"] for h in stack]
        stack.append(heading)
    while stack:
        stack.pop()["end_line"] = len(lines)
    return found, fm_end


def load_docs(root, paths):
    tracked, docs = set(paths), []
    for rel in paths:
        try:
            path = safe_file(root, rel, tracked)
        except DocsError:
            continue
        lines, size = read_file(path)
        headings, fm_end = parse_headings(lines)
        docs.append({"path": rel, "lines": lines, "size": size, "fm_end": fm_end,
                     "headings": headings})
    return docs


def section_for_line(doc, number):
    matches = [h for h in doc["headings"] if h["start_line"] <= number <= h["end_line"]]
    return max(matches, key=lambda h: h["level"]) if matches else None


def heading_row(doc, heading):
    return {"path": doc["path"], "level": heading["level"], "title": heading["title"],
            "start_line": heading["start_line"], "end_line": heading["end_line"],
            "section_path": heading["ancestors"] + [heading["title"]]}


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def read_continuation(path, line, end, offset=0):
    command = f"read {shlex.quote(path)} --lines {line}:{end}"
    return command + (f" --column-offset {offset}" if offset else "")


def pack_read(base, records, limit, max_chars, header, path, end):
    """Choose one shared set of source lines for both renderers."""
    all_records = records
    omitted_by_limit = max(0, len(records) - limit)
    records = records[:limit]
    result = dict(base, lines=[], truncated=False, omitted_count=0, continuation=None)

    def fits(candidate, next_line, next_offset, omitted):
        candidate["omitted_count"] = omitted
        candidate["truncated"] = bool(omitted)
        candidate["continuation"] = (read_continuation(path, next_line, end, next_offset)
                                     if omitted else None)
        json_fits = len(dump(candidate)) + 1 <= max_chars
        text_body = sum(len(r["text"]) + 12 for r in candidate["lines"])
        text_mark = (len(candidate["continuation"]) + 45) if omitted else 0
        text_fits = len(header) + text_body + text_mark + 8 <= max_chars
        return json_fits and text_fits

    for i, record in enumerate(records):
        number, value = record["line"], record["text"]
        offset = record.get("char_start", 0)
        remaining = len(records) - i - 1 + omitted_by_limit
        full = dict(record, text=value[offset:], char_start=offset, char_end=len(value))
        trial = dict(result)
        trial["lines"] = result["lines"] + [full]
        next_record = all_records[i + 1] if remaining else None
        next_line = next_record["line"] if remaining else 0
        next_offset = next_record.get("char_start", 0) if remaining else 0
        if fits(trial, next_line, next_offset, remaining):
            result = trial
            continue
        # Preserve as much of an overlong line as fits, with a resumable character offset.
        low, high, best = 0, len(value) - offset, None
        while low <= high:
            middle = (low + high) // 2
            partial = dict(record, text=value[offset:offset + middle],
                           char_start=offset, char_end=offset + middle)
            trial = dict(result)
            trial["lines"] = result["lines"] + ([partial] if middle else [])
            if fits(trial, number, offset + middle,
                    len(records) - i + omitted_by_limit):
                best, low = trial, middle + 1
            else:
                high = middle - 1
        if best:
            result = best
        else:
            result["omitted_count"] = len(records) - i + omitted_by_limit
            result["truncated"] = True
            result["continuation"] = read_continuation(path, number, end, offset)
        break
    return result


def render_read_text(packed, header):
    output = list(header)
    output.extend(f"{r['line']:>6}: {r['text']}" for r in packed["lines"])
    if packed["truncated"]:
        output.append(f"... {packed['omitted_count']} source line(s) omitted; continue with: "
                      f"{packed['continuation']}")
    return "\n".join(output)


def introduction(doc):
    headings = doc["headings"]
    boundary = next((h["start_line"] - 1 for h in headings if h["level"] > 1), len(doc["lines"]))
    if not any(h["level"] == 1 for h in headings):
        boundary = min(boundary, headings[0]["start_line"] - 1) if headings else len(doc["lines"])
    return set(range(doc["fm_end"] + 1, boundary + 1))


def command_read(args, root, repo, paths):
    path = safe_file(root, args.file, set(paths))
    rel = path.relative_to(root).as_posix()
    lines, size = read_file(path)
    headings, fm_end = parse_headings(lines)
    doc = {"path": rel, "lines": lines, "fm_end": fm_end, "headings": headings}
    if args.section is not None:
        found = [h for h in headings if h["title"] == args.section]
        if not found:
            raise DocsError(f"section title not found in {rel}: {args.section!r}")
        if len(found) > 1:
            raise DocsError("section title is ambiguous at lines " +
                            ", ".join(str(h["start_line"]) for h in found) +
                            "; use --heading-line")
        target = found[0]
        start, end, selector = target["start_line"], target["end_line"], "section"
    elif args.heading_line is not None:
        target = next((h for h in headings if h["start_line"] == args.heading_line), None)
        if target is None:
            raise DocsError(f"line {args.heading_line} is not an ATX heading in {rel}")
        start, end, selector = target["start_line"], target["end_line"], "heading-line"
    elif args.lines is not None:
        start, end = args.lines
        if end > len(lines):
            raise DocsError(f"line range ends at {end}, but {rel} has {len(lines)} lines")
        target, selector = None, "lines"
    else:
        start, end = (1, len(lines)) if lines else (0, 0)
        target, selector = section_for_line(doc, start) if start else None, "document"
    if args.column_offset is not None and selector != "lines":
        raise DocsError("--column-offset requires an explicit --lines range")

    # Explicit line reads contain only that range; section reads also carry source context.
    included = set(range(start, end + 1)) if start else set()
    ancestor_headings = []
    if selector != "lines":
        included.update(range(1, fm_end + 1))
        included.update(introduction(doc))
        if target:
            ancestor_lines = set(target["ancestor_lines"])
            if selector != "section":
                ancestor_lines.add(target["start_line"])
            ancestor_headings = [h for h in headings if h["start_line"] in ancestor_lines]
            included.update(h["start_line"] for h in ancestor_headings)
    records = []
    for number in sorted(included):
        if 1 <= number <= len(lines):
            kinds = []
            if number <= fm_end:
                kinds.append("front_matter")
            if number in introduction(doc):
                kinds.append("introduction")
            if start <= number <= end:
                kinds.append("selection")
            if number in {h["start_line"] for h in ancestor_headings}:
                kinds.append("ancestor_heading")
            record = {"line": number, "text": lines[number - 1], "kind": kinds or ["context"]}
            if number == start and args.column_offset is not None:
                record["char_start"] = args.column_offset
            records.append(record)

    source = {"path": rel, **repo, "size_bytes": size, "line_count": len(lines)}
    base = {"command": "read", "source": source,
            "selection": {"type": selector, "start_line": start, "end_line": end},
            "ancestors": [heading_row(doc, h) for h in ancestor_headings]}
    header = [f"{rel} | HEAD {repo['revision']} | {'dirty' if repo['dirty'] else 'clean'}",
              f"Selection: {selector} lines {start}-{end} | {size} bytes, {len(lines)} lines"]
    if ancestor_headings:
        header.append("Context: " + " > ".join(h["title"] for h in ancestor_headings))
    packed = pack_read(base, records, args.limit, args.max_chars, "\n".join(header), rel,
                       end or len(lines))
    rendered = dump(packed) if args.json else render_read_text(packed, header)
    if len(rendered) > args.max_chars:
        raise DocsError("--max-chars is too small for source metadata; increase it")
    return rendered


def output_object(command, base, records, limit, max_chars, continuation):
    shown = records[:limit]
    omitted_by_limit = len(records) - len(shown)
    result = dict(base, **{command: shown}, truncated=bool(omitted_by_limit),
                  omitted_count=omitted_by_limit, continuation=continuation if omitted_by_limit else None)
    for i, record in enumerate(shown):
        trial = dict(result)
        remaining = omitted_by_limit + len(shown) - i - 1
        trial["omitted_count"] = remaining
        trial["truncated"] = bool(remaining)
        trial["continuation"] = continuation if remaining else None
        if len(dump(trial)) > max_chars:
            result[command] = shown[:i]
            result["omitted_count"] = len(records) - i
            result["truncated"] = True
            result["continuation"] = continuation
            break
    if len(dump(result)) > max_chars:
        raise DocsError("--max-chars is too small for the response metadata; increase it")
    return result


def command_index(args, root, repo, paths):
    docs = load_docs(root, paths)
    selected = docs[:args.limit]
    files = [{"path": d["path"], "size_bytes": d["size"], "line_count": len(d["lines"]),
              "heading_count": len(d["headings"])} for d in selected]
    all_heads = [heading_row(d, h) for d in selected for h in d["headings"] if h["level"] <= args.depth]
    continuation = f"index --depth 6 --limit {max(len(docs), len(all_heads))} --max-chars {max(args.max_chars * 2, 24000)}"
    if args.json:
        base = {"command": "index", "repository": repo, "total_files": len(docs),
                "files_omitted": len(docs) - len(selected), "files": files}
        result = output_object("outline", base, all_heads, args.limit, args.max_chars, continuation)
        result["omitted_count"] += len(docs) - len(selected)
        if len(docs) > len(selected):
            result["truncated"], result["continuation"] = True, continuation
        if len(dump(result)) + 1 > args.max_chars:
            raise DocsError("--max-chars is too small for index metadata; increase it")
        return dump(result)
    rows = [f"{d['path']} ({d['size']} bytes, {len(d['lines'])} lines, {len(d['headings'])} headings)"
            for d in selected]
    rows.extend(f"  {'#' * h['level']} {h['title']} [{h['path']}:{h['start_line']}-{h['end_line']}]"
                for h in all_heads[:args.limit])
    header = [f"HEAD {repo['revision']} | {'dirty' if repo['dirty'] else 'clean'}",
              f"Markdown files: {len(docs)} (showing {len(selected)}); outline depth: {args.depth}",
              f"Headings shown: {min(len(all_heads), args.limit)} of {len(all_heads)}"]
    if len(docs) > len(selected):
        header.append(f"{len(docs) - len(selected)} file(s) omitted by --limit")
    if len(all_heads) > args.limit:
        header.append(f"{len(all_heads) - args.limit} heading(s) omitted by --limit")
    return bounded_text(header, rows, args.max_chars, continuation)


def command_search(args, root, repo, paths):
    docs, matches, query = load_docs(root, paths), [], args.query.casefold()
    for doc in docs:
        if query in doc["path"].casefold():
            matches.append({"kind": "path", "path": doc["path"], "line": None,
                            "excerpt": doc["path"], "section_path": []})
        for number, text in enumerate(doc["lines"], 1):
            heading = next((h for h in doc["headings"] if h["start_line"] == number
                            and query in h["title"].casefold()), None)
            if query not in text.casefold() and heading is None:
                continue
            section = section_for_line(doc, number)
            offset = text.casefold().find(query)
            offset = max(offset, 0)
            excerpt_start = max(0, offset - 160)
            excerpt_end = min(len(text), excerpt_start + 600)
            matches.append({"kind": "heading" if heading else "body", "path": doc["path"],
                            "line": number, "excerpt": text[excerpt_start:excerpt_end],
                            "character_offset": excerpt_start,
                            "excerpt_truncated": excerpt_start > 0 or excerpt_end < len(text),
                            "section_path": section["ancestors"] + [section["title"]] if section else [],
                            "section_start": section["start_line"] if section else None,
                            "section_end": section["end_line"] if section else None})
    continuation = (f"search {shlex.quote(args.query)} --limit {len(matches)} "
                    f"--max-chars {max(args.max_chars * 2, 24000)}")
    base = {"command": "search", "query": args.query, "repository": repo, "total_matches": len(matches)}
    if args.json:
        return dump(output_object("matches", base, matches, args.limit, args.max_chars, continuation))
    shown = matches[:args.limit]
    header = [f"HEAD {repo['revision']} | {'dirty' if repo['dirty'] else 'clean'}",
              f"Matches for {args.query!r}: {len(matches)} (showing {len(shown)})"]
    if len(matches) > len(shown):
        header.append(f"{len(matches) - len(shown)} match(es) omitted by --limit; use --limit {len(matches)}")
    rows = [f"{m['path']}{':' + str(m['line']) if m['line'] else ''} "
            f"[{' > '.join(m['section_path']) or 'document preamble'}] {m['excerpt']}"
            for m in shown]
    return bounded_text(header, rows, args.max_chars, continuation)


def positive(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def depth_value(value):
    number = positive(value)
    if number > 6:
        raise argparse.ArgumentTypeError("must be between 1 and 6")
    return number


def line_range(value):
    match = re.fullmatch(r"([1-9][0-9]*):([1-9][0-9]*)", value)
    if not match or int(match.group(1)) > int(match.group(2)):
        raise argparse.ArgumentTypeError("expected inclusive one-based START:END")
    return tuple(map(int, match.groups()))


def bounded_budget(value):
    number = positive(value)
    if number < 1024:
        raise argparse.ArgumentTypeError("must be at least 1024 to preserve continuation metadata")
    return number


def add_output(parser, with_limit=True):
    parser.add_argument("--json", action="store_true", help="emit structured JSON")
    if with_limit:
        parser.add_argument("--limit", type=positive, default=20, help="maximum files or result records")
    parser.add_argument("--max-chars", type=bounded_budget, default=DEFAULT_MAX,
                        help=f"maximum output characters (default {DEFAULT_MAX}; minimum 1024)")


def build_parser():
    parser = argparse.ArgumentParser(description="Read-only Markdown context tools for a Git project")
    parser.add_argument("--root", required=True, help="top-level path of the project Git checkout")
    commands = parser.add_subparsers(dest="command", required=True)
    index = commands.add_parser("index", help="list tracked Markdown files and ATX outline")
    add_output(index)
    index.add_argument("--depth", type=depth_value, default=2,
                       help="maximum heading level shown (default 2; use 6 for all)")
    search = commands.add_parser("search", help="find a literal case-insensitive phrase")
    search.add_argument("query")
    add_output(search)
    read = commands.add_parser("read", help="read a file, section, heading, or line range")
    read.add_argument("file")
    group = read.add_mutually_exclusive_group()
    group.add_argument("--section")
    group.add_argument("--heading-line", type=positive)
    group.add_argument("--lines", type=line_range)
    read.add_argument("--column-offset", type=int,
                      help="zero-based offset for the first line in an explicit --lines range")
    add_output(read)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "column_offset", None) is not None and args.column_offset < 0:
        parser.error("--column-offset must be zero or greater")
    try:
        root, repo, paths = repository(args.root)
        if args.command == "index":
            result = command_index(args, root, repo, paths)
        elif args.command == "search":
            result = command_search(args, root, repo, paths)
        else:
            result = command_read(args, root, repo, paths)
        if len(result) + 1 > args.max_chars:
            raise DocsError("output exceeds --max-chars; increase the budget or narrow the selection")
        print(result)
        return 0
    except DocsError as exc:
        print(f"docs.py: error: {exc}", file=sys.stderr)
        return 2


def bounded_text(header, rows, max_chars, continuation):
    lines = list(header)
    output, used = [], 0
    for line in lines:
        if used + len(line) + (1 if output else 0) > max_chars - 160:
            break
        output.append(line)
        used += len(line) + (1 if len(output) > 1 else 0)
    omitted = 0
    for i, row in enumerate(rows):
        if used + len(row) + 1 <= max_chars - 160:
            output.append(row)
            used += len(row) + 1
        else:
            omitted = len(rows) - i
            break
    if omitted:
        output.append(f"... {omitted} row(s) omitted; continue with: {continuation}")
        while len("\n".join(output)) > max_chars and len(output) > 1:
            output.pop(-2)
            omitted += 1
            output[-1] = f"... {omitted} row(s) omitted; continue with: {continuation}"
        if len("\n".join(output)) > max_chars:
            output = ["... output omitted; increase --max-chars and rerun the command"]
    return "\n".join(output)


if __name__ == "__main__":
    raise SystemExit(main())
