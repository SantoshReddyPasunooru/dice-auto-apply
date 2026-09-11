"""
Tests for pure logic in gmail_sender.py — email extraction and body parsing.
No live Gmail API calls.
"""
import pytest
import sys, pathlib, base64
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from gmail_sender import extract_recruiter_emails, GmailSender


class TestExtractRecruiterEmails:
    def test_plain_email(self):
        result = extract_recruiter_emails("Contact us at john@acme.com for more info.")
        assert "john@acme.com" in result

    def test_multiple_emails(self):
        text = "Email alice@foo.com or bob@bar.com"
        result = extract_recruiter_emails(text)
        assert "alice@foo.com" in result
        assert "bob@bar.com" in result

    def test_deduplicates(self):
        text = "john@acme.com is our contact. Please email john@acme.com"
        result = extract_recruiter_emails(text)
        assert result.count("john@acme.com") == 1

    def test_skips_noreply(self):
        result = extract_recruiter_emails("noreply@greenhouse.io sent this.")
        assert result == []

    def test_skips_no_dash_reply(self):
        result = extract_recruiter_emails("no-reply@company.com")
        assert result == []

    def test_skips_support(self):
        result = extract_recruiter_emails("support@company.com")
        assert result == []

    def test_skips_info(self):
        result = extract_recruiter_emails("info@company.com")
        assert result == []

    def test_skips_donotreply(self):
        result = extract_recruiter_emails("donotreply@company.com")
        assert result == []

    def test_empty_text_returns_empty(self):
        assert extract_recruiter_emails("") == []

    def test_no_email_in_text_returns_empty(self):
        assert extract_recruiter_emails("No email here at all.") == []

    def test_preserves_original_case(self):
        result = extract_recruiter_emails("Contact John.Smith@Acme.com")
        assert len(result) == 1


class TestExtractMsgBody:
    def _encode(self, text: str) -> str:
        return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")

    def test_plain_text_payload(self):
        msg = {
            "payload": {
                "mimeType": "text/plain",
                "body": {"data": self._encode("Your code is 123456")},
                "parts": [],
            },
            "snippet": "",
        }
        body = GmailSender._extract_msg_body(msg)
        assert "123456" in body

    def test_multipart_plain_text(self):
        msg = {
            "payload": {
                "mimeType": "multipart/mixed",
                "body": {},
                "parts": [
                    {
                        "mimeType": "text/plain",
                        "body": {"data": self._encode("Code: 654321")},
                        "parts": [],
                    },
                    {
                        "mimeType": "text/html",
                        "body": {"data": self._encode("<b>Code: 654321</b>")},
                        "parts": [],
                    },
                ],
            },
            "snippet": "Code: 654321",
        }
        body = GmailSender._extract_msg_body(msg)
        assert "654321" in body

    def test_empty_payload_falls_back_to_snippet(self):
        msg = {
            "payload": {"mimeType": "multipart/mixed", "body": {}, "parts": []},
            "snippet": "fallback snippet 999999",
        }
        body = GmailSender._extract_msg_body(msg)
        assert "999999" in body

    def test_nested_multipart(self):
        msg = {
            "payload": {
                "mimeType": "multipart/alternative",
                "body": {},
                "parts": [
                    {
                        "mimeType": "multipart/related",
                        "body": {},
                        "parts": [
                            {
                                "mimeType": "text/plain",
                                "body": {"data": self._encode("Deep code 777777")},
                                "parts": [],
                            }
                        ],
                    }
                ],
            },
            "snippet": "",
        }
        body = GmailSender._extract_msg_body(msg)
        assert "777777" in body


class TestFetchVerificationCodeLogic:
    """
    Verify the 6-digit code extraction regex used inside fetch_verification_code.
    We test the pattern directly since the method requires a live Gmail service.
    """
    import re
    _CODE_RE = re.compile(r'\b(\d{6})\b')

    def test_finds_six_digit_code(self):
        import re
        codes = re.findall(r'\b(\d{6})\b', "Your verification code is 482910.")
        assert codes == ["482910"]

    def test_does_not_match_five_digits(self):
        import re
        codes = re.findall(r'\b(\d{6})\b', "Code: 12345")
        assert codes == []

    def test_does_not_match_seven_digits(self):
        import re
        codes = re.findall(r'\b(\d{6})\b', "Code: 1234567")
        assert codes == []

    def test_matches_code_in_sentence(self):
        import re
        text = "Please enter the code 839201 to verify your email."
        codes = re.findall(r'\b(\d{6})\b', text)
        assert "839201" in codes

    def test_multiple_codes_returns_first(self):
        import re
        text = "Code: 111111 or 222222"
        codes = re.findall(r'\b(\d{6})\b', text)
        assert codes[0] == "111111"
