"""
Tests for answer_for() in company_apply.py — the profile-to-form-field mapper.
"""
import pytest
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from company_apply import answer_for

EMAIL = "santoshpasunoorureddy@gmail.com"

@pytest.fixture
def profile():
    return {
        "name": "Santosh Reddy Pasunooru",
        "current_title": "Gen AI Engineer",
        "current_company": "JP Morgan",
        "work_auth": "OPT",
        "needs_sponsorship": True,
        "years_experience": 5,
        "location": "Austin, TX",
        "school": "University of North Carolina - Charlotte",
        "degree": "Master's Degree",
        "graduation_year": "2024",
        "phone": "7047268793",
        "linkedin_url": "https://www.linkedin.com/in/santoshreddypas/",
        "github_url": "https://github.com/santosh",
        "website_url": "",
        "expected_salary": "open to discussion",
        "preferred_work": "Remote",
    }


class TestNameFields:
    def test_first_name(self, profile):
        assert answer_for("First Name*", profile, EMAIL) == "Santosh"

    def test_last_name(self, profile):
        assert answer_for("Last Name*", profile, EMAIL) == "Reddy Pasunooru"

    def test_full_name(self, profile):
        assert answer_for("Full Name", profile, EMAIL) == "Santosh Reddy Pasunooru"

    def test_given_name(self, profile):
        assert answer_for("Given Name", profile, EMAIL) == "Santosh"

    def test_family_name(self, profile):
        assert answer_for("Family Name", profile, EMAIL) == "Reddy Pasunooru"

    def test_surname(self, profile):
        assert answer_for("Surname", profile, EMAIL) == "Reddy Pasunooru"


class TestContactFields:
    def test_email(self, profile):
        assert answer_for("Email*", profile, EMAIL) == EMAIL

    def test_email_address(self, profile):
        assert answer_for("Email Address", profile, EMAIL) == EMAIL

    def test_phone(self, profile):
        assert answer_for("Phone*", profile, EMAIL) == "7047268793"

    def test_mobile(self, profile):
        assert answer_for("Mobile Number", profile, EMAIL) == "7047268793"

    def test_cell(self, profile):
        assert answer_for("Cell Phone", profile, EMAIL) == "7047268793"


class TestLinkFields:
    def test_linkedin(self, profile):
        assert answer_for("LinkedIn Profile", profile, EMAIL) == "https://www.linkedin.com/in/santoshreddypas/"

    def test_linkedin_url(self, profile):
        assert answer_for("LinkedIn URL", profile, EMAIL) == "https://www.linkedin.com/in/santoshreddypas/"

    def test_github(self, profile):
        assert answer_for("GitHub", profile, EMAIL) == "https://github.com/santosh"

    def test_website_empty(self, profile):
        assert answer_for("Website", profile, EMAIL) == ""

    def test_portfolio_empty(self, profile):
        assert answer_for("Portfolio URL", profile, EMAIL) == ""


class TestWorkAuthFields:
    def test_authorized_to_work(self, profile):
        assert answer_for("Are you authorized to work in the US?", profile, EMAIL) == "Yes"

    def test_legally_work(self, profile):
        assert answer_for("Are you legally authorized to work here?", profile, EMAIL) == "Yes"

    def test_sponsorship_needed_yes(self, profile):
        assert answer_for("Will you require visa sponsorship?", profile, EMAIL) == "Yes"

    def test_sponsorship_not_needed(self, profile):
        profile_no_sponsor = {**profile, "needs_sponsorship": False}
        assert answer_for("Will you require visa sponsorship?", profile_no_sponsor, EMAIL) == "No"

    def test_visa_type(self, profile):
        assert answer_for("Visa Status / Work Auth Type", profile, EMAIL) == "OPT"


class TestLocationFields:
    def test_city(self, profile):
        assert answer_for("City", profile, EMAIL) == "Austin, TX"

    def test_location(self, profile):
        assert answer_for("Location", profile, EMAIL) == "Austin, TX"

    def test_current_location(self, profile):
        assert answer_for("Current Location", profile, EMAIL) == "Austin, TX"

    def test_country_returns_us(self, profile):
        assert answer_for("Country", profile, EMAIL) == "United States"

    def test_country_of_residence(self, profile):
        assert answer_for("Country of Residence", profile, EMAIL) == "United States"

    def test_country_where_job_is_located_not_matched(self, profile):
        # This should NOT return "United States" — it's asking about job location, not residence
        result = answer_for("Country where the job is located*", profile, EMAIL)
        assert result != "United States"


class TestEducationFields:
    def test_school(self, profile):
        assert answer_for("School", profile, EMAIL) == "University of North Carolina - Charlotte"

    def test_university(self, profile):
        assert answer_for("University", profile, EMAIL) == "University of North Carolina - Charlotte"

    def test_degree(self, profile):
        assert answer_for("Degree", profile, EMAIL) == "Master's Degree"

    def test_degree_type(self, profile):
        assert answer_for("Degree Type", profile, EMAIL) == "Master's Degree"

    def test_graduation_year(self, profile):
        assert answer_for("Graduation Year", profile, EMAIL) == "2024"


class TestExperienceFields:
    def test_years_experience(self, profile):
        assert answer_for("Years of experience", profile, EMAIL) == "5"

    def test_how_many_years(self, profile):
        assert answer_for("How many years of experience do you have?", profile, EMAIL) == "5"

    def test_current_title(self, profile):
        assert answer_for("Current Job Title", profile, EMAIL) == "Gen AI Engineer"

    def test_current_company(self, profile):
        assert answer_for("Current Company", profile, EMAIL) == "JP Morgan"

    def test_current_employer(self, profile):
        assert answer_for("Current or Previous Employer", profile, EMAIL) == "JP Morgan"


class TestHardcodedFields:
    def test_disability_returns_decline(self, profile):
        ans = answer_for("Disability Status", profile, EMAIL)
        assert "wish" in ans.lower() or "don't" in ans.lower()

    def test_veteran_returns_not_protected(self, profile):
        ans = answer_for("Veteran Status", profile, EMAIL)
        assert "not a protected veteran" in ans.lower()

    def test_salary(self, profile):
        assert answer_for("Expected Salary", profile, EMAIL).lower() != ""

    def test_cover_letter_returns_empty(self, profile):
        assert answer_for("Cover Letter", profile, EMAIL) == ""

    def test_unknown_field_returns_empty(self, profile):
        assert answer_for("Favorite Color", profile, EMAIL) == ""
