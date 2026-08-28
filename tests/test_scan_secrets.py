"""The credential scanner must fail closed.

Every secret used here is synthetic and generated in a throwaway repository
under tmp_path. No real credential appears in this file.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import scan_secrets  # noqa: E402

# Synthetic, never issued by any portal.
FAKE_UUID = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
FAKE_HEX = "0" * 64


def run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    run_git(path, "init", "-q", "-b", "main")
    run_git(path, "config", "user.email", "test@example.invalid")
    run_git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("clean\n")
    run_git(path, "add", "-A")
    run_git(path, "commit", "-qm", "base")
    return path


def commit(repo: Path, name: str, body: str, message: str) -> None:
    # Parents, because some paths are meaningful: the snapshot-manifest
    # exemption is scoped by path, so its tests cannot write at the root.
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-qm", message)


def scan_range(repo: Path, rev_range: str) -> list:
    return scan_secrets.scan_commits(
        scan_secrets.commits_in(rev_range, cwd=repo), cwd=repo)


# ------------------------------------------------------------------ detection


def test_yaml_credentials_are_caught(repo: Path):
    commit(repo, "config.yaml",
           f'api:\n  access_key: "{FAKE_UUID}"\n'
           f'  secret_keys:\n    master: "{FAKE_HEX}"\n',
           "add config")

    findings = scan_range(repo, "main~1..main")

    assert len({f.fingerprint for f in findings}) == 2
    assert any(f.rule == "portal-uuid-key" for f in findings)


def test_python_header_credentials_are_caught(repo: Path):
    commit(repo, "config.py",
           'HEADERS = {\n'
           f'    "accesskey": "{FAKE_UUID}",\n'
           f'    "secretkey": "{FAKE_HEX}",\n'
           '}\n',
           "add headers")

    assert scan_range(repo, "main~1..main")


def test_private_key_block_is_caught(repo: Path):
    commit(repo, "id_rsa",
           "-----BEGIN RSA PRIVATE KEY-----\nnot-a-real-key\n", "add key")

    assert any(f.rule == "private-key-block" for f in scan_range(repo, "main~1..main"))


def test_secret_deleted_in_a_later_commit_is_still_caught(repo: Path):
    """Deleting the line does not revoke the key, so the range must still fail."""
    commit(repo, "config.yaml", f'access_key: "{FAKE_UUID}"\n', "add secret")
    commit(repo, "config.yaml", 'access_key: "${MY_KEY}"\n', "remove secret")

    assert scan_secrets.scan_worktree(repo) == []       # tree looks clean
    assert scan_range(repo, "main~2..main")             # history does not


# ------------------------------------------------------------------ no noise


def test_clean_commit_passes(repo: Path):
    commit(repo, "config.py",
           'import os\n\nACCESS_KEY = os.environ["MERI_PANCHAYAT_ACCESS_KEY"]\n',
           "env-backed")

    assert scan_range(repo, "main~1..main") == []


def test_env_example_with_names_only_passes(repo: Path):
    commit(repo, ".env.example",
           "MERI_PANCHAYAT_ACCESS_KEY=\nMERI_PANCHAYAT_SECRET_MASTER=\n",
           "add example")

    assert scan_range(repo, "main~1..main") == []


def test_lockfile_hashes_are_not_flagged(repo: Path):
    commit(repo, "uv.lock",
           f'[[package]]\nname = "x"\nhash = "sha256:{FAKE_HEX}"\n',
           "add lock")

    assert scan_range(repo, "main~1..main") == []


def test_placeholder_values_are_not_flagged(repo: Path):
    commit(repo, "config.yaml",
           'access_key: "your-access-key-here"\npassword: "<changeme>"\n',
           "add placeholders")

    assert scan_range(repo, "main~1..main") == []


# ------------------------------------------------- snapshot manifest digests

# The exemption is deliberately narrow, so each half of "path AND field" gets
# its own negative case: widening either one silently would let a real
# credential through, and only a test that fails on the widening will say so.


def _manifest(digest: str = FAKE_HEX) -> str:
    """A snapshot manifest, shaped like the real infra/snapshots/*.json."""
    return (
        '{\n'
        '  "label": "provisional_full_state_snapshot",\n'
        '  "bucket": "dpic-prdw-snapshots",\n'
        '  "key": "duckdb/database_allgps.duckdb",\n'
        f'  "sha256": "{digest}",\n'
        '  "byte_size": 1011363840\n'
        '}\n'
    )


def test_snapshot_manifest_digest_is_not_flagged(repo: Path):
    commit(repo, "infra/snapshots/full_state.json", _manifest(), "publish snapshot")

    assert scan_range(repo, "main~1..main") == []


def test_a_republished_snapshot_needs_no_new_allowlist_entry(repo: Path):
    r"""The point of the exemption: a NEW digest must not need a NEW baseline.

    The fingerprint is sha256(path\0value), so every republish produces a
    different one. If this regressed to an allow-list approach the second
    commit here would fail while the first passed.
    """
    commit(repo, "infra/snapshots/full_state.json", _manifest("a" * 64), "publish")
    commit(repo, "infra/snapshots/full_state.json", _manifest("b" * 64), "republish")

    assert scan_range(repo, "main~2..main") == []


def test_an_uppercase_digest_in_the_manifest_is_still_flagged(repo: Path):
    """The exemption tracks what the manifest will actually accept.

    SnapshotManifest.__post_init__ rejects anything but 64 lowercase hex, so an
    uppercase value in this field can never be a digest this project deploys --
    only a secret that happens to be sitting there. Exempting it would widen
    the hole rather than close it.
    """
    commit(repo, "infra/snapshots/full_state.json", _manifest("A" * 64), "uppercase")

    findings = scan_range(repo, "main~1..main")
    assert len(findings) == 1


def test_a_credential_field_in_a_manifest_is_still_flagged(repo: Path):
    """Path alone must not exempt: same file, credential-shaped field."""
    body = _manifest().replace(
        '  "byte_size": 1011363840\n',
        f'  "byte_size": 1011363840,\n  "api_key": "{"c" * 64}"\n',
    )
    commit(repo, "infra/snapshots/full_state.json", body, "leak a key")

    findings = scan_range(repo, "main~1..main")
    assert len(findings) == 1
    assert findings[0].path == "infra/snapshots/full_state.json"


def test_a_digest_field_outside_the_manifest_path_is_still_flagged(repo: Path):
    """Field name alone must not exempt: same field, different file."""
    commit(repo, "config.json", f'{{"sha256": "{FAKE_HEX}"}}\n', "add config")

    findings = scan_range(repo, "main~1..main")
    assert len(findings) == 1
    assert findings[0].path == "config.json"


def test_a_bare_digest_in_a_manifest_is_still_flagged(repo: Path):
    """Assignment to `sha256` is what is exempt, not 64 hex anywhere in the file."""
    body = _manifest().replace(
        '  "byte_size": 1011363840\n',
        f'  "byte_size": 1011363840,\n  "notes": ["{"d" * 64}"]\n',
    )
    commit(repo, "infra/snapshots/full_state.json", body, "add a stray hex")

    findings = scan_range(repo, "main~1..main")
    assert len(findings) == 1


# ---------------------------------------------------- container base digests


def test_pinned_base_image_digest_is_not_flagged(repo: Path):
    commit(repo, "docker/Dockerfile",
           f"FROM python:3.13-slim@sha256:{FAKE_HEX}\nRUN echo hi\n", "pin base")

    assert scan_range(repo, "main~1..main") == []


@pytest.mark.parametrize("path", [
    "docker/Dockerfile",
    "docker/Dockerfile.prod",
    "docker/api.Dockerfile",
])
@pytest.mark.parametrize("line", [
    "FROM python:3.13-slim@sha256:{d}",
    "from python:3.13-slim@sha256:{d}",                       # keyword is case-insensitive
    "  FROM python:3.13-slim@sha256:{d}",                     # indented
    "FROM --platform=$BUILDPLATFORM python:3.13-slim@sha256:{d}",
])
def test_base_digest_forms_that_docker_accepts_are_not_flagged(repo: Path, path: str, line: str):
    """Every spelling docker itself builds must clear the scanner.

    A form that builds fine but fails the scan has no way out except a
    .secretsallow entry, which goes stale on the next digest bump -- the exact
    trap #80 removed.
    """
    commit(repo, path, line.format(d=FAKE_HEX) + "\nRUN echo hi\n", f"pin via {path}")

    assert scan_range(repo, "main~1..main") == []


def test_a_credential_elsewhere_in_a_dockerfile_is_still_flagged(repo: Path):
    """The FROM line is exempt, not the file."""
    commit(repo, "docker/Dockerfile",
           f"FROM python:3.13-slim@sha256:{FAKE_HEX}\n"
           f'ENV API_KEY="{"e" * 64}"\n', "leak in a dockerfile")

    findings = scan_range(repo, "main~1..main")
    assert len(findings) == 1


def test_a_from_digest_outside_a_dockerfile_is_still_flagged(repo: Path):
    """Field alone must not exempt: same syntax, ordinary file."""
    commit(repo, "notes.txt", f"FROM python:3.13-slim@sha256:{FAKE_HEX}\n", "prose")

    findings = scan_range(repo, "main~1..main")
    assert len(findings) == 1


def test_an_uppercase_base_digest_is_still_flagged(repo: Path):
    """OCI digests are lowercase hex; an uppercase value there is not a digest."""
    commit(repo, "docker/Dockerfile",
           f"FROM python:3.13-slim@sha256:{'A' * 64}\n", "uppercase")

    findings = scan_range(repo, "main~1..main")
    assert len(findings) == 1


# ------------------------------------------------------------------ allowlist


def test_allowlisted_fingerprint_is_suppressed(repo: Path, tmp_path: Path):
    commit(repo, "config.yaml", f'access_key: "{FAKE_UUID}"\n', "add secret")
    findings = scan_range(repo, "main~1..main")
    assert findings

    baseline = tmp_path / ".secretsallow"
    baseline.write_text(f"# reviewed: synthetic\n{findings[0].fingerprint}\n")
    allowed = scan_secrets.load_allowlist(baseline)

    assert [f for f in findings if f.fingerprint not in allowed] == []


def test_report_never_prints_the_value(repo: Path, capsys):
    commit(repo, "config.yaml", f'access_key: "{FAKE_UUID}"\n', "add secret")

    scan_secrets.report(scan_range(repo, "main~1..main"))

    captured = capsys.readouterr()
    assert FAKE_UUID not in captured.err + captured.out
    assert "fingerprint:" in captured.err


# ------------------------------------------------------------------ this repo


def test_this_repository_has_no_unreviewed_credentials():
    """Every finding on this branch must be a recorded, justified baseline entry."""
    allowed = scan_secrets.load_allowlist()
    unreviewed = [f for f in scan_secrets.scan_worktree()
                  if f.fingerprint not in allowed]

    assert unreviewed == [], (
        "New credential findings are not covered by .secretsallow. Rotate the "
        "value first, then remove it from the code."
    )


def test_the_baseline_stays_small():
    """One exposure, three committed values. Growth past that is normalising leaks.

    The cap was two while only branch tips had been scanned. The dev -> main
    range spans 27 commits and reaches e69daf2, which carries a third
    `secretkey`: the file tells the next operator to paste in a fresh one when
    the scraper halts, so every refresh committed another value of the same
    header block. Three lines is still the one known exposure in #12, not a
    new one -- but the ratchet only works if raising it stays deliberate, so
    this number goes up by an explicit edit or not at all.

    #28 removes the headers from tracked source and takes this to zero.
    """
    assert len(scan_secrets.load_allowlist()) <= 3
