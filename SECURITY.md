# Security Policy for ZeroBot Z1

ZeroBot is a live trading system. Security is paramount.

---

## 🚨 Reporting Security Vulnerabilities

**DO NOT** file public GitHub issues for security problems.

### Report privately to:
- **Email**: security@zerobot.dev (if this were a real project)
- **GPG Key**: [Public key here if applicable]
- **Response Time**: 24 hours

Include:
- Vulnerability description
- Steps to reproduce
- Potential impact
- Suggested fix (optional)

We will:
1. Acknowledge receipt within 24 hours
2. Provide status updates every 48 hours
3. Deploy fix within 7 days (critical) or 30 days (standard)
4. Credit you in security advisory (unless you prefer anonymity)

---

## ✅ Credential Security

### DO:
- ✅ Use `config/.env.example` as template
- ✅ Keep `config/.env` private and git-ignored
- ✅ Rotate all credentials quarterly
- ✅ Use environment variables (`os.getenv()`)
- ✅ Enable 2FA on all broker/service accounts
- ✅ Use strong unique passwords (20+ chars)
- ✅ Run pre-commit hooks before committing

### DON'T:
- ❌ Commit `config/.env` or any .env files
- ❌ Hardcode API keys in source code
- ❌ Share credentials in issues, PRs, or Slack
- ❌ Store credentials in version control
- ❌ Use same credentials across environments (dev/staging/prod)
- ❌ Share credentials via email or Slack
- ❌ Store credentials in browser bookmarks/history

### If Credentials Are Exposed:

1. **IMMEDIATELY** rotate at source (Groq, Telegram, Shoonya, Angel One, etc.)
2. Remove from git history: `git filter-repo --invert-paths --path config/.env`
3. Force-push: `git push origin --force-with-lease`
4. Run security scan: `./security-validate.sh`
5. Notify all contributors
6. Review incident report

See [SECURITY_INCIDENT_RESPONSE.md](SECURITY_INCIDENT_RESPONSE.md) for detailed remediation steps.

---

## 🔒 Security Tools

### Pre-Commit Hooks
- Scans commits for hardcoded secrets
- Blocks commits containing API keys, tokens, passwords
- Install: `cp .git-pre-commit-hook.py .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit`
- Test: `python .git/hooks/pre-commit` (should pass with clean repo)

### Centralized Config Module
- `core/secure_config.py` — All credential access via environment variables
- Never stores plain credentials in memory longer than needed
- Use example: `from core.secure_config import secure_config; key = secure_config.groq_api_key`

### Git Safety Measures
- `.gitignore` excludes all credential files:
  - `config/.env`
  - `*.key`, `*.secret`, `*.pem`
  - `logs/`, `data/`, `cache/`
- History is regularly scanned for secrets

---

## 📋 Supported Platforms & Vulnerabilities

### Python Version
- Minimum: 3.9
- Recommended: 3.11+
- Known Issues: None current

### Broker Integrations
- **Shoonya (Finvasia)**: Fully secured with credential rotation
- **Angel One**: Fully secured with credential rotation
- **Paper Broker**: No credentials needed (safe for testing)

### External Dependencies
- All dependencies in `requirements.txt` are regularly scanned
- `security-check.sh` validates vulnerable packages quarterly
- Report any vulnerable dependencies immediately

---

## 🛡️ Best Practices

### Local Development
```bash
# 1. Copy environment template
cp config/.env.example config/.env

# 2. Add YOUR credentials (never commit!)
nano config/.env

# 3. Verify credentials are loaded
python -c "from core.secure_config import secure_config; print('✅ Config loaded')"

# 4. Never modify .gitignore to allow .env
# (if you do, CI/CD will catch and reject)

# 5. After pulling code, always update credentials
git pull
cp config/.env.example config/.env  # Review what changed
# ... update .env with your credentials ...
```

### CI/CD Pipeline
- Pre-commit hooks run on all commits
- GitHub Actions scans for secrets before merge
- All PRs require clean security scan
- No secrets can be in environment variables passed to CI

### Deployment
- Credentials are managed by your deployment tool (Docker secrets, K8s secrets, etc.)
- Never pass credentials as command-line arguments
- Use secure vaults for production (HashiCorp Vault, AWS Secrets Manager, etc.)
- Rotate credentials when:
  - Employee leaves
  - Developer laptop compromised
  - Credential was accidentally exposed
  - Quarterly rotation schedule (best practice)

---

## 📚 Security Resources

- **OWASP Top 10**: https://owasp.org/www-project-top-ten/
- **12-Factor App Config**: https://12factor.net/config
- **git filter-repo**: https://github.com/newren/git-filter-repo
- **Detecting Secrets**: https://github.com/Yelp/detect-secrets

---

## 🔐 Known Issues & Mitigations

| Issue | Severity | Mitigation | Status |
|-------|----------|-----------|--------|
| Exposed credentials in historic commit | CRITICAL | git filter-repo cleanup, credential rotation | RESOLVED |
| No secret detection on-commit | HIGH | Pre-commit hook installed | RESOLVED |
| .env in .gitignore but file cached | MEDIUM | git rm --cached config/.env | RESOLVED |

---

## 📞 Security Contacts

| Role | Email |
|------|-------|
| Security Team Lead | security@zerobot.dev (if applicable) |
| Incident Response | devops@zerobot.dev (if applicable) |

---

## Changelog

### 2026-03-23
- **Incident**: Exposed credentials in git history
- **Fix**: Removed config/.env from all commits using git filter-repo
- **Prevention**: Installed pre-commit hook for secret detection
- **Status**: ✅ RESOLVED

---

**Last Updated**: 2026-03-23  
**Next Review**: 2026-06-23 (quarterly)
