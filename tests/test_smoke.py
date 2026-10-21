import re

import builder
import contracts


def test_packages_import():
    assert re.fullmatch(r"\d+\.\d+\.\d+", builder.VERSION)  # the setup docs bump it before a builder re-release
    assert contracts.__doc__
