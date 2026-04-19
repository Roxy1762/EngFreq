"""
Abstract base class for vocabulary providers.

All concrete providers (AI, dictionary APIs, …) must implement this interface.
Consumers in vocabulary_generator.py depend only on this abstraction —
swapping or adding providers never requires changes elsewhere.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from backend.models.schemas import LemmaEntry, VocabEntry


class BaseVocabProvider(ABC):
    """
    Given a list of LemmaEntry objects and optional context text,
    return enriched VocabEntry objects.

    Implementations may make API calls, read local dictionaries, etc.
    They should be idempotent and should not raise unless truly unrecoverable.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier shown in VocabEntry.source."""
        ...

    @abstractmethod
    async def enrich(
        self,
        entries: List[LemmaEntry],
        context_text: str = "",
    ) -> List[VocabEntry]:
        """
        Enrich *entries* with definitions, POS, examples, etc.

        Args:
            entries:      Lemma entries to enrich (pre-sorted by score).
            context_text: Raw exam text for in-context example extraction.

        Returns:
            List of VocabEntry, one per input entry (order preserved where possible).
            On partial failure, return what was successfully enriched.
        """
        ...
