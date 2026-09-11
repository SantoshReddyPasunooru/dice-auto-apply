"""
Tests for edge cases in degree-keyword extraction and grad_yr parsing
that were identified as crash-prone (BUG-3, BUG-6).

These are pure logic tests — no browser required.
"""
import re
import sys
import pathlib
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))


def _deg_kw(degree: str) -> str:
    """Mirror the logic from workday.py _wd_my_experience."""
    words = degree.lower().split()
    return "master" if "master" in degree.lower() else (words[0] if words else "bachelor")


def _parse_grad_yr(grad_yr) -> int | None:
    """Mirror the safe int-parsing logic from workday.py."""
    try:
        return int(re.sub(r"[^\d]", "", str(grad_yr))[:4])
    except (ValueError, TypeError):
        return None


class TestDegreeKeywordExtraction:
    def test_empty_string_returns_bachelor_fallback(self):
        assert _deg_kw("") == "bachelor"

    def test_none_would_crash_but_workday_uses_empty(self):
        # profile.get("degree", "") always returns str — simulate
        assert _deg_kw("") == "bachelor"

    def test_master_detected_in_any_case(self):
        assert _deg_kw("Master of Science") == "master"
        assert _deg_kw("MASTER'S") == "master"
        assert _deg_kw("master") == "master"

    def test_bachelor_returns_first_word(self):
        assert _deg_kw("Bachelor of Science") == "bachelor"

    def test_single_word_degree(self):
        assert _deg_kw("PhD") == "phd"

    def test_associate_degree(self):
        assert _deg_kw("Associate Degree") == "associate"


class TestGraduationYearParsing:
    def test_clean_year_parses(self):
        assert _parse_grad_yr("2026") == 2026

    def test_year_with_suffix_parses(self):
        assert _parse_grad_yr("2026 (expected)") == 2026

    def test_month_year_string_parses_year(self):
        assert _parse_grad_yr("May 2026") == 2026

    def test_int_input_works(self):
        assert _parse_grad_yr(2026) == 2026

    def test_none_returns_none(self):
        assert _parse_grad_yr(None) is None

    def test_empty_string_returns_none(self):
        assert _parse_grad_yr("") is None

    def test_only_letters_returns_none(self):
        assert _parse_grad_yr("TBD") is None

    def test_first_four_digits_taken(self):
        # "20261" would truncate to "2026"
        assert _parse_grad_yr("20261") == 2026
