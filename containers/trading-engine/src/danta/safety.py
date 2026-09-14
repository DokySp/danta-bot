"""Reject recognizable credentials before model input, reports, sharing or build.

This detects explicit credential fields and common literal forms. It cannot
identify every arbitrary secret or encoded value; allowlists remain separate.
"""

import ast
import html
import json
import re
from pathlib import Path

FORBIDDEN_KEYS = frozenset({"appkey", "appsecret", "apikey", "token", "authorization", "accountnumber", "cano", "acntprdtcd", "authjson", "secret",
    "accesstoken", "refreshtoken", "idtoken", "dartapikey", "kisappsecret", "kisappkey", "kisaccountref", "telegrambottoken", "openaiapikey", "credentials", "crtfckey", "password", "privatekey"})
CREDENTIAL_TEXT = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b|"
    r"\bBearer\s+[A-Za-z0-9_.~-]{12,}|"
    r"\b\d{7,12}:[A-Za-z0-9_-]{25,}\b|"
    r"\b(?:(?:openai|kis|dart|telegram)[_-]?)?(?:api[_-]?key|app[_-]?(?:key|secret)|access[_-]?token|crtfc[_-]?key|password)[\"']?[ \t]*[=:]+[ \t]*[\"']?[A-Za-z0-9_.~+-]{8,}|"
    r"https?://[^\s/@:]+:[^\s/@]+@", re.IGNORECASE)


class CredentialError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def reject_credentials(value):
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
            if normalized in FORBIDDEN_KEYS and item not in (None, ""):
                raise CredentialError("SENSITIVE_FIELD")
            reject_credentials(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            reject_credentials(item)
    elif isinstance(value, bytes):
        reject_credentials(value.decode("utf-8", errors="replace"))
    elif isinstance(value, str):
        if CREDENTIAL_TEXT.search(value):
            raise CredentialError("SENSITIVE_TEXT")
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except ValueError:
                pass
            else:
                reject_credentials(parsed)
        if "<" in value and ">" in value:
            # Covers escaped report table fields without executing HTML.
            visible = re.sub(r"<[^>]*>", ":", html.unescape(value))
            if CREDENTIAL_TEXT.search(visible):
                raise CredentialError("SENSITIVE_TEXT")


def scan_file(path):
    """Source uses literal AST values, so variable names are not credential values."""
    path = Path(path)
    try:
        if path.is_symlink():
            raise CredentialError("SYMLINK_NOT_ALLOWED")
        content = path.read_text(encoding="utf-8")
        if path.suffix == ".py":
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
                    reject_credentials(node.value)
                elif isinstance(node, ast.Dict):
                    reject_credentials({key.value: value.value for key, value in zip(node.keys, node.values)
                        if isinstance(key, ast.Constant) and isinstance(value, ast.Constant)})
                elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                    reject_credentials({target.id if isinstance(target, ast.Name) else target.attr: node.value.value
                        for target in node.targets if isinstance(target, (ast.Name, ast.Attribute))})
                elif isinstance(node, ast.AnnAssign) and isinstance(node.value, ast.Constant) and isinstance(node.target, (ast.Name, ast.Attribute)):
                    reject_credentials({node.target.id if isinstance(node.target, ast.Name) else node.target.attr: node.value.value})
                elif isinstance(node, ast.keyword) and isinstance(node.value, ast.Constant):
                    reject_credentials({node.arg: node.value.value})
        else:
            reject_credentials(content)
    except (OSError, UnicodeError, SyntaxError):
        raise CredentialError("UNSCANNABLE_FILE") from None


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Scan only explicitly supplied build/share paths; never prints credential values.")
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()
    failures, scanned = [], 0
    for root in args.paths:
        paths = sorted(root.rglob("*")) if root.is_dir() else [root]
        for path in paths:
            if path.is_dir() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            try:
                scan_file(path)
                scanned += 1
            except CredentialError as error:
                failures.append({"path": str(path), "category": error.code})
    print(json.dumps({"scanned_files": scanned, "failures": failures}))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
