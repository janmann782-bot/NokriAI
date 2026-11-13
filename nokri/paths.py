"""Locate read-only application resources independent of the process working directory."""

from pathlib import Path

# In a source checkout, use the editable config and knowledge files.
SOURCE_ROOT = Path(__file__).resolve().parent.parent

# In a wheel or pip install, use copies packaged with the Python module.
PACKAGE_ROOT = Path(__file__).resolve().parent / "resources"


def bundled_path(relative_name: str) -> Path:
    """Get a bundled, read-only resource from checkout or installed package."""
    relative = Path(relative_name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Resource name must be relative to the project")
    for root in (SOURCE_ROOT, PACKAGE_ROOT):
        candidate = root / relative
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"Не найден ресурс Nokri AI: {relative}. "
        "Проверьте полноту установки и наличие ресурсов в GitHub"
    )
