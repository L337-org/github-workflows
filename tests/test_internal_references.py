"""The hygiene check's search for tracker keys: what it reports, and the standards it does not.

The check is loaded from the action's script, so what is tested is the script that ships.  The
keys it should report are assembled at run time, so that this file holds none for the check to
find.
"""

import pytest
from conftest import load_hygiene


def findings(tmp_path, line):
    path = tmp_path / "copyright"
    path.write_text(line + "\n", encoding="utf-8")
    return load_hygiene().check_no_internal_references(tmp_path, [path], [path])


@pytest.mark.parametrize("line", [
    "License: Unicode-DFS-2016",
    "License: GPL-3+ and Expat and Unicode-DFS-2016 and Unicode-3.0",
    "Encoded as UTF-8, per RFC-3629 and ISO-8859",
])
def test_an_external_standard_is_not_a_tracker_key(tmp_path, line):
    assert findings(tmp_path, line) == []


@pytest.mark.parametrize("before,key", [
    ("Fixes ", "XY" + "-22"),
    ("See ", "ABC" + "-1"),
    # Only the Unicode licence's name is let through, not every key ending its shape.
    ("Fixes ", "DFS" + "-2016"),
    ("MyUnicode-", "DFS" + "-2016"),
])
def test_a_tracker_key_is_reported(tmp_path, before, key):
    assert findings(tmp_path, before + key) == [f"copyright:1: tracker key {key}"]
