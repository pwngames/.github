"""Fail-closed CI classification and exact-tree qualification, using stdlib + gh."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import zipfile

HEX = re.compile(r"[0-9a-f]{40}\Z")
REPO = re.compile(r"[\w.-]+/[\w.-]+\Z")
JOB = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*\Z")
MAX_ARTIFACT = 65536
MAX_RECEIPT = 16384
DOC = re.compile(r"README[^/]*\.md\Z")


def compact(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def fingerprint(value):
    return hashlib.sha256(compact(value).encode()).hexdigest()


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


class GitHub:
    def get(self, endpoint, raw=False):
        output = subprocess.check_output(["gh", "api", endpoint], stderr=subprocess.DEVNULL, timeout=30)
        return output if raw else json.loads(output)


def inputs(env=os.environ):
    repo = env.get("GITHUB_REPOSITORY", "")
    ref = env.get("CI_IMPLEMENTATION_REF", "")
    workflow = env.get("CI_WORKFLOW_PATH", ".github/workflows/ci.yml")
    expected = json.loads(env.get("CI_EXPECTED_JOBS", "[]"))
    trusted = json.loads(env.get("CI_TRUSTED_WORKFLOWS", "[]")) or [workflow]
    gate_names = json.loads(env.get("CI_GATE_NAMES", "{}")) or {p: env.get("CI_GATE_NAME", "ci-gate") for p in trusted}
    if env.get("GITHUB_ACTIONS") == "true" and env.get("CI_ACTION_REF") != ref:
        raise ValueError("Action execution reference must match the immutable policy reference")
    if not REPO.fullmatch(repo) or not HEX.fullmatch(ref):
        raise ValueError("Repository and immutable implementation ref are required")
    if not re.fullmatch(r"\.github/workflows/[\w.-]+\.ya?ml", workflow):
        raise ValueError("Invalid CI workflow path")
    if not isinstance(trusted, list) or not trusted or len(set(trusted)) != len(trusted) or any(not isinstance(x, str) or not re.fullmatch(r"\.github/workflows/[\w.-]+\.ya?ml", x) for x in trusted) or workflow not in trusted:
        raise ValueError("Invalid trusted CI workflow list")
    if not isinstance(gate_names, dict) or set(gate_names) != set(trusted) or any(not isinstance(v, str) or not v or len(v) > 200 for v in gate_names.values()):
        raise ValueError("Gate names must map every trusted workflow to its exact aggregate job label")
    if not isinstance(expected, list) or not expected or len(set(expected)) != len(expected) or any(not isinstance(x, str) or not JOB.fullmatch(x) for x in expected):
        raise ValueError("Expected jobs must be a nonempty unique job-id list")
    profile = env.get("CI_PROFILE", "")
    context = env.get("CI_VALIDATION_CONTEXT", "")
    if not profile or len(profile) > 200 or not context or len(context) > 1000:
        raise ValueError("Explicit bounded profile and validation context are required")
    return {"repository": repo, "implementation": ref, "workflow": workflow, "profile": profile,
            "context": context, "expected_jobs": sorted(expected), "trusted_workflows": sorted(trusted), "gate_names": gate_names}


def local_identity(config, env=os.environ):
    if git("status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Tracked checkout files changed after checkout")
    sha = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    if not HEX.fullmatch(sha) or not HEX.fullmatch(tree) or sha != env.get("GITHUB_SHA"):
        raise ValueError("Actual checkout must match the event checkout SHA")
    workflow_blob = git("rev-parse", f"HEAD:{config['workflow']}")
    if not HEX.fullmatch(workflow_blob):
        raise ValueError("Workflow blob is missing")
    blobs = {p: git("rev-parse", f"HEAD:{p}") for p in config["trusted_workflows"]}
    if any(not HEX.fullmatch(blob) for blob in blobs.values()):
        raise ValueError("Trusted workflow policy is missing")
    return {**config, "checkout_sha": sha, "tree": tree, "workflow_blob": workflow_blob, "workflow_blobs": blobs}


def docs_only(paths):
    # Explicit human-documentation allowlist; never arbitrary app Markdown.
    return bool(paths) and all(isinstance(p, str) and not p.startswith("/") and ".." not in PurePosixPath(p).parts
                               and (p.startswith("docs/") or DOC.fullmatch(p)) for p in paths)


def policy_changed(paths):
    return any(p.startswith(".github/") or PurePosixPath(p).name in
               ("AGENTS.md", "Dockerfile", ".dockerignore", ".nvmrc", ".npmrc", "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock")
               or "config" in PurePosixPath(p).name.lower()
               or PurePosixPath(p).name.startswith(("requirements", ".env"))
               or PurePosixPath(p).suffix in (".toml", ".ini", ".cfg") for p in paths)


def changed_paths(env=os.environ):
    event = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text())
    if env.get("GITHUB_EVENT_NAME") == "pull_request":
        base = event["pull_request"]["base"]["sha"]
    elif env.get("GITHUB_EVENT_NAME") == "push":
        base = event.get("before", "")
    else:
        return None
    if not HEX.fullmatch(base) or base == "0" * 40:
        return None
    # A missing shallow base or git failure means full validation.
    output = subprocess.check_output(["git", "diff", "--name-only", "-z", "--no-renames", base, "HEAD"], timeout=15)
    return [p.decode("utf8", errors="strict") for p in output.split(b"\0") if p]


def artifact_name(identity, attempt):
    return f"ci-qualification-{identity['tree']}-{fingerprint({k: identity[k] for k in ('profile', 'context', 'workflow', 'implementation')})[:20]}-attempt-{attempt}"


def read_receipt(payload):
    if len(payload) > MAX_ARTIFACT:
        raise ValueError("Artifact exceeds bound")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        files = archive.infolist()
        if len(files) != 1 or files[0].filename != "ci-qualification.json" or files[0].file_size > MAX_RECEIPT or files[0].flag_bits & 1 or files[0].compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise ValueError("Unexpected receipt archive")
        return json.loads(archive.read(files[0]))


def source_checkout_valid(api, repo, run, receipt):
    sha = receipt["checkout_sha"]
    commit = api.get(f"repos/{repo}/git/commits/{sha}")
    if commit.get("sha") != sha or commit.get("tree", {}).get("sha") != receipt["tree"]:
        return False
    if run["event"] != "pull_request":
        return sha == run.get("head_sha")
    # Actions run metadata identifies the PR head; checkout usually uses its
    # synthetic merge SHA. Never treat that head as the actual tested tree.
    if sha == run.get("head_sha"):
        return False  # default PR merge validation, not an unqualified head-only checkout
    parents = commit.get("parents", [])
    if len(parents) != 2 or parents[1].get("sha") != run.get("head_sha"):
        return False
    source_pr = receipt.get("source_pr", {})
    number = source_pr.get("number")
    if type(number) is not int or number <= 0 or source_pr.get("head") != parents[1]["sha"] or source_pr.get("base") != parents[0]["sha"]:
        return False
    prs = run.get("pull_requests", [])
    if any(p.get("number") == number and p.get("head", {}).get("sha") == parents[1]["sha"] and p.get("base", {}).get("sha") == parents[0]["sha"] for p in prs):
        return True
    # Merged PR runs can lose their pull_requests metadata. Corroborate the
    # recorded event PR against the exact source-head commit association, then
    # verify both repositories and stable head. Current base SHA may advance.
    associated = api.get(f"repos/{repo}/commits/{run['head_sha']}/pulls?per_page=100")
    if not isinstance(associated, list) or len(associated) >= 100 or not any(p.get("number") == number for p in associated):
        return False
    pr = api.get(f"repos/{repo}/pulls/{number}")
    return (pr.get("number") == number and pr.get("head", {}).get("sha") == parents[1]["sha"]
            and pr.get("head", {}).get("repo", {}).get("full_name") == repo
            and pr.get("base", {}).get("repo", {}).get("full_name") == repo)


def verify_proof(api, identity, run, artifact, receipt):
    repo = identity["repository"]
    if run.get("repository", {}).get("full_name") != repo or run.get("head_repository", {}).get("full_name") != repo:
        raise ValueError("Foreign or missing source repository")
    if run.get("path") not in identity["trusted_workflows"] or run.get("status") != "completed" or run.get("conclusion") != "success" or run.get("event") not in ("push", "pull_request", "workflow_dispatch"):
        raise ValueError("Source run is not successful trusted CI")
    for key, value in identity.items():
        if key != "checkout_sha" and receipt.get(key) != value:
            raise ValueError(f"Qualification identity mismatch: {key}")
    source = artifact.get("workflow_run", {})
    if not all(type(x) is int and x > 0 for x in (run.get("id"), run.get("repository", {}).get("id"), run.get("head_repository", {}).get("id"), artifact.get("id"))) or source.get("id") != run.get("id") or source.get("head_sha") != run.get("head_sha") or source.get("repository_id") != run.get("repository", {}).get("id") or source.get("head_repository_id") != run.get("head_repository", {}).get("id"):
        raise ValueError("Artifact is not bound to this repository/run/head")
    attempt = receipt.get("attempt")
    if receipt.get("schema") != 1 or receipt.get("mode") != "full" or receipt.get("run_id") != run.get("id") or type(attempt) is not int or not 1 <= attempt <= run.get("run_attempt", 0):
        raise ValueError("Receipt is not a full source qualification")
    if artifact.get("name") != artifact_name(identity, attempt) or artifact.get("expired") is not False or artifact.get("size_in_bytes", MAX_ARTIFACT + 1) > MAX_ARTIFACT:
        raise ValueError("Artifact identity or bounds mismatch")
    if receipt.get("source_event") != run.get("event") or receipt.get("run_head_sha") != run.get("head_sha"):
        raise ValueError("Receipt source event/head mismatch")
    if not HEX.fullmatch(str(receipt.get("checkout_sha", ""))) or not source_checkout_valid(api, repo, run, receipt):
        raise ValueError("Actual tested checkout is not bound to the run")
    for workflow, expected_blob in identity["workflow_blobs"].items():
        blob = api.get(f"repos/{repo}/contents/{workflow}?ref={receipt['checkout_sha']}")
        if blob.get("sha") != expected_blob:
            raise ValueError("Source workflow policy changed")
    jobs = api.get(f"repos/{repo}/actions/runs/{run['id']}/attempts/{attempt}/jobs?per_page=100")
    if jobs.get("total_count", 101) > 100:
        raise ValueError("Source job listing truncated")
    gates = [j for j in jobs.get("jobs", []) if j.get("name") == identity["gate_names"][run["path"]]]
    if len(gates) != 1 or gates[0].get("conclusion") != "success":
        raise ValueError("Required source gate did not succeed")
    check_url = gates[0].get("check_run_url", "")
    match = re.fullmatch(r"https://api.github.com/repos/" + re.escape(repo) + r"/check-runs/(\d+)", check_url)
    if not match:
        raise ValueError("Unbound check URL")
    check = api.get(f"repos/{repo}/check-runs/{match[1]}")
    if check.get("app", {}).get("id") != 15368 or check.get("conclusion") != "success" or check.get("head_sha") not in (receipt["checkout_sha"], run.get("head_sha")):
        raise ValueError("Required gate is not trusted GitHub Actions")
    if receipt.get("results") != {job: "success" for job in identity["expected_jobs"]}:
        raise ValueError("Receipt did not validate every expected job")
    return {"run_id": run["id"], "attempt": attempt, "artifact_id": artifact["id"], "checkout_sha": receipt["checkout_sha"]}


def find_qualification(api, identity):
    repo = identity["repository"]
    runs = []
    for workflow in identity["trusted_workflows"]:
        listing = api.get(f"repos/{repo}/actions/workflows/{workflow.split('/')[-1]}/runs?status=success&per_page=10")
        runs.extend(listing.get("workflow_runs", [])[:10])
    prefix = artifact_name(identity, 1).rsplit("-", 1)[0] + "-"
    for run in runs:
        try:
            artifacts = api.get(f"repos/{repo}/actions/runs/{run['id']}/artifacts?per_page=100")
            if artifacts.get("total_count", 101) > 100:
                continue
            for artifact in artifacts.get("artifacts", []):
                if artifact.get("name", "").startswith(prefix) and artifact.get("size_in_bytes", MAX_ARTIFACT + 1) <= MAX_ARTIFACT:
                    receipt = read_receipt(api.get(f"repos/{repo}/actions/artifacts/{artifact['id']}/zip", raw=True))
                    return verify_proof(api, identity, run, artifact, receipt)
        except (ValueError, KeyError, TypeError, subprocess.SubprocessError, zipfile.BadZipFile, AttributeError, OSError, RuntimeError, NotImplementedError):
            continue
    return None


def plan(api, identity, paths):
    result = {"schema": 1, "identity": identity, "mode": "full", "reason": "no_valid_qualification"}
    if paths is None:
        return {**result, "reason": "unknown_change_set"}
    if docs_only(paths):
        return {**result, "mode": "docs", "reason": "audited_docs_only", "paths": paths}
    try:
        proof = find_qualification(api, identity)
        if proof:
            return {**result, "mode": "reuse", "reason": "verified_exact_tree", "proof": proof}
    except (ValueError, KeyError, TypeError, AttributeError, OSError, subprocess.SubprocessError):
        pass
    if policy_changed(paths):
        result["reason"] = "new_policy_without_full_qualification"
    return result


def validate_results(value, identity, selected):
    expected = identity["expected_jobs"]
    if set(value) != set(expected) | {"plan"} or value.get("plan", {}).get("result") != "success":
        raise ValueError("Unexpected/missing jobs or failed plan")
    outcome = "success" if selected == "full" else "skipped"
    if any(value[job].get("result") != outcome for job in expected):
        raise ValueError("Job outcomes contradict the verified plan")
    return {job: value[job]["result"] for job in expected}


def record(api, identity, selected, results, paths):
    if selected.get("schema") != 1 or selected.get("identity") != identity or selected.get("mode") not in ("full", "docs", "reuse"):
        raise ValueError("Plan identity mismatch")
    mode = selected["mode"]
    statuses = validate_results(results, identity, mode)
    if mode == "docs" and (not docs_only(paths) or paths != selected.get("paths")):
        raise ValueError("Docs classification no longer matches")
    if mode == "reuse":
        if paths is None:
            raise ValueError("Unknown change set cannot reuse validation")
        proof = selected.get("proof", {})
        if set(proof) != {"run_id", "attempt", "artifact_id", "checkout_sha"} or not all(type(proof.get(k)) is int and proof[k] > 0 for k in ("run_id", "attempt", "artifact_id")) or not HEX.fullmatch(str(proof.get("checkout_sha", ""))):
            raise ValueError("Invalid qualification proof identifiers")
        repo = identity["repository"]
        run = api.get(f"repos/{repo}/actions/runs/{proof['run_id']}")
        artifact = api.get(f"repos/{repo}/actions/artifacts/{proof['artifact_id']}")
        receipt = read_receipt(api.get(f"repos/{repo}/actions/artifacts/{proof['artifact_id']}/zip", raw=True))
        if verify_proof(api, identity, run, artifact, receipt) != proof:
            raise ValueError("Qualification proof changed")
    if mode != "full":
        return None
    return {"schema": 1, **identity, "mode": "full", "results": statuses}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("plan", "record"))
    args = parser.parse_args()
    identity = local_identity(inputs())
    try:
        paths = changed_paths()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError, UnicodeError):
        paths = None
    api = GitHub()
    outputs = {}
    if args.command == "plan":
        selected = plan(api, identity, paths)
        outputs = {"mode": selected["mode"], "tree": identity["tree"], "checkout-sha": identity["checkout_sha"], "plan": compact(selected)}
        print(f"CI mode: {selected['mode']} ({selected['reason']})")
    else:
        selected = json.loads(os.environ["CI_PLAN"])
        results = json.loads(os.environ["CI_RESULTS"])
        receipt = record(api, identity, selected, results, paths)
        outputs["full"] = "false"
        if receipt:
            run = int(os.environ["GITHUB_RUN_ID"])
            attempt = int(os.environ["GITHUB_RUN_ATTEMPT"])
            event_name = os.environ["GITHUB_EVENT_NAME"]
            if event_name not in ("push", "pull_request", "workflow_dispatch"):
                raise ValueError("Unsupported full qualification event")
            receipt.update(run_id=run, attempt=attempt, source_event=event_name, run_head_sha=identity["checkout_sha"])
            if event_name == "pull_request":
                event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
                pr = event["pull_request"]
                base, head = pr["base"]["sha"], pr["head"]["sha"]
                if not HEX.fullmatch(base) or not HEX.fullmatch(head) or git("rev-parse", "HEAD^1") != base or git("rev-parse", "HEAD^2") != head:
                    raise ValueError("Actual PR checkout must match both event merge parents")
                receipt.update(run_head_sha=head, source_pr={"number": event["number"], "base": base, "head": head})
            target = Path(os.environ["RUNNER_TEMP"]) / "ci-qualification.json"
            payload = compact(receipt)
            if len(payload.encode()) > MAX_RECEIPT:
                raise ValueError("Receipt exceeds bounds")
            target.write_text(payload + "\n")
            outputs.update(full="true", file=str(target), artifact=artifact_name(identity, attempt))
        print(f"CI gate validated: {selected['mode']}")
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        for key, value in outputs.items():
            output.write(f"{key}={value}\n")


if __name__ == "__main__":
    main()
