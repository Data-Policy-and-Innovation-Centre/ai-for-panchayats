#!/usr/bin/env python3
"""Fail-closed credential scanner for this repository.

Scans a range of commits rather than only the final tree, because a secret that
was added and then deleted in a later commit is still published: it stays in the
history and the value stays live until it is rotated.

    python scripts/scan_secrets.py --range origin/dev..HEAD   # a PR's commits
    python scripts/scan_secrets.py --worktree                 # what is staged now
    python scripts/scan_secrets.py --all-history              # the whole repo

A finding is reported as a fingerprint (path + rule + salted hash of the match),
never as the value itself, so CI logs and review threads never republish it.
Accepted findings go in .secretsallow, one fingerprint per line, and each entry
must be justified in review.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = REPO_ROOT / ".secretsallow"

# Paths whose contents are checksums or vendored metadata, not credentials.
SKIP = re.compile(
    # Lockfiles are inventories of content digests. Every entry is a 64-hex
    # literal by construction, so scanning them yields only false positives.
    r"(^|/)(uv\.lock|dvc\.lock|poetry\.lock|package-lock\.json"
    r"|\.terraform\.lock\.hcl)$"
    r"|(^|/)\.dvc/|\.dvc$"
    r"|(^|/)\.secretsallow$"
    r"|(^|/)scripts/scan_secrets\.py$"
    r"|(^|/)tests/test_scan_secrets\.py$"
)

SENSITIVE = r"(?:access[_-]?key|secret[_-]?key|secretkey|accesskey|api[_-]?key|auth[_-]?token|password|passwd|bearer)"


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern
    why: str


RULES = [
    Rule(
        "portal-uuid-key",
        re.compile(rf'{SENSITIVE}["\']?\s*[:=]\s*["\']?'
                   r'([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-'
                   r'[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})', re.I),
        "a UUID-shaped API key assigned to a credential field",
    ),
    Rule(
        "long-hex-secret",
        re.compile(rf'{SENSITIVE}["\']?\s*[:=]\s*["\']?([0-9a-fA-F]{{32,}})', re.I),
        "a long hex secret assigned to a credential field",
    ),
    Rule(
        "quoted-credential",
        re.compile(rf'{SENSITIVE}["\']?\s*[:=]\s*["\']([^"\'\s${{}}]{{12,}})["\']', re.I),
        "a hard-coded credential literal",
    ),
    Rule(
        "private-key-block",
        re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |PGP )?PRIVATE KEY-----"),
        "a private key block",
    ),
    Rule(
        "aws-access-key-id",
        re.compile(r"\b(AKIA[0-9A-Z]{16})\b"),
        "an AWS access key id",
    ),
    Rule(
        # Catches nested blocks such as
        #     secret_keys:
        #       master: "<64 hex>"
        # where the credential field name is on the parent line, so the
        # name-adjacent rules above never see it.
        "bare-sha256-literal",
        re.compile(r'["\':=\s]([0-9a-fA-F]{64})["\'\s,]'),
        "a 64-character hex literal, the shape of an issued API secret",
    ),
]

# A content digest is structurally the opposite of a credential: publishing it
# is the entire point, and it cannot be rotated. But it is 64 hex characters,
# which is also what an issued API secret looks like, so `bare-sha256-literal`
# cannot tell them apart on shape alone.
#
# Baselining each one in .secretsallow does not work here. The fingerprint is
# sha256(path\0value), so a NEW digest is a NEW fingerprint: every snapshot
# republish would open a pull request that is red by construction and needs a
# hand-written allow-list line. An allow-list that grows on every routine
# release stops being read, and an unread allow-list is where a real credential
# goes to hide.
#
# So the exemption is structural instead: it must match BOTH a path and a field
# name. A 64-hex literal in any other file is still a finding, and a 64-hex
# literal in a credential-shaped field of a manifest is still a finding -- only
# the digest field of a snapshot manifest is exempt.
@dataclass(frozen=True)
class Exemption:
    name: str
    path: re.Pattern
    # Must capture the exempt value in group 1: the span of that group is what
    # gets compared against findings, so a rule firing on the same characters
    # is suppressed while one firing elsewhere in the file is not.
    pattern: re.Pattern
    why: str


EXEMPTIONS = [
    Exemption(
        "snapshot-manifest-digest",
        # Anchored at the repository root: a vendored or copied tree that
        # happens to contain infra/snapshots/*.json is not this project's
        # manifest and gets no exemption.
        re.compile(r"^infra/snapshots/[^/]+\.json$"),
        re.compile(r'"sha256"\s*:\s*"([0-9a-fA-F]{64})"'),
        "the content digest of a published snapshot artifact",
    ),
]


def exempt_spans(text: str, path: str) -> list[tuple[int, int]]:
    """Character ranges in this file that are known not to be credentials.

    Scoped by path AND by the field the value is assigned to. Both must match,
    so renaming the field or moving the file re-arms the scanner rather than
    silently keeping the exemption.
    """
    spans = []
    for exemption in EXEMPTIONS:
        if not exemption.path.search(path):
            continue
        for match in exemption.pattern.finditer(text):
            spans.append(match.span(1))
    return spans


# Values that are obviously not credentials, so contributors are not forced to
# baseline every example. Keep this list short and literal.
PLACEHOLDERS = re.compile(
    r"^(?:|null|none|true|false|changeme|your[-_\w]*|x{3,}|\.{3,}|"
    r"<[^>]+>|\$\{?\w+\}?|os\.environ.*|os\.getenv.*|placeholder\w*|example\w*|"
    r"test[-_]\w*|fake[-_]\w*|dummy\w*|[-_\w]*(?:here|goes[-_]here))$",
    re.I,
)


@dataclass(frozen=True)
class Finding:
    path: str
    rule: str
    why: str
    commit: str
    fingerprint: str


def fingerprint(path: str, value: str) -> str:
    """Identify a finding by where it is and what it is, never by its value.

    Deliberately independent of which rule fired: one credential usually
    matches several rules, and baselining it once must silence all of them.
    """
    digest = hashlib.sha256(f"{path}\0{value}".encode()).hexdigest()
    return f"{path}:{digest[:16]}"


def scan_text(text: str, path: str, commit: str = "worktree") -> list[Finding]:
    findings: dict[str, Finding] = {}
    exempt = exempt_spans(text, path)
    for rule in RULES:
        for match in rule.pattern.finditer(text):
            span = match.span(1) if rule.pattern.groups else match.span(0)
            # Containment, not equality: a rule whose group covers part of an
            # exempt value is describing the same characters, and a substring
            # of a published digest is not a credential either.
            if any(start <= span[0] and span[1] <= end for start, end in exempt):
                continue
            value = (match.group(1) if rule.pattern.groups else match.group(0)).strip()
            if PLACEHOLDERS.match(value):
                continue
            mark = fingerprint(path, value)
            # First rule wins: RULES is ordered most specific first, so the
            # reported reason is the most informative one.
            findings.setdefault(mark, Finding(
                path=path, rule=rule.name, why=rule.why, commit=commit,
                fingerprint=mark,
            ))
    return list(findings.values())


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd or REPO_ROOT,
        capture_output=True, text=True, check=True,
    ).stdout


def commits_in(rev_range: str, cwd: Path | None = None) -> list[str]:
    out = git("rev-list", rev_range, cwd=cwd).strip()
    return out.splitlines() if out else []


def files_in(commit: str, cwd: Path | None = None) -> list[str]:
    out = git("ls-tree", "-r", "--name-only", commit, cwd=cwd).strip()
    return out.splitlines() if out else []


def changed_in(commit: str, cwd: Path | None = None) -> list[str]:
    """Files this commit added or modified."""
    out = git("show", "--pretty=format:", "--name-only", "--diff-filter=AM",
              commit, cwd=cwd).strip()
    return [line for line in out.splitlines() if line]


def blob(commit: str, path: str, cwd: Path | None = None) -> str | None:
    try:
        return subprocess.run(
            ["git", "show", f"{commit}:{path}"], cwd=cwd or REPO_ROOT,
            capture_output=True, text=True, check=True,
        ).stdout
    except subprocess.CalledProcessError:
        return None


def scan_commits(commits: list[str], cwd: Path | None = None,
                 every_file: bool = False) -> list[Finding]:
    findings: list[Finding] = []
    for commit in commits:
        paths = files_in(commit, cwd) if every_file else changed_in(commit, cwd)
        for path in paths:
            if SKIP.search(path):
                continue
            text = blob(commit, path, cwd)
            if text is None:
                continue
            findings.extend(scan_text(text, path, commit[:12]))
    return findings


def scan_worktree(root: Path | None = None) -> list[Finding]:
    root = root or REPO_ROOT
    findings: list[Finding] = []
    for path in git("ls-files", cwd=root).splitlines():
        if SKIP.search(path):
            continue
        full = root / path
        try:
            text = full.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        findings.extend(scan_text(text, path))
    return findings


def load_allowlist(path: Path | None = None) -> set[str]:
    path = path or ALLOWLIST
    if not path.exists():
        return set()
    entries = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        # An entry may carry an inline justification: "<fingerprint>  # why".
        entry = line.split("#", 1)[0].strip()
        if entry:
            entries.add(entry)
    return entries


def report(findings: list[Finding]) -> None:
    print(f"{len(findings)} unreviewed credential finding(s):\n", file=sys.stderr)
    seen = set()
    for finding in findings:
        if finding.fingerprint in seen:
            continue
        seen.add(finding.fingerprint)
        print(f"  {finding.path}  (commit {finding.commit})", file=sys.stderr)
        print(f"    {finding.why}", file=sys.stderr)
        print(f"    fingerprint: {finding.fingerprint}\n", file=sys.stderr)
    print(
        "The value is not printed here on purpose. If one of these is a real\n"
        "credential: rotate it first, then remove it from the code, then decide\n"
        "whether the history needs rewriting. Deleting the line does not revoke\n"
        "the key. If a finding is genuinely not a credential, add its\n"
        "fingerprint to .secretsallow with a comment saying why.",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--range", help="commit range, e.g. origin/dev..HEAD")
    source.add_argument("--worktree", action="store_true",
                        help="scan tracked files as they stand")
    source.add_argument("--all-history", action="store_true",
                        help="scan every commit reachable from HEAD")
    args = parser.parse_args(argv)

    if args.worktree:
        findings = scan_worktree()
    elif args.all_history:
        findings = scan_commits(commits_in("HEAD"))
    else:
        rev_range = args.range or "origin/dev..HEAD"
        commits = commits_in(rev_range)
        print(f"Scanning {len(commits)} commit(s) in {rev_range}")
        findings = scan_commits(commits)

    allowed = load_allowlist()
    findings = [f for f in findings if f.fingerprint not in allowed]

    if findings:
        report(findings)
        return 1

    print("No unreviewed credentials found.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
