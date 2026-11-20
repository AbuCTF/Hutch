import os
import re

import hutch


def test_version_is_the_one_in_pyproject():
    root = os.path.dirname(os.path.dirname(__file__))
    with open(os.path.join(root, "pyproject.toml")) as f:
        declared = re.search(r'^version = "([^"]+)"', f.read(), re.M).group(1)
    assert hutch.__version__ == declared
