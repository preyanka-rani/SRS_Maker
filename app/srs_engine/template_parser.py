"""Reads the template manifest (sections, generation methods, status labels)."""
from pathlib import Path

import yaml


def load_manifest(path: str = "templates/srs_base_template.manifest.yaml") -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))
