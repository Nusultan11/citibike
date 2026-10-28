from importlib import import_module
from importlib.metadata import distribution


def test_package_imports() -> None:
    package = import_module("citibike")

    assert package.__file__ is not None


def test_installed_package_metadata() -> None:
    installed = distribution("citibike")

    assert installed.metadata["Name"] == "citibike"
    assert installed.version
