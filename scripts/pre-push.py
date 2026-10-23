#!/usr/bin/env python3
"""Validate the checked-out commit before allowing a source push."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def main():
    os.chdir(git("rev-parse", "--show-toplevel"))
    updates = []
    for line in sys.stdin:
        fields = line.split()
        if len(fields) != 4:
            raise ValueError("Malformed Git pre-push update")
        local_ref, sha, remote_ref, remote_sha = fields
        if set(sha) == {"0"}:
            continue  # Deleting a ref does not publish source code.
        if remote_ref in ("refs/heads/main", "refs/heads/master", "refs/heads/develop"):
            raise ValueError("Promote protected branches through a pull request")
        if git("rev-parse", sha + "^{commit}") != git("rev-parse", "HEAD"):
            raise ValueError("Check out the commit being pushed so local checks validate that exact code")
        updates.append((sha, remote_sha))
    if not updates:
        return 0
    if git("status", "--porcelain", "--untracked-files=normal"):
        raise ValueError("Commit or stash local changes first; checks must match the pushed commit")
    head = git("rev-parse", "HEAD")
    for sha, remote_sha in updates:
        known = subprocess.run(["git", "cat-file", "-e", remote_sha + "^{commit}"], capture_output=True).returncode == 0
        revisions = [remote_sha + ".." + sha] if known else [sha, "--not", "--remotes"]
        for commit in git("rev-list", *revisions).splitlines():
            subprocess.run(["git", "diff-tree", "--check", "--root", commit], check=True)
    gate = Path(".githooks/checks.sh")
    if not gate.is_file():
        raise ValueError("Missing .githooks/checks.sh; reinstall this branch's local checks")
    print("pre-push: running local CI checks for " + head[:12], flush=True)
    # Backend's existing laptop gate already isolates DB/cache and requires its
    # configured local stack. Other gates get a clean clone with no local .env,
    # dependencies, or build output; a running developer preview stays intact.
    if Path("ops/local/stack.sh").exists() and Path("scripts/ci-infrastructure.sh").exists():
        with open(os.devnull) as null:
            subprocess.run(["bash", str(gate)], stdin=null, check=True)
    else:
        setting = subprocess.run(["git", "config", "--get", "pwn.prepushScratchRoot"], capture_output=True, text=True).stdout.strip()
        scratch = Path(setting) if setting else Path.cwd() / "temp" / "prepush"
        scratch.mkdir(parents=True, exist_ok=True)
        source = str(Path.cwd())
        # Do not forward provider/cloud credentials or remote browser targets.
        allowed = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TERM", "TMPDIR", "DOCKER_HOST", "DOCKER_CONTEXT", "NVM_DIR", "PNPM_HOME"}
        environment = {key: value for key, value in os.environ.items() if key in allowed}
        environment["PWN_PREPUSH_SCRATCH_ROOT"] = str(scratch.resolve())
        with tempfile.TemporaryDirectory(prefix=Path(source).name + "-", dir=scratch) as folder:
            subprocess.run(["git", "clone", "--quiet", "--shared", "--no-checkout", source, folder], env=environment, check=True)
            subprocess.run(["git", "-C", folder, "checkout", "--quiet", "--detach", head], env=environment, check=True)
            with open(os.devnull) as null:
                subprocess.run(["bash", str(gate)], cwd=folder, env=environment, stdin=null, check=True)
    if git("rev-parse", "HEAD") != head or git("status", "--porcelain", "--untracked-files=normal"):
        raise ValueError("The checkout changed during validation; inspect it and run the push again")
    print("pre-push: local checks passed; hosted CI remains authoritative", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, subprocess.CalledProcessError) as error:
        print("pre-push blocked: " + str(error), file=sys.stderr)
        sys.exit(1)
