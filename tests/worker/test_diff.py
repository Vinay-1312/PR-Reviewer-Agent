from pr_reviewer.github.diff import annotate_diff, truncate

DIFF = "\n".join(
    [
        "diff --git a/app.py b/app.py",
        "index 1..2 100644",
        "--- a/app.py",
        "+++ b/app.py",
        "@@ -1,4 +1,5 @@",
        " import os",
        "+import sys",
        " ",
        "-old = 1",
        "+new = 2",
        " def main():",
        "diff --git a/gone.py b/gone.py",
        "--- a/gone.py",
        "+++ /dev/null",
        "@@ -1,2 +0,0 @@",
        "-a",
        "-b",
        "diff --git a/lib/util.py b/lib/util.py",
        "--- a/lib/util.py",
        "+++ b/lib/util.py",
        "@@ -10,2 +10,2 @@ def f():",
        " keep",
        "+++ not a header",
    ]
)


def test_commentable_lines_use_new_file_numbers():
    _, commentable = annotate_diff(DIFF)
    assert commentable["app.py"] == {1, 2, 3, 4, 5}
    assert commentable["lib/util.py"] == {10, 11}


def test_deleted_files_are_not_commentable():
    _, commentable = annotate_diff(DIFF)
    assert "gone.py" not in commentable


def test_annotation_prefixes_new_lines_and_pads_removed_ones():
    text, _ = annotate_diff(DIFF)
    lines = text.splitlines()
    assert "    2 +import sys" in lines
    assert "      -old = 1" in lines
    assert "    4 +new = 2" in lines


def test_added_line_that_looks_like_a_file_header_stays_in_the_hunk():
    text, commentable = annotate_diff(DIFF)
    assert "   11 +++ not a header" in text.splitlines()
    assert 11 in commentable["lib/util.py"]


def test_truncate_cuts_on_a_line_boundary():
    text = "aaaa\nbbbb\ncccc"
    assert truncate(text, 100) == text
    assert truncate(text, 7) == "aaaa\n[diff truncated]"
