#!/usr/bin/env python3
"""
Pre-commit hook to detect secrets before they're committed.

Install: cp .git-pre-commit-hook.py .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit

Detects:
- API keys (Groq, OpenRouter, Angel One, Shoonya)
- Passwords and tokens
- Private keys
- Database credentials
- AWS/GCP/Azure keys
- JWT tokens
- TOTP secrets
"""

import re
import sys
from pathlib import Path

# List of file patterns to check
PATTERNS_TO_CHECK = [
    (r'api[_-]?key\s*[:=]\s*[\'"]([a-zA-Z0-9\-_.]{20,})[\'"]', 'API Key'),
    (r'password\s*[:=]\s*[\'"]([^\'\"]{8,})[\'"]', 'Password'),
    (r'token\s*[:=]\s*[\'"]([a-zA-Z0-9\-_.]{20,})[\'"]', 'Token'),
    (r'secret\s*[:=]\s*[\'"]([a-zA-Z0-9\-_.]{20,})[\'"]', 'Secret'),
    (r'sk[_-]?(or|live|test)[_-]?[a-zA-Z0-9]{20,}', 'OpenRouter API Key'),
    (r'sk[_-]?(live|test)[_-]?[a-zA-Z0-9]{20,}', 'Stripe API Key'),
    (r'gsk[_-][a-zA-Z0-9]{20,}', 'Groq API Key'),
    (r'(AKIA|ASIA)[0-9A-Z]{16}', 'AWS Access Key'),
    (r'-----BEGIN (RSA|DSA|EC|PGP) PRIVATE KEY', 'Private Key'),
    (r'ghp_[a-zA-Z0-9]{36}', 'GitHub Personal Access Token'),
    (r'totp[_-]?secret\s*[:=]\s*[\'"]([A-Z2-7=]{16,})[\'"]', 'TOTP Secret'),
]

# Files to never check (binary, generated, etc)
EXCLUDED_FILES = {
    '.git', '.gitignore', '__pycache__', '*.pyc', '*.pyo',
    '.pytest_cache', '.tox', 'venv', '.venv', 'node_modules',
    '.env.example', '.env.template', 'LICENSE', '*.md',
    'CHANGELOG', 'QUICKSTART', 'CONTRIBUTING', 'README',
}

# Paths to always check (even if not modified)
CRITICAL_FILES = {
    'config/settings.yaml',
    'core/config.py',
    'core/engine.py',
    'core/state_manager.py',
}

def is_excluded(file_path: str) -> bool:
    """Check if file is excluded from secret scanning."""
    for excluded in EXCLUDED_FILES:
        if excluded in file_path or file_path.endswith(excluded):
            return True
    return False

def scan_file(file_path: str) -> list:
    """Scan file for potential secrets."""
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
    except Exception:
        return []
    
    findings = []
    for pattern, secret_type in PATTERNS_TO_CHECK:
        matches = re.finditer(pattern, content, re.IGNORECASE)
        for match in matches:
            line_num = content[:match.start()].count('\n') + 1
            findings.append((secret_type, line_num, file_path))
    
    return findings

def main():
    """Run pre-commit secret detection."""
    # Get staged files
    import subprocess
    result = subprocess.run(
        ['git', 'diff', '--cached', '--name-only'],
        capture_output=True, text=True
    )
    staged_files = result.stdout.strip().split('\n')
    
    # Add critical files even if not staged
    staged_files.extend(CRITICAL_FILES)
    staged_files = [f for f in staged_files if f.strip()]
    
    all_findings = []
    for file_path in staged_files:
        if not is_excluded(file_path) and Path(file_path).exists():
            findings = scan_file(file_path)
            all_findings.extend(findings)
    
    if all_findings:
        print("\n" + "="*70)
        print("🚨 SECURITY ALERT: Potential secrets detected in staged files!")
        print("="*70 + "\n")
        
        for secret_type, line_num, file_path in all_findings:
            print(f"  [{secret_type}] {file_path}:{line_num}")
        
        print("\n" + "="*70)
        print("⚠️  PREVENTION STEPS:")
        print("="*70)
        print("  1. Remove the secret from the file")
        print("  2. Use environment variables instead: os.getenv('SECRET_NAME')")
        print("  3. Add to config/.env.example (WITHOUT the actual value)")
        print("  4. Run: git add <fixed_files>")
        print("  5. Run: git commit again")
        print("\nExample:")
        print("  ❌ BAD:  api_key = 'sk-1234567890abcdef'")
        print("  ✅ GOOD: api_key = os.getenv('GROQ_API_KEY')")
        print("="*70 + "\n")
        
        sys.exit(1)
    
    sys.exit(0)

if __name__ == '__main__':
    main()
