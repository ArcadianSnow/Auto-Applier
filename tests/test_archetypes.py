"""Tests for archetype definitions, loading, saving, and resume filtering."""
import json

import pytest

from auto_applier.resume import archetypes as arch
from auto_applier.resume.archetypes import (
    Archetype,
    CONFIDENCE_THRESHOLD,
    ClassificationResult,
    filter_resumes_by_archetype,
    load_archetypes,
    resume_archetypes,
    save_archetypes,
)


@pytest.fixture
def tmp_archetypes_file(tmp_path, monkeypatch):
    """Redirect ARCHETYPES_FILE to a temp path."""
    monkeypatch.setattr(arch, "ARCHETYPES_FILE", tmp_path / "archetypes.json")
    return tmp_path / "archetypes.json"


class TestLoadArchetypes:
    def test_missing_file_returns_empty(self, tmp_archetypes_file):
        assert load_archetypes() == []

    def test_malformed_json_returns_empty(self, tmp_archetypes_file):
        tmp_archetypes_file.write_text("{ not json")
        assert load_archetypes() == []

    def test_valid_file_loads(self, tmp_archetypes_file):
        tmp_archetypes_file.write_text(json.dumps({
            "archetypes": [
                {"name": "data_analyst", "description": "SQL and dashboards"},
                {"name": "ml_engineer", "description": "Model training", "keywords": ["pytorch", "tensorflow"]},
            ]
        }))
        archs = load_archetypes()
        assert len(archs) == 2
        assert archs[0].name == "data_analyst"
        assert archs[0].description == "SQL and dashboards"
        assert archs[1].keywords == ["pytorch", "tensorflow"]

    def test_empty_archetypes_list(self, tmp_archetypes_file):
        tmp_archetypes_file.write_text(json.dumps({"archetypes": []}))
        assert load_archetypes() == []

    def test_skips_entries_without_name(self, tmp_archetypes_file):
        tmp_archetypes_file.write_text(json.dumps({
            "archetypes": [
                {"name": "valid", "description": "ok"},
                {"description": "no name"},
                {"name": "", "description": "empty name"},
            ]
        }))
        archs = load_archetypes()
        assert len(archs) == 1
        assert archs[0].name == "valid"


class TestSaveArchetypes:
    def test_round_trip(self, tmp_archetypes_file):
        archs = [
            Archetype("data_analyst", "SQL and dashboards"),
            Archetype("ml_engineer", "Model training", ["pytorch"]),
        ]
        save_archetypes(archs)
        loaded = load_archetypes()
        assert len(loaded) == 2
        assert loaded[0].name == "data_analyst"
        assert loaded[1].keywords == ["pytorch"]


class TestResumeArchetypes:
    def test_missing_field_is_empty(self):
        assert resume_archetypes({}) == []

    def test_non_list_is_empty(self):
        assert resume_archetypes({"archetypes": "not a list"}) == []

    def test_strips_whitespace_and_empties(self):
        profile = {"archetypes": ["  data_analyst  ", "", "  "]}
        assert resume_archetypes(profile) == ["data_analyst"]

    def test_coerces_non_strings(self):
        profile = {"archetypes": ["a", 123, None]}
        result = resume_archetypes(profile)
        assert "a" in result
        # 123 is coerced to "123", None to "None"
        assert "123" in result


class TestFilterResumesByArchetype:
    def _pair(self, label: str, tags: list[str] | None):
        class Stub: pass
        r = Stub()
        r.label = label
        profile = {"archetypes": tags} if tags is not None else {}
        return (r, profile)

    def test_empty_target_returns_all(self):
        resumes = [
            self._pair("da", ["data_analyst"]),
            self._pair("ml", ["ml_engineer"]),
        ]
        assert len(filter_resumes_by_archetype(resumes, "")) == 2

    def test_exact_match(self):
        resumes = [
            self._pair("da", ["data_analyst"]),
            self._pair("ml", ["ml_engineer"]),
        ]
        kept = filter_resumes_by_archetype(resumes, "data_analyst")
        assert len(kept) == 1
        assert kept[0][0].label == "da"

    def test_untagged_is_wildcard(self):
        resumes = [
            self._pair("da", ["data_analyst"]),
            self._pair("legacy", None),  # no archetypes field at all
        ]
        kept = filter_resumes_by_archetype(resumes, "data_analyst")
        assert len(kept) == 2
        labels = {r.label for r, _ in kept}
        assert labels == {"da", "legacy"}

    def test_empty_tags_is_wildcard(self):
        resumes = [
            self._pair("da", ["data_analyst"]),
            self._pair("empty", []),
        ]
        kept = filter_resumes_by_archetype(resumes, "data_analyst")
        assert len(kept) == 2

    def test_multi_tagged(self):
        resumes = [
            self._pair("multi", ["data_analyst", "ml_engineer"]),
        ]
        assert len(filter_resumes_by_archetype(resumes, "data_analyst")) == 1
        assert len(filter_resumes_by_archetype(resumes, "ml_engineer")) == 1
        assert len(filter_resumes_by_archetype(resumes, "backend")) == 0


class TestClassificationResult:
    def test_fields(self):
        r = ClassificationResult(archetype="x", confidence=0.8, reason="because")
        assert r.archetype == "x"
        assert r.confidence == 0.8

    def test_threshold_constant(self):
        # Sanity check — the threshold should be in the "more likely
        # than not" zone, not at 0 or 1.
        assert 0.4 < CONFIDENCE_THRESHOLD < 0.9


class TestArchetypeClassifier:
    def test_disabled_when_no_archetypes_defined(self, tmp_archetypes_file):
        import asyncio
        from unittest.mock import MagicMock

        router = MagicMock()
        classifier = arch.ArchetypeClassifier(router)
        result = asyncio.run(classifier.classify("any job"))
        assert result.archetype == ""
        assert result.confidence == 0.0
        router.complete_json.assert_not_called()

    def test_unknown_archetype_name_rejected(self, tmp_archetypes_file):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        router = MagicMock()
        router.complete_json = AsyncMock(return_value={
            "archetype": "made_up_category",
            "confidence": 0.9,
        })
        archs = [Archetype("data_analyst", "SQL"), Archetype("ml_engineer", "ML")]
        classifier = arch.ArchetypeClassifier(router)
        result = asyncio.run(classifier.classify("any job", archs))
        assert result.archetype == ""
        assert result.confidence == 0.9  # confidence still returned

    def test_clamps_out_of_range_confidence(self, tmp_archetypes_file):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        router = MagicMock()
        router.complete_json = AsyncMock(return_value={
            "archetype": "data_analyst",
            "confidence": 1.5,
        })
        archs = [Archetype("data_analyst", "SQL")]
        classifier = arch.ArchetypeClassifier(router)
        result = asyncio.run(classifier.classify("job", archs))
        assert result.confidence == 1.0

    def test_llm_exception_returns_empty(self, tmp_archetypes_file):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        router = MagicMock()
        router.complete_json = AsyncMock(side_effect=RuntimeError("LLM down"))
        archs = [Archetype("data_analyst", "SQL")]
        classifier = arch.ArchetypeClassifier(router)
        result = asyncio.run(classifier.classify("job", archs))
        assert result.archetype == ""
        assert result.confidence == 0.0
