"""
Tests for _infer_industry and _infer_exp in dashboard.py.
"""
import pytest
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import dashboard
_infer_industry = dashboard._infer_industry
_infer_exp      = dashboard._infer_exp


class TestInferIndustry:
    # Engineering
    def test_software_engineer(self):
        assert _infer_industry("Software Engineer") == "engineering"

    def test_backend_developer(self):
        assert _infer_industry("Backend Developer") == "engineering"

    def test_frontend_developer(self):
        assert _infer_industry("Frontend Developer") == "engineering"

    def test_devops_engineer(self):
        assert _infer_industry("DevOps Engineer") == "engineering"

    def test_fullstack_developer(self):
        assert _infer_industry("Full Stack Developer") == "engineering"

    def test_platform_engineer(self):
        assert _infer_industry("Platform Engineer") == "engineering"

    def test_sre(self):
        assert _infer_industry("Site Reliability Engineer") == "engineering"

    # IT — must take priority over Engineering for IT-specific titles
    def test_it_manager(self):
        assert _infer_industry("IT Manager") == "it"

    def test_it_support(self):
        assert _infer_industry("IT Support Specialist") == "it"

    def test_sysadmin(self):
        assert _infer_industry("Sysadmin") == "it"

    def test_systems_administrator(self):
        assert _infer_industry("Systems Administrator") == "it"

    def test_network_engineer(self):
        # "Network Engineer" has "engineer" (Engineering) but also matches network.engineer (IT)
        # IT rule must win because it runs first
        assert _infer_industry("Network Engineer") == "it"

    def test_help_desk(self):
        assert _infer_industry("Help Desk Technician") == "it"

    def test_desktop_support(self):
        assert _infer_industry("Desktop Support Analyst") == "it"

    def test_it_analyst(self):
        assert _infer_industry("IT Analyst") == "it"

    # Management — BUG: "VP of Engineering" contains "engineer" which fires Engineering rule first
    def test_vp_engineering_is_management(self):
        # EXPECTED: "management" — currently returns "engineering" (BUG)
        assert _infer_industry("VP of Engineering") == "management"

    def test_engineering_manager_is_management(self):
        # EXPECTED: "management" — currently returns "engineering" (BUG)
        assert _infer_industry("Engineering Manager") == "management"

    def test_director_of_product(self):
        assert _infer_industry("Director of Product") == "management"

    def test_cto(self):
        assert _infer_industry("CTO") == "management"

    def test_head_of_engineering(self):
        assert _infer_industry("Head of Engineering") == "management"

    # Data / Analytics
    def test_data_scientist(self):
        assert _infer_industry("Data Scientist") == "data"

    def test_data_analyst(self):
        assert _infer_industry("Data Analyst") == "data"

    def test_ml_researcher(self):
        assert _infer_industry("ML Researcher") == "data"

    # Sales
    def test_account_executive(self):
        assert _infer_industry("Account Executive") == "sales"

    def test_bdr(self):
        assert _infer_industry("Business Development Representative") == "sales"

    # Marketing
    def test_content_marketer(self):
        assert _infer_industry("Content Marketing Manager") == "marketing"

    # Design
    def test_ux_designer(self):
        assert _infer_industry("UX Designer") == "design"

    def test_ui_designer(self):
        assert _infer_industry("UI Designer") == "design"

    # Operations
    def test_program_manager(self):
        assert _infer_industry("Program Manager") == "operations"

    def test_project_manager(self):
        assert _infer_industry("Project Manager") == "operations"

    # Finance
    def test_accountant(self):
        assert _infer_industry("Senior Accountant") == "finance"

    # Support
    def test_customer_success(self):
        assert _infer_industry("Customer Success Manager") == "support"

    # Legal / HR
    def test_recruiter(self):
        assert _infer_industry("Technical Recruiter") == "legal"

    def test_hr(self):
        assert _infer_industry("HR Business Partner") == "legal"

    # Other
    def test_unknown_falls_to_other(self):
        assert _infer_industry("Barista") == "other"

    def test_empty_title(self):
        assert _infer_industry("") == "other"


class TestInferExp:
    def test_senior(self):
        assert _infer_exp("Senior Software Engineer") == "Senior"

    def test_sr_abbreviation(self):
        assert _infer_exp("Sr. Developer") == "Senior"

    def test_junior(self):
        assert _infer_exp("Junior Developer") == "Junior"

    def test_jr_abbreviation(self):
        assert _infer_exp("Jr. Engineer") == "Junior"

    def test_staff(self):
        assert _infer_exp("Staff Engineer") == "Staff"

    def test_principal(self):
        assert _infer_exp("Principal Architect") == "Principal"

    def test_lead(self):
        assert _infer_exp("Lead Developer") == "Lead"

    def test_intern(self):
        assert _infer_exp("Software Engineering Intern") == "Intern"

    def test_internship(self):
        assert _infer_exp("Summer Internship - Data Science") == "Intern"

    def test_entry_level(self):
        assert _infer_exp("Entry Level Software Engineer") == "Junior"

    def test_associate(self):
        assert _infer_exp("Associate Software Engineer") == "Junior"

    def test_mid_default(self):
        # No level keyword → defaults to "Mid"
        assert _infer_exp("Software Engineer") == "Mid"

    def test_mid_explicit(self):
        assert _infer_exp("Mid-Level Engineer") == "Mid"

    def test_intermediate(self):
        assert _infer_exp("Intermediate Developer") == "Mid"

    def test_distinguished(self):
        assert _infer_exp("Distinguished Engineer") == "Principal"

    def test_roman_iv(self):
        assert _infer_exp("Software Engineer IV") == "Senior"

    def test_roman_ii(self):
        assert _infer_exp("Software Engineer II") == "Mid"
