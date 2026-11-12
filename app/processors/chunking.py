"""Group evidence segments into LLM-sized chunks without splitting a segment."""
from dataclasses import dataclass
from typing import Sequence

DEFAULT_MAX_CHARS = 6000


@dataclass(frozen=True)
class Chunk:
    index: int
    evidence_ids: tuple[str, ...]
    texts: tuple[str, ...]

    @property
    def text(self) -> str:
        return "\n\n".join(self.texts)


def build_chunks(items: Sequence[tuple[str, str]], max_chars: int = DEFAULT_MAX_CHARS) -> list[Chunk]:
    """items: (evidence_id, text) in document order. A single oversized item gets its own chunk."""
    chunks: list[Chunk] = []
    ids: list[str] = []
    texts: list[str] = []
    size = 0
    for evidence_id, text in items:
        if texts and size + len(text) > max_chars:
            chunks.append(Chunk(len(chunks), tuple(ids), tuple(texts)))
            ids, texts, size = [], [], 0
        ids.append(evidence_id)
        texts.append(text)
        size += len(text)
    if texts:
        chunks.append(Chunk(len(chunks), tuple(ids), tuple(texts)))
    return chunks
