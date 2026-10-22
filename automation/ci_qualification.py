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
    required_artifacts = json.loads(env.get("CI_REQUIRED_ARTIFACTS", "[]"))
    trusted = json.loads(env.get("CI_TRUSTED_WORKFLOWS", "[]")) or [workflow]
    gate_names = json.loads(env.get("CI_GATE_NAMES", "{}")) or {p: env.get("CI_GATE_NAME", "ci-gate") for p in trusted}
    if env.get("GITHUB_ACTIONS") == "true" and env.get("CI_ACTION_REF") != ref:
        raise ValueError("Action execution reference must match the immutable policy reference")
    if not REPO.fullmatch(repo) or not HEX.fullmatch(ref):
        raise ValueError("Repository and immutable implementation ref are required")
    if not re.fullmatch(r"\.github/workflows/[\w.-]+\.ya?ml", workflow):
        raise ValueError("Invalid CI workflow path")
    if not isinstance(required_artifacts, list) or len(required_artifacts) > 10 or len(set(required_artifacts)) != len(required_artifacts) or any(not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", x) for x in required_artifacts):
        raise ValueError("Invalid required build artifact list")
    if not isinstance(trusted, list) or len(trusted) > 5 or not trusted or len(set(trusted)) != len(trusted) or any(not isinstance(x, str) or not re.fullmatch(r"\.github/workflows/[\w.-]+\.ya?ml", x) for x in trusted) or workflow not in trusted:
        raise ValueError("Invalid trusted CI workflow list")
    if not isinstance(gate_names, dict) or set(gate_names) != set(trusted) or any(not isinstance(v, str) or not v or len(v) > 200 for v in gate_names.values()):
        raise ValueError("Gate names must map every trusted workflow to its exact aggregate job label")
    if not isinstance(expected, list) or len(expected) > 30 or not expected or len(set(expected)) != len(expected) or any(not isinstance(x, str) or not JOB.fullmatch(x) for x in expected):
        raise ValueError("Expected jobs must be a nonempty unique job-id list")
    profile = env.get("CI_PROFILE", "")
    context = env.get("CI_VALIDATION_CONTEXT", "")
    if not profile or len(profile) > 200 or not context or len(context) > 1000:
        raise ValueError("Explicit bounded profile and validation context are required")
    return {"repository": repo, "implementation": ref, "workflow": workflow, "profile": profile,
            "context": context, "required_artifacts": sorted(required_artifacts), "expected_jobs": sorted(expected), "trusted_workflows": sorted(trusted), "gate_names": gate_names}


def local_identity(config, env=os.environ, expected_sha=None):
    if git("status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Tracked checkout files changed after checkout")
    sha = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    if not HEX.fullmatch(sha) or not HEX.fullmatch(tree) or sha != (expected_sha or env.get("GITHUB_SHA")):
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


def read_receipt(payload, filename="ci-qualification.json"):
    if len(payload) > MAX_ARTIFACT:
        raise ValueError("Artifact exceeds bound")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        files = archive.infolist()
        if len(files) != 1 or files[0].filename != filename or files[0].file_size > MAX_RECEIPT or files[0].flag_bits & 1 or files[0].compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
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


def verify_proof(api, identity, run, artifact, receipt, kind="qualification"):
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
    if type(receipt.get("schema")) is not int or receipt.get("schema") != 1 or type(receipt.get("run_id")) is not int or receipt.get("mode") not in (("full",) if kind == "qualification" else ("full", "docs", "reuse")) or receipt.get("run_id") != run.get("id") or type(attempt) is not int or not 1 <= attempt <= run.get("run_attempt", 0):
        raise ValueError("Receipt is not a full source qualification")
    expected_name = artifact_name(identity, attempt) if kind == "qualification" else f"ci-verdict-attempt-{attempt}"
    if artifact.get("name") != expected_name or artifact.get("expired") is not False or artifact.get("size_in_bytes", MAX_ARTIFACT + 1) > MAX_ARTIFACT:
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
    expected_result = "success" if receipt["mode"] == "full" else "skipped"
    if receipt.get("results") != {job: expected_result for job in identity["expected_jobs"]}:
        raise ValueError("Receipt did not validate every expected job")
    if kind == "qualification" and identity.get("required_artifacts"):
        artifacts = api.get(f"repos/{repo}/actions/runs/{run['id']}/artifacts?per_page=100")
        if artifacts.get("total_count", 101) > 100:
            raise ValueError("Build artifact listing truncated")
        for name in identity["required_artifacts"]:
            selected = [a for a in artifacts.get("artifacts", []) if a.get("name") == name and a.get("expired") is False
                        and a.get("workflow_run", {}).get("id") == run["id"] and a.get("workflow_run", {}).get("head_sha") == run["head_sha"]
                        and a.get("workflow_run", {}).get("repository_id") == run["repository"]["id"]
                        and a.get("workflow_run", {}).get("head_repository_id") == run["head_repository"]["id"]]
            if len(selected) != 1:
                raise ValueError("Required qualified build output is missing/expired")
    if kind == "verdict" and receipt["mode"] == "docs":
        verify_docs_source(api, identity["repository"], receipt)
    return {"run_id": run["id"], "attempt": attempt, "artifact_id": artifact["id"], "checkout_sha": receipt["checkout_sha"]}


def verify_docs_source(api, repo, receipt):
    paths, base = receipt.get("paths"), receipt.get("source_base", "")
    if not docs_only(paths) or not HEX.fullmatch(base):
        raise ValueError("Docs verdict has no strict source diff")
    comparison = api.get(f"repos/{repo}/compare/{base}...{receipt['checkout_sha']}")
    files = comparison.get("files", [])
    if comparison.get("merge_base_commit", {}).get("sha") != base or comparison.get("total_commits", 251) > 250 or len(files) >= 300:
        raise ValueError("Docs comparison is incomplete")
    actual = []
    for file in files:
        actual.append(file["filename"])
        if file.get("status") == "renamed":
            actual.append(file["previous_filename"])
    if sorted(set(actual)) != sorted(paths) or not docs_only(actual):
        raise ValueError("Docs verdict does not match actual source paths")


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
                if artifact.get("name", "").startswith(prefix) and artifact.get("expired") is False and artifact.get("size_in_bytes", MAX_ARTIFACT + 1) <= MAX_ARTIFACT:
                    receipt = read_receipt(api.get(f"repos/{repo}/actions/artifacts/{artifact['id']}/zip", raw=True))
                    return verify_proof(api, identity, run, artifact, receipt)
        except (ValueError, KeyError, TypeError, subprocess.SubprocessError, zipfile.BadZipFile, AttributeError, OSError, RuntimeError, NotImplementedError):
            continue
    return None


def plan(api, identity, paths):
    result = {"schema": 1, "identity": identity, "mode": "full", "reason": "no_valid_qualification"}
    if paths is None:
        return {**result, "reason": "unknown_change_set"}
    if docs_only(paths) and len(compact(paths).encode()) <= 4096:
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


def write_runtime_noop(identity, source_run, source_attempt, branch, env=os.environ):
    if branch not in ("main", "develop"):
        raise ValueError("Runtime noop receipts are only for release branches")
    run_id = env.get("GITHUB_RUN_ID", "")
    if not re.fullmatch(r"[1-9][0-9]*", run_id) or type(source_run) is not int or source_run <= 0 or type(source_attempt) is not int or source_attempt <= 0:
        raise ValueError("Invalid runtime noop provenance")
    payload = {"schema": 1, "repository": identity["repository"], "run_id": run_id, "branch": branch,
               "environment": "production" if branch == "main" else "development", "success": True,
               "sha": identity["checkout_sha"], "outcome": "no_runtime_change", "source_ci_run": str(source_run),
               "source_ci_attempt": source_attempt, "qualification_mode": "docs"}
    filename = "production-sync-receipt.json" if branch == "main" else "runtime-noop.json"
    target = Path(env["RUNNER_TEMP"]) / filename
    target.write_text(compact(payload) + "\n")
    return {"noop_file": str(target), "noop_artifact": "production-sync-receipt" if branch == "main" else "runtime-noop"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("plan", "record", "verdict"))
    args = parser.parse_args()
    identity = local_identity(inputs(), expected_sha=os.environ.get("CI_SOURCE_SHA") if args.command == "verdict" else None)
    try:
        paths = changed_paths()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError, UnicodeError):
        paths = None
    api = GitHub()
    outputs = {}
    if args.command == "plan":
        selected = plan(api, identity, paths)
        proof = selected.get("proof", {})
        outputs = {"mode": selected["mode"], "tree": identity["tree"], "checkout-sha": identity["checkout_sha"], "plan": compact(selected),
                   "qualified-run": str(proof.get("run_id", os.environ["GITHUB_RUN_ID"])),
                   "qualified-attempt": str(proof.get("attempt", os.environ["GITHUB_RUN_ATTEMPT"])),
                   "qualified-checkout": proof.get("checkout_sha", identity["checkout_sha"])}
        print(f"CI mode: {selected['mode']} ({selected['reason']})")
    elif args.command == "record":
        selected = json.loads(os.environ["CI_PLAN"])
        results = json.loads(os.environ["CI_RESULTS"])
        receipt = record(api, identity, selected, results, paths)
        outputs.update(full="false", mode=selected["mode"])
        verdict = {"schema": 1, **identity, "mode": selected["mode"], "results": {job: results[job]["result"] for job in identity["expected_jobs"]}}
        if selected["mode"] == "docs":
            verdict["paths"] = paths
        for name, payload_record in (("verdict", verdict), ("qualification", receipt)):
            if payload_record is None:
                continue
            run = int(os.environ["GITHUB_RUN_ID"])
            attempt = int(os.environ["GITHUB_RUN_ATTEMPT"])
            event_name = os.environ["GITHUB_EVENT_NAME"]
            if event_name not in ("push", "pull_request", "workflow_dispatch"):
                raise ValueError("Unsupported full qualification event")
            payload_record.update(run_id=run, attempt=attempt, source_event=event_name, run_head_sha=identity["checkout_sha"])
            event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
            payload_record["source_base"] = event.get("before") if event_name == "push" else event.get("pull_request", {}).get("base", {}).get("sha")
            if event_name == "pull_request":
                event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
                pr = event["pull_request"]
                base, head = pr["base"]["sha"], pr["head"]["sha"]
                if not HEX.fullmatch(base) or not HEX.fullmatch(head) or git("rev-parse", "HEAD^1") != base or git("rev-parse", "HEAD^2") != head:
                    raise ValueError("Actual PR checkout must match both event merge parents")
                payload_record.update(run_head_sha=head, source_pr={"number": event["number"], "base": base, "head": head})
            target = Path(os.environ["RUNNER_TEMP"]) / f"ci-{name}.json"
            payload = compact(payload_record)
            if len(payload.encode()) > MAX_RECEIPT:
                raise ValueError("Receipt exceeds bounds")
            target.write_text(payload + "\n")
            if name == "qualification":
                outputs.update(full="true", file=str(target), artifact=artifact_name(identity, attempt))
            else:
                outputs.update(verdict_file=str(target), verdict_artifact=f"ci-verdict-attempt-{attempt}")
        if selected["mode"] == "docs" and os.environ.get("CI_RECORD_RUNTIME_NOOP") == "true" and os.environ["GITHUB_EVENT_NAME"] == "push":
            outputs.update(write_runtime_noop(identity, int(os.environ["GITHUB_RUN_ID"]), int(os.environ["GITHUB_RUN_ATTEMPT"]), os.environ["GITHUB_REF_NAME"]))
        print(f"CI gate validated: {selected['mode']}")
    else:
        run_id = os.environ.get("CI_SOURCE_RUN", "")
        if not run_id.isdigit() or int(run_id) <= 0:
            raise ValueError("Explicit source run is required")
        repo = identity["repository"]
        if os.environ.get("GITHUB_EVENT_NAME") == "workflow_run":
            trigger = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text()).get("workflow_run", {})
            if str(trigger.get("id")) != run_id or trigger.get("head_sha") != identity["checkout_sha"] or trigger.get("head_repository", {}).get("full_name") != repo:
                raise ValueError("Delivery trigger is not this exact own source run")
        elif os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
            raise ValueError("Unsupported downstream verdict event")
        run = api.get(f"repos/{repo}/actions/runs/{run_id}")
        if run.get("event") != "push" or run.get("head_sha") != identity["checkout_sha"]:
            raise ValueError("Delivery verdict requires the exact own push checkout")
        artifacts = api.get(f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100")
        if artifacts.get("total_count", 101) > 100:
            raise ValueError("Verdict artifact listing truncated")
        selected = sorted([a for a in artifacts.get("artifacts", []) if re.fullmatch(r"ci-verdict-attempt-[1-9][0-9]*", a.get("name", ""))],
                          key=lambda a: int(a["name"].rsplit("-", 1)[1]), reverse=True)
        if not selected:
            raise ValueError("Source CI verdict is missing")
        artifact = selected[0]
        if artifact.get("expired") is not False or artifact.get("size_in_bytes", MAX_ARTIFACT + 1) > MAX_ARTIFACT:
            raise ValueError("Verdict artifact unavailable or oversized")
        verdict = read_receipt(api.get(f"repos/{repo}/actions/artifacts/{artifact['id']}/zip", raw=True), filename="ci-verdict.json")
        verify_proof(api, identity, run, artifact, verdict, kind="verdict")
        if verdict["checkout_sha"] != identity["checkout_sha"]:
            raise ValueError("Verdict checkout changed")
        outputs.update(mode=verdict["mode"], outcome="no_runtime_change" if verdict["mode"] == "docs" else "runtime_validation_required")
        if verdict["mode"] == "docs" and os.environ.get("CI_RECORD_RUNTIME_NOOP") == "true":
            outputs.update(write_runtime_noop(identity, run["id"], verdict["attempt"], run.get("head_branch")))
        print(f"Delivery source verdict verified: {verdict['mode']}")
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        for key, value in outputs.items():
            output.write(f"{key}={value}\n")


if __name__ == "__main__":
    main()
