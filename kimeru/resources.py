"""Compatibility helpers for the canonical source/install asset resolver."""
from . import paths


def resource_dir(name):
    """Return a built-in asset directory under paths.base(), without a second installation layout."""
    return paths.base() / name


def resource_path(name, *parts):
    return resource_dir(name).joinpath(*parts)
