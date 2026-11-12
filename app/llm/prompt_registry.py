"""Versioned prompt files: app/llm/prompts/<prompt_id>/v<N>.md. Version + hash are recorded."""
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

PROMPT_DIR = Path(__file__).parent / "prompts"
_VERSION = re.compile(r"v(\d+)\.md$")


@dataclass(frozen=True)
class Prompt:
    prompt_id: str
    version: str
    text: str
    sha256: str


def load_prompt(prompt_id: str, version: str | None = None) -> Prompt:
    folder = PROMPT_DIR / prompt_id
    if version is None:
        versions = sorted((int(m.group(1)), p) for p in folder.glob("v*.md")
                          if (m := _VERSION.search(p.name)))
        if not versions:
            raise FileNotFoundError(f"no prompt versions for {prompt_id}")
        number, path = versions[-1]
        version = str(number)
    else:
        path = folder / f"v{version}.md"
    text = path.read_text(encoding="utf-8")
    return Prompt(prompt_id, version, text, hashlib.sha256(text.encode()).hexdigest())
