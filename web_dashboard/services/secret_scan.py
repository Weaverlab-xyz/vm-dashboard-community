"""Artefact secret-scanning (community backlog #4, phase 2).

Advisory, read-only scan of the artefacts the platform *executes* — Ansible
playbooks / shell / PowerShell scripts — for hard-coded secrets, so an operator
gets warned before a credential is stored in the asset backend or shipped to a
target. It never blocks: the upload/run proceeds; the finding is a heads-up.

Pure functions (no I/O) so it unit-tests trivially. High-precision regex ruleset
plus placeholder/vault filtering — an advisory that cries wolf gets ignored, so we
skip templated values (``{{ … }}``), env refs (``$VAR`` / ``${VAR}``), obvious
placeholders, and Ansible-Vault-encrypted files rather than flag them.
"""
import re

# (rule id, compiled pattern). When a pattern has a capture group, that group is
# the secret value (used for redaction); otherwise the whole match is.
_RULES = [
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("aws_secret_access_key",
     re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*['\"]?([A-Za-z0-9/+]{40})\b")),
    ("private_key",
     re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    ("github_token", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("generic_secret_assignment",
     re.compile(r"(?i)\b(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)\b"
                r"\s*[:=]\s*['\"]?([^\s'\"#{}$<>]{8,})")),
]

# Values that look like a secret assignment but aren't a hard-coded credential.
_PLACEHOLDER = re.compile(
    r"^(?:changeme|change_me|example|placeholder|redacted|secret|password|"
    r"x{3,}|\*{3,}|\.{3,}|null|none|true|false|yes|no|\d+)$", re.I)


def _redact(value: str) -> str:
    v = value.strip().strip("'\"")
    if len(v) <= 8:
        return "****"
    return f"{v[:3]}…{v[-2:]}"


def _looks_templated(value: str) -> bool:
    v = value.strip().strip("'\"")
    return (not v
            or v.startswith(("{{", "$", "<", "vault_"))
            or v.endswith("}}")
            or bool(_PLACEHOLDER.match(v)))


def scan_text(text: str, filename: str | None = None) -> list[dict]:
    """Scan text for hard-coded secrets. Returns ``[{rule, line, match}]`` with
    the value redacted. Ansible-Vault-encrypted content is skipped wholesale."""
    if not text or "$ANSIBLE_VAULT" in text[:200]:
        return []

    findings: list[dict] = []
    seen: set = set()
    for lineno, line in enumerate(text.splitlines(), start=1):
        for rule, pat in _RULES:
            m = pat.search(line)
            if not m:
                continue
            value = m.group(1) if m.lastindex else m.group(0)
            if rule == "generic_secret_assignment" and _looks_templated(value):
                continue
            dedupe = (rule, lineno)
            if dedupe in seen:
                continue
            seen.add(dedupe)
            findings.append({"rule": rule, "line": lineno, "match": _redact(value)})
    return findings


# Extensions we scan as text; everything else (.rpm/.deb and unknown) is skipped.
_TEXT_EXTS = (".yml", ".yaml", ".sh", ".ps1", ".tf", ".tfvars", ".json", ".env", ".cfg", ".conf", ".ini")


def scan_bytes(data: bytes, filename: str | None = None) -> list[dict]:
    """Decode text-like asset bytes and scan. Binary assets (or undecodable
    content) return no findings — we don't scan .rpm/.deb packages."""
    if not data:
        return []
    if filename and not filename.lower().endswith(_TEXT_EXTS):
        return []
    if b"\x00" in data[:8192]:  # NUL byte → treat as binary
        return []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return []
    return scan_text(text, filename=filename)
