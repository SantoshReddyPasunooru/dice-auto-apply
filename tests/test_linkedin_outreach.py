"""
Tests for pure logic in linkedin_outreach.py — job post detection and email extraction.
Ollama fallback (strategy 4) is skipped since it requires a local model.
"""
import pytest
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from linkedin_outreach import is_job_post, is_experience_match, extract_email


class TestIsJobPost:
    # Should detect job posts
    def test_hiring_keyword(self):
        assert is_job_post("We are hiring a Python developer!") is True

    def test_we_re_hiring(self):
        assert is_job_post("We're hiring a Senior Engineer") is True

    def test_looking_for(self):
        assert is_job_post("Looking for a GenAI engineer with 3+ years exp") is True

    def test_open_role(self):
        assert is_job_post("Open role: Data Scientist. DM me!") is True

    def test_opt_keyword(self):
        assert is_job_post("OPT/CPT candidates welcome. W2 or C2C.") is True

    def test_apply_now(self):
        assert is_job_post("Apply now for this great position!") is True

    def test_send_resume(self):
        assert is_job_post("Send resume to hr@company.com") is True

    def test_dm_me(self):
        assert is_job_post("DM me your resume for this role") is True

    def test_c2c(self):
        assert is_job_post("C2C or W2 — 6-month contract available") is True

    # Should NOT detect celebration/self-promo posts
    def test_congratulations_skipped(self):
        assert is_job_post("Congratulations to our new hire!") is False

    def test_i_joined_skipped(self):
        assert is_job_post("Excited to share — I joined Google today!") is False

    def test_promoted_to_skipped(self):
        assert is_job_post("Promoted to Senior Engineer at my company") is False

    def test_new_role_at_skipped(self):
        assert is_job_post("Starting a new role at Amazon next week") is False

    def test_happy_to_share_skipped(self):
        assert is_job_post("Happy to share I got a new job!") is False

    def test_empty_text(self):
        assert is_job_post("") is False

    def test_generic_text_no_keywords(self):
        assert is_job_post("Great weather today in Austin!") is False


class TestIsExperienceMatch:
    def test_any_experience_always_matches(self):
        assert is_experience_match("Looking for senior engineer", ["any"]) is True

    def test_empty_experience_always_matches(self):
        assert is_experience_match("Looking for senior engineer", []) is True

    def test_no_experience_mention_always_matches(self):
        # Post doesn't mention any level → let it through
        assert is_experience_match("We are hiring a Python developer", ["senior"]) is True

    def test_senior_matches_senior_post(self):
        assert is_experience_match("Looking for a senior developer", ["senior"]) is True

    def test_senior_does_not_match_junior_post(self):
        assert is_experience_match("Entry level junior developer needed", ["senior"]) is False

    def test_junior_matches_entry_level(self):
        assert is_experience_match("Looking for entry level candidates", ["junior"]) is True

    def test_multiple_levels_match_any(self):
        text = "Open to senior or staff engineers"
        assert is_experience_match(text, ["junior", "senior"]) is True

    def test_intern_matches_intern_post(self):
        assert is_experience_match("Summer intern position available", ["intern"]) is True


class TestExtractEmail:
    def test_direct_email(self):
        result = extract_email("Send your resume to john@acme.com for consideration.")
        assert result == "john@acme.com"

    def test_email_with_dots_and_plus(self):
        result = extract_email("Contact john.smith+jobs@company.io")
        assert result == "john.smith+jobs@company.io"

    def test_obfuscated_at_word(self):
        result = extract_email("Email me at john at acme dot com")
        assert result == "john@acme.com"

    def test_obfuscated_bracket_at(self):
        result = extract_email("Contact: john[@]startup.io")
        assert result == "john@startup.io"

    def test_obfuscated_paren_at(self):
        result = extract_email("Reach me at jane(at)company.co")
        assert result == "jane@company.co"

    def test_skips_noreply(self):
        result = extract_email("Sent from noreply@greenhouse.io")
        assert result is None

    def test_skips_linkedin_domain(self):
        result = extract_email("Visit linkedin.com/jobs/view/12345")
        assert result is None

    def test_no_email_returns_none(self):
        result = extract_email("This post has no contact information at all.")
        assert result is None

    def test_empty_string_returns_none(self):
        result = extract_email("")
        assert result is None

    def test_strips_trailing_dot(self):
        result = extract_email("Email john@acme.com.")
        assert result == "john@acme.com"

    def test_returns_lowercase(self):
        result = extract_email("Contact JOHN@ACME.COM for info")
        assert result == "john@acme.com"
