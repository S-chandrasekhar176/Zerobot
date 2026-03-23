#!/usr/bin/env python3
"""
security-validate.py — ZeroBot Security Validation Suite
Verifies that:
1. No secrets are hardcoded in source
2. Pre-commit hook is installed
3. config/.env is git-ignored
4. Credentials load only from environment
5. No sensitive files in git history
"""

import os
import re
import sys
import subprocess
from pathlib import Path
from typing import List, Tuple

# Color codes
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
RESET = "\033[0m"

# Secret patterns (same as pre-commit hook)
SECRET_PATTERNS = {
    "Groq API": r"gsk_[a-zA-Z0-9]{32,}",
    "AWS Access Key": r"AKIA[0-9A-Z]{16}",
    "AWS Secret": r"aws_secret_access_key\s*=\s*['\"]?[A-Za-z0-9/+=]{40}['\"]?",
    "GitHub Token": r"ghp_[a-zA-Z0-9_]{36,255}",
    "Telegram Bot": r"\d{9,10}:[A-Za-z0-9_-]{35,}",
    "Private Key": r"-----BEGIN [A-Z]+ PRIVATE KEY-----",
    "TOTP Secret": r"[A-Z2-7]{16,}",  # base32 encoded
    "Password": r"password\s*=\s*['\"]([^'\"]+)['\"]",
    "API Key Generic": r"api[_-]?key\s*[:=]\s*['\"]?([a-zA-Z0-9_-]{20,})",
    "Bearer Token": r"Bearer\s+[a-zA-Z0-9_-]{20,}",
}

