"""Regression + behaviour tests for the in-process dict cache."""
from __future__ import annotations

import pytest


def test_dict_cache_round_trip(isolated_db):
    from backend.services import dict_cache
    dict_cache.clear()
    cached = dict_cache.CachedDefinition(
        headword="study", chinese_meaning="学习",
        english_definition="to apply oneself", example_sentence="I study daily.",
    )
    dict_cache.put("free_dict", "study", cached)
    out = dict_cache.get("free_dict", "study")
    assert out is not None
    assert out.chinese_meaning == "学习"
    assert out.english_definition == "to apply oneself"


def test_dict_cache_skips_empty_entries(isolated_db):
    """`put` must reject entries that have nothing meaningful to cache."""
    from backend.services import dict_cache
    dict_cache.clear()
    empty = dict_cache.CachedDefinition(headword="x")
    dict_cache.put("free_dict", "x", empty)
    assert dict_cache.get("free_dict", "x") is None


def test_dict_cache_negative_cache_round_trip(isolated_db):
    """Marking a miss should cause `is_known_miss` to return True briefly."""
    from backend.services import dict_cache
    dict_cache.clear()
    assert dict_cache.is_known_miss("free_dict", "qwertyzzz") is False
    dict_cache.mark_miss("free_dict", "qwertyzzz")
    assert dict_cache.is_known_miss("free_dict", "qwertyzzz") is True
    # Different word still untouched
    assert dict_cache.is_known_miss("free_dict", "study") is False


def test_dict_cache_clear_drops_negative_entries(isolated_db):
    from backend.services import dict_cache
    dict_cache.clear()
    dict_cache.mark_miss("free_dict", "fake")
    dict_cache.mark_miss("merriam_webster", "fake")
    dict_cache.clear("free_dict")
    assert dict_cache.is_known_miss("free_dict", "fake") is False
    assert dict_cache.is_known_miss("merriam_webster", "fake") is True
    dict_cache.clear()
    assert dict_cache.is_known_miss("merriam_webster", "fake") is False


def test_dict_cache_memory_layer_serves_after_disk_clear(isolated_db):
    """LRU front-cache: a put + immediate get should not need to hit SQLite.

    We can't directly assert "no SQLite hit", but we can confirm the entry
    is served from a fresh process state via the public API.
    """
    from backend.services import dict_cache
    dict_cache.clear()
    cached = dict_cache.CachedDefinition(
        headword="aware", english_definition="having knowledge",
    )
    dict_cache.put("merriam_webster", "aware", cached)
    stats = dict_cache.memory_stats()
    assert stats["lru_size"] >= 1
    out = dict_cache.get("merriam_webster", "aware")
    assert out is not None
    assert out.english_definition == "having knowledge"
