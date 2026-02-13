import os
from functools import lru_cache


@lru_cache(maxsize=1)
def kittyhack_root() -> str:
    """Absolute path to the kittyhack repository root (directory containing `src/`)."""
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


@lru_cache(maxsize=1)
def install_base() -> str:
    """Installation base path.

    Historically kittyhack uses sibling directories under `/root/`:
    - /root/kittyhack
    - /root/pictures
    - /root/models/yolo
    - /root/labelstudio

    For relocatable installs, we treat the parent directory of the repo root as the base.
    """
    # Allow explicit override (useful for custom layouts)
    override = os.environ.get("KITTYHACK_INSTALL_BASE")
    if override:
        return os.path.abspath(override)

    return os.path.abspath(os.path.join(kittyhack_root(), ".."))


def pictures_root() -> str:
    return os.path.join(install_base(), "pictures")


def pictures_original_dir() -> str:
    return os.path.join(pictures_root(), "original_images")


def pictures_thumbnails_dir() -> str:
    return os.path.join(pictures_root(), "thumbnails")


def models_yolo_root() -> str:
    return os.path.join(install_base(), "models", "yolo")


def labelstudio_root() -> str:
    return os.path.join(install_base(), "labelstudio")
