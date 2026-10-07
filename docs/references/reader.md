# Markdown reader contract

The optional helper in scripts/docs.py is a small, read-only index, literal search, and reader for project context. It requires Python 3.9+, Git, a checkout top-level --root, and an existing HEAD. The option order is intentional:

    python3 docs/scripts/docs.py --root /path/to/project index
    python3 docs/scripts/docs.py --root /path/to/project index --depth 6
    python3 docs/scripts/docs.py --root /path/to/project search "deployment contract"
    python3 docs/scripts/docs.py --root /path/to/project read docs/guide.md --section "Deployment"
    python3 docs/scripts/docs.py --root /path/to/project read docs/guide.md --lines 12:30

It enumerates Git tracked paths ending in .md case-insensitively and reads their current working-tree contents. It reports the short HEAD and whether Git reports a dirty checkout. Untracked Markdown is excluded. Git calls disable optional locks; the helper does not change files, the index, or repository state and makes no network requests. Files outside the checkout, untracked paths, and paths containing a symlink component are rejected.

index shows tracked file sizes, line counts, and ATX heading ranges. Heading levels and ancestor paths are retained. --depth filters displayed headings by ATX level, defaults to 2, and accepts 1 through 6. --limit caps displayed files and outline records; omitted results are reported. search performs a case-insensitive literal search over paths and document lines, including heading text. Results contain a source line excerpt, character offset, and containing section path and range. Long matching lines show a bounded excerpt around the match.

read accepts one selector: --section TITLE, --heading-line N, or inclusive one-based --lines START:END. A duplicate section title is an error; use its heading line to select one. Section reads include the selected heading and descendants through the line before the next heading at the same or higher level. They also include the original opening front matter, the document introduction through the first non-H1 heading, and ancestor headings for context. An explicit --lines read returns only those lines plus source metadata. Without a selector, read returns the document. --limit caps source-line records returned by read and search matches; omitted results are reported.

Output defaults to 12,000 characters. --max-chars accepts values from 1,024 upward. --json returns structured records with truncated, omitted_count, and continuation fields. Text output labels omissions and gives a continuation command. A long source line can be resumed with --lines N:N --column-offset K; the offset is zero-based and applies to the first line of an explicit --lines range. --lines reads only the requested range, so continuing does not re-add a large introduction or front matter.

The parser recognizes ATX headings from H1 through H6 outside fenced code blocks. A fence uses a run of at least three backticks or tildes, indented at most three spaces; its closing run must use the same character and be at least as long. A closed front-matter block is recognized only when the first line is --- and a later line is exactly --- or .... It is treated as opaque raw text and is returned unchanged by contextual reads; no YAML parsing occurs. Setext headings, HTML blocks, inline Markdown semantics, and the rest of CommonMark are not parsed. This helper does not validate links, metadata schemas, factual accuracy, or document quality. Use project-specific tooling for those checks.
