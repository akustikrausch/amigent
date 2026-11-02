from amigent.inventory import diff, parse

SAMPLE = """@SYS:C/Dir|12000
dir 47.2
@SYS:Tools/My App|500
Could not find version information for 'SYS:Tools/My App'
@SYS:Libs/foo|bar.library|64
foo.library 3.1
"""


def test_parse_handles_missing_versions_and_odd_names():
    items = parse(SAMPLE)
    assert items["SYS:C/Dir"] == {"size": 12000, "version": "dir 47.2"}
    assert items["SYS:Tools/My App"]["version"] == ""
    assert items["SYS:Libs/foo|bar.library"]["version"] == "foo.library 3.1"


def test_diff_reports_changed_added_removed():
    old = {"a": {"version": "1"}, "b": {"version": "1"}}
    new = {"a": {"version": "2"}, "c": {"version": "1"}}
    assert diff(old, new) == {
        "changed": [{"path": "a", "old": "1", "new": "2"}],
        "added": ["c"],
        "removed": ["b"],
    }
