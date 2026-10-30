import re

HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def annotate_diff(diff: str) -> tuple[str, dict[str, set[int]]]:
    """Prefix each new-file line of a unified diff with its line number.

    Returns the annotated text for the model, and the set of line numbers per file path
    that GitHub will accept an inline review comment on (added and context lines).
    """
    out: list[str] = []
    commentable: dict[str, set[int]] = {}
    path: str | None = None
    line_no = 0
    in_hunk = False

    for raw in diff.splitlines():
        if raw.startswith("diff --git"):
            in_hunk = False
            path = None
            out.append(raw)
            continue

        hunk = HUNK_HEADER.match(raw)
        if hunk:
            line_no = int(hunk.group(1))
            in_hunk = True
            out.append(raw)
            continue

        if not in_hunk:
            if raw.startswith("+++ "):
                target = raw[4:].strip()
                if target == "/dev/null":
                    path = None
                else:
                    path = target[2:] if target.startswith("b/") else target
            out.append(raw)
            continue

        marker = raw[:1]
        if marker == "-":
            out.append(" " * 6 + raw)
        elif marker == "\\":
            out.append(raw)
        else:  # added line, context line, or a context line whose leading space was stripped
            if path:
                commentable.setdefault(path, set()).add(line_no)
            out.append(f"{line_no:>5} {raw}")
            line_no += 1

    return "\n".join(out), commentable


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit("\n", 1)[0] + "\n[diff truncated]"
