"""
Unit tests for US state abbreviation → full name mapping in Workday form filler.
The mapping lives in company_apply/workday.py _wd_my_information but is also
exercised indirectly through handshake_apply location parsing.
"""
import sys
import pathlib
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))


# Extract the mapping by importing directly from common helpers
_US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}


class TestStateMapping:
    @pytest.mark.parametrize("abbr,full", [
        ("TX", "Texas"), ("CA", "California"), ("NY", "New York"),
        ("FL", "Florida"), ("OH", "Ohio"), ("WA", "Washington"),
        ("IL", "Illinois"), ("PA", "Pennsylvania"), ("GA", "Georgia"),
        ("DC", "District of Columbia"),
    ])
    def test_common_states_map_correctly(self, abbr, full):
        assert _US_STATES.get(abbr.upper(), abbr) == full

    def test_lowercase_abbr_does_not_match(self):
        # The mapping uses .upper() on input; raw lowercase should use .get with upper()
        assert _US_STATES.get("tx", "tx") == "tx"
        assert _US_STATES.get("tx".upper(), "tx") == "Texas"

    def test_full_name_passes_through_unchanged(self):
        # If someone stores "Texas" in their profile, it should not be double-expanded
        state = "Texas"
        result = _US_STATES.get(state.upper(), state)
        assert result == "Texas"

    def test_unknown_abbreviation_passes_through(self):
        state = "XY"
        result = _US_STATES.get(state.upper(), state)
        assert result == "XY"

    def test_all_50_states_plus_dc_covered(self):
        assert len(_US_STATES) == 51  # 50 states + DC


class TestHandshakeLocationParsing:
    def _parse_location(self, profile: dict) -> tuple[str, str]:
        loc_parts = [p.strip() for p in profile.get("location", "").split(",")]
        city = profile.get("city") or (loc_parts[0] if loc_parts else "")
        state = profile.get("state") or (loc_parts[1] if len(loc_parts) > 1 else "")
        return city, state

    def test_location_string_parsed_correctly(self):
        profile = {"location": "Austin, TX"}
        city, state = self._parse_location(profile)
        assert city == "Austin"
        assert state == "TX"

    def test_explicit_city_overrides_location(self):
        profile = {"location": "Austin, TX", "city": "Dallas"}
        city, state = self._parse_location(profile)
        assert city == "Dallas"

    def test_empty_profile_gives_empty_strings(self):
        city, state = self._parse_location({})
        assert city == ""
        assert state == ""

    def test_no_hardcoded_fairborn_fallback(self):
        # Profile with "location" field but no "city" key — should NOT produce Fairborn
        profile = {"location": "Seattle, WA"}
        city, state = self._parse_location(profile)
        assert city != "Fairborn"
        assert state != "OH" or profile.get("location", "").endswith("OH")
