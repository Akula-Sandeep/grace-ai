"""Server-sent-event chunks inside the agent's text stream.

The agent yields answer text and control events ("event: status", "event:
references", "event: state", ...) through one stream. Every consumer must tell
them apart the same way; a hand-written list of event names was missing new
events, so "event: state" leaked into stored answers.
"""

from __future__ import annotations


def is_event(chunk: str) -> bool:
    return isinstance(chunk, str) and chunk.startswith("event: ")


def answer_text(chunks) -> str:
    return "".join(chunk for chunk in chunks if not is_event(chunk))


def named_references(answer: str, references: list[dict]) -> list[dict]:
    """The references the answer names, in their order.

    Longer titles are matched first and taken out of the text, so "Dune" is not named by
    "Dune: Part Two" (a card for the 2021 film under an answer about the sequel).
    """
    from app.services.movie_data_fallback import loose_title

    text = f" {loose_title(answer or '')} "
    named: set[int] = set()
    order = sorted(
        range(len(references)), key=lambda i: -len(loose_title(str(references[i].get("title") or "")))
    )
    for index in order:
        name = loose_title(str(references[index].get("title") or ""))
        if name and f" {name} " in text:
            named.add(index)
            text = text.replace(f" {name} ", "  ")
    return [ref for i, ref in enumerate(references) if i in named]
