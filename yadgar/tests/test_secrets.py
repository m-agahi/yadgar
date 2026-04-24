"""Tests for built-in secret detection (yadgar/secrets.py)."""

from yadgar.secrets import check_secrets


class TestCheckSecretsBlocked:
    def test_aws_access_key(self):
        blocked, reason, pattern = check_secrets("My key is AKIAIOSFODNN7EXAMPLE here")
        assert blocked is True
        assert "AWS access key" in reason
        assert "AKIA" in pattern

    def test_rsa_private_key(self):
        # Split across variable to avoid false-positive secret scanners.
        header = "-----BEGIN RSA " + "PRIVATE KEY-----"  # gitleaks:allow
        content = f"key:\n{header}\nABC123\n-----END RSA PRIVATE KEY-----"
        blocked, reason, _ = check_secrets(content)
        assert blocked is True
        assert "Private key" in reason

    def test_ec_private_key(self):
        header = "-----BEGIN EC " + "PRIVATE KEY-----"  # gitleaks:allow
        blocked, reason, _ = check_secrets(header)
        assert blocked is True
        assert "Private key" in reason

    def test_openssh_private_key(self):
        header = "-----BEGIN OPENSSH " + "PRIVATE KEY-----"  # gitleaks:allow
        blocked, reason, _ = check_secrets(header)
        assert blocked is True
        assert "Private key" in reason

    def test_jwt_token(self):
        token = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
            ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
            ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        )
        blocked, reason, _ = check_secrets(f"Authorization: Bearer {token}")
        assert blocked is True
        assert "JWT token" in reason

    def test_github_token(self):
        content = f"TOKEN=ghp_{'A' * 36}"
        blocked, reason, _ = check_secrets(content)
        assert blocked is True
        assert "GitHub token" in reason

    def test_gitlab_token(self):
        content = f"CI_TOKEN=glpat-{'a' * 20}"
        blocked, reason, _ = check_secrets(content)
        assert blocked is True
        assert "GitLab token" in reason

    def test_postgres_connection_string(self):
        content = "DB_URL=postgres://user:supersecretpassword@db.example.com/mydb"
        blocked, reason, _ = check_secrets(content)
        assert blocked is True
        assert "Database connection string" in reason

    def test_mysql_connection_string(self):
        content = "mysql://root:hunter2@localhost/prod"
        blocked, reason, _ = check_secrets(content)
        assert blocked is True
        assert "Database connection string" in reason

    def test_credential_pattern_api_key(self):
        content = "api_key=" + "AbCdEfGhIjKlMnOpQrSt123456"  # gitleaks:allow
        blocked, reason, _ = check_secrets(content)
        assert blocked is True
        assert "Credential" in reason

    def test_credential_pattern_secret_equals(self):
        content = "SECRET=verylongpasswordthatexceedstwentycharacters"
        blocked, reason, _ = check_secrets(content)
        assert blocked is True
        assert "Credential" in reason


class TestCheckSecretsClean:
    def test_normal_memory_not_blocked(self):
        blocked, _, _ = check_secrets(
            "Implemented the feature using async/await pattern in server.py"
        )
        assert blocked is False

    def test_empty_content_not_blocked(self):
        blocked, _, _ = check_secrets("")
        assert blocked is False

    def test_short_password_word_not_blocked(self):
        # "password" without a long value following it shouldn't trigger
        blocked, _, _ = check_secrets("Changed the password field label in the UI")
        assert blocked is False

    def test_aws_prefix_too_short_not_blocked(self):
        # "AKIA" needs exactly 16 uppercase alphanumeric chars after it
        blocked, _, _ = check_secrets("AKIA is a prefix")
        assert blocked is False


class TestCheckSecretsReturnValues:
    def test_reason_format(self):
        blocked, reason, _ = check_secrets("AKIAIOSFODNN7EXAMPLE")
        assert blocked is True
        assert reason.startswith("secret_detected:")

    def test_pattern_matched_truncated(self):
        # matched text longer than 20 chars should get "..."
        blocked, _, pattern = check_secrets("AKIAIOSFODNN7EXAMPLE trailing text here")
        assert blocked is True
        assert len(pattern) <= 23  # 20 chars + "..."

    def test_clean_content_returns_empty_strings(self):
        blocked, reason, pattern = check_secrets("no secrets here")
        assert blocked is False
        assert reason == ""
        assert pattern == ""