class SecurityValidator:
    """Comprehensive security validation suite."""
    
    def __init__(self, repo_path: str = "."):
        self.repo_path = Path(repo_path).resolve()
        self.issues: List[Tuple[str, str]] = []  # (issue_type, message)
        self.warnings: List[Tuple[str, str]] = []
        self.passes: List[str] = []
        
    def run_all_checks(self) -> bool:
        """Run all security checks. Returns True if all pass."""
        print(f"\n{BLUE}{'='*70}")
        print("🔐 ZeroBot Security Validation Suite")
        print(f"{'='*70}{RESET}\n")
        
        checks = [
            ("Pre-Commit Hook", self.check_precommit_hook),
            ("Git Ignore", self.check_gitignore),
            ("Hardcoded Secrets (Current)", self.check_hardcoded_secrets_current),
            ("Git History", self.check_git_history),
            ("Config Loading", self.check_config_loading),
            ("Environment Setup", self.check_environment_setup),
            ("File Permissions", self.check_file_permissions),
        ]
        
        for check_name, check_func in checks:
            try:
                print(f"{BLUE}[{check_name}]{RESET}", end=" ")
                check_func()
            except Exception as e:
                self.issues.append((check_name, f"Exception: {str(e)}"))
                print(f"{RED}FAIL{RESET}")
        
        self.print_summary()
        return len(self.issues) == 0
    
    def check_precommit_hook(self):
        """Verify pre-commit hook is installed."""
        hook_path = self.repo_path / ".git" / "hooks" / "pre-commit"
        
        if not hook_path.exists():
            self.warnings.append(("Pre-Commit Hook", "Hook not installed. Run: cp .git-pre-commit-hook.py .git/hooks/pre-commit"))
            print(f"{YELLOW}WARN{RESET}")
            return
        
        if not os.access(hook_path, os.X_OK):
            self.warnings.append(("Pre-Commit Hook", f"{hook_path} exists but not executable. Run: chmod +x {hook_path}"))
            print(f"{YELLOW}WARN{RESET}")
            return
        
        self.passes.append("Pre-commit hook installed and executable")
        print(f"{GREEN}PASS{RESET}")
    
    def check_gitignore(self):
        """Verify config/.env is in .gitignore."""
        gitignore_path = self.repo_path / ".gitignore"
        
        if not gitignore_path.exists():
            self.issues.append(("Git Ignore", ".gitignore not found"))
            print(f"{RED}FAIL{RESET}")
            return
        
        content = gitignore_path.read_text()
        
        required_entries = ["config/.env", "*.key", "*.secret", ".env"]
        missing = [e for e in required_entries if e not in content]
        
        if missing:
            self.warnings.append(("Git Ignore", f"Missing entries: {', '.join(missing)}"))
            print(f"{YELLOW}WARN{RESET}")
            return
        
        self.passes.append(".gitignore properly excludes secrets")
        print(f"{GREEN}PASS{RESET}")
    
    def check_hardcoded_secrets_current(self):
        """Scan current working files for hardcoded secrets."""
        issues_found = []
        
        # Skip directories
        skip_dirs = {
            ".git", ".gitignore", "__pycache__", "node_modules", ".pytest_cache",
            ".venv", "venv", "env", "dist", "build", "catboost_info", "logs"
        }
        
        for py_file in self.repo_path.rglob("*.py"):
            # Skip if in skip_dirs
            if any(skip in py_file.parts for skip in skip_dirs):
                continue
            
            try:
                content = py_file.read_text(encoding='utf-8', errors='ignore')
                
                for pattern_name, pattern in SECRET_PATTERNS.items():
                    matches = list(re.finditer(pattern, content, re.IGNORECASE))
                    if matches:
                        line_nums = self._find_line_numbers(content, matches)
                        issues_found.append({
                            "file": str(py_file.relative_to(self.repo_path)),
                            "pattern": pattern_name,
                            "lines": line_nums
                        })
            except Exception as e:
                pass  # Skip binary files
        
        if issues_found:
            for issue in issues_found:
                self.issues.append((
                    "Hardcoded Secrets",
                    f"{issue['file']}: {issue['pattern']} (lines {issue['lines']})"
                ))
            print(f"{RED}FAIL{RESET}")
        else:
            self.passes.append("No hardcoded secrets in current files")
            print(f"{GREEN}PASS{RESET}")
    
    def check_git_history(self):
        """Scan recent git commits for secrets."""
        try:
            # Get last 50 commits
            result = subprocess.run(
                ["git", "log", "--oneline", "-50"],
                cwd=self.repo_path,
                capture_output=True,
                text=True
            )
            
            if result.returncode != 0:
                self.warnings.append(("Git History", "Could not access git history"))
                print(f"{YELLOW}WARN{RESET}")
                return
            
            commits = result.stdout.strip().split('\n') if result.stdout.strip() else []
            
            if not commits:
                self.warnings.append(("Git History", "No commits found"))
                print(f"{YELLOW}WARN{RESET}")
                return
            
            secrets_in_history = []
            for commit in commits[:10]:  # Check most recent 10
                commit_hash = commit.split()[0]
                diff_result = subprocess.run(
                    ["git", "show", commit_hash],
                    cwd=self.repo_path,
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                
                diff_content = diff_result.stdout
                for pattern_name, pattern in SECRET_PATTERNS.items():
                    if re.search(pattern, diff_content, re.IGNORECASE):
                        secrets_in_history.append((commit_hash[:7], pattern_name))
            
            if secrets_in_history:
                for commit_hash, pattern in secrets_in_history:
                    self.issues.append((
                        "Git History",
                        f"Potential {pattern} in commit {commit_hash}"
                    ))
                print(f"{RED}FAIL{RESET}")
            else:
                self.passes.append("No secrets detected in recent git history")
                print(f"{GREEN}PASS{RESET}")
        
        except Exception as e:
            self.warnings.append(("Git History", f"Could not scan git history: {str(e)}"))
            print(f"{YELLOW}WARN{RESET}")
    
    def check_config_loading(self):
        """Verify config loads from environment variables."""
        try:
            # Try to import config
            sys.path.insert(0, str(self.repo_path))
            from core.secure_config import SecureConfig
            
            config = SecureConfig()
            
            # Check that credentials come from environment
            groq_key = os.getenv("GROQ_API_KEY")
            if groq_key:
                self.passes.append("Credentials load from environment variables")
                print(f"{GREEN}PASS{RESET}")
            else:
                self.warnings.append(("Config Loading", "No GROQ_API_KEY in environment (this is OK in non-prod)"))
                print(f"{YELLOW}WARN{RESET}")
        
        except ImportError:
            self.warnings.append(("Config Loading", "SecureConfig not found (may not be installed yet)"))
            print(f"{YELLOW}WARN{RESET}")
        except Exception as e:
            self.warnings.append(("Config Loading", f"Exception: {str(e)}"))
            print(f"{YELLOW}WARN{RESET}")
    
    def check_environment_setup(self):
        """Verify environment is properly configured."""
        # Check for config/.env
        env_file = self.repo_path / "config" / ".env"
        
        if env_file.exists():
            self.warnings.append((
                "Environment Setup",
                f"{env_file} exists locally (OK) but should NOT be committed"
            ))
            print(f"{YELLOW}WARN{RESET}")
        else:
            self.passes.append("config/.env not in working directory (good)")
            print(f"{GREEN}PASS{RESET}")
    
    def check_file_permissions(self):
        """Verify sensitive files have correct permissions."""
        config_dir = self.repo_path / "config"
        
        if not config_dir.exists():
            self.warnings.append(("File Permissions", "config/ directory not found"))
            print(f"{YELLOW}WARN{RESET}")
            return
        
        issues = []
        
        # Check .env.example
        example_file = config_dir / ".env.example"
        if example_file.exists():
            stat = example_file.stat()
            # Should be world-readable (644 or better)
            perms = oct(stat.st_mode)[-3:]
            if perms not in ["644", "444", "755"]:
                issues.append(f".env.example has unsafe permissions: {perms}")
        
        if issues:
            for issue in issues:
                self.warnings.append(("File Permissions", issue))
            print(f"{YELLOW}WARN{RESET}")
        else:
            self.passes.append("Sensitive files have correct permissions")
            print(f"{GREEN}PASS{RESET}")
    
    def _find_line_numbers(self, content: str, matches) -> str:
        """Find line numbers for regex matches."""
        line_nums = []
        for match in matches[:3]:  # Limit to first 3 matches
            line_num = content[:match.start()].count('\n') + 1
            line_nums.append(str(line_num))
        return ", ".join(line_nums)
    
    def print_summary(self):
        """Print validation summary."""
        print(f"\n{BLUE}{'='*70}")
        print("SUMMARY")
        print(f"{'='*70}{RESET}\n")
        
        # Passes
        if self.passes:
            print(f"{GREEN}✅ {len(self.passes)} checks passed:{RESET}")
            for msg in self.passes:
                print(f"   • {msg}")
            print()
        
        # Warnings
        if self.warnings:
            print(f"{YELLOW}⚠️  {len(self.warnings)} warnings:{RESET}")
            for check_type, msg in self.warnings:
                print(f"   • [{check_type}] {msg}")
            print()
        
        # Issues
        if self.issues:
            print(f"{RED}❌ {len(self.issues)} critical issues:{RESET}")
            for check_type, msg in self.issues:
                print(f"   • [{check_type}] {msg}")
            print()
            print(f"{RED}SECURITY VALIDATION FAILED{RESET}")
            print(f"Action: Review issues above and remediate using SECURITY_INCIDENT_RESPONSE.md\n")
            return False
        else:
            print(f"{GREEN}✅ SECURITY VALIDATION PASSED{RESET}\n")
            if self.warnings:
                print(f"{YELLOW}Note: {len(self.warnings)} warnings remain. Review above.{RESET}\n")
            return True

if __name__ == "__main__":
    validator = SecurityValidator()
    success = validator.run_all_checks()
    sys.exit(0 if success else 1)
