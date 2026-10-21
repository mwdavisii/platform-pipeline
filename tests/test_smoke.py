import builder
import contracts


def test_packages_import():
    assert builder.VERSION == "0.2.0"
    assert contracts.__doc__
