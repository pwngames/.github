"""Create a protected, history-preserving main -> develop pull request.

No source code is executed, no protected branch is pushed and no merge bypass is
available. GITHUB_TOKEN PRs get an explicit CI dispatch, as release-please does.
"""
import argparse
import io
import json
import os
import re
import subprocess
import zipfile
from pathlib import Path

SHA = re.compile(r"[a-f0-9]{40}\Z")
RECEIPT = 'production-sync-receipt'


def command(*args, cwd=None, check=True):
    result = subprocess.run(args, cwd=cwd, check=check, capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(f'{args[0]} failed')
    return result.stdout.strip()


def api(path, *args):
    return json.loads(command('gh', 'api', path, *args))


def source_sha(repo, workflow, run_id, receipt=False):
    run = api(f'repos/{repo}/actions/runs/{run_id}')
    if (run.get('conclusion') != 'success' or run.get('path') != f'.github/workflows/{workflow}'
            or run.get('head_repository', {}).get('full_name') != repo):
        raise ValueError('Only a successful run of the configured delivery workflow is accepted.')
    if receipt:
        artifacts = api(f'repos/{repo}/actions/runs/{run_id}/artifacts')['artifacts']
        matches = [item for item in artifacts if item['name'] == RECEIPT and not item['expired']]
        if not matches:
            return None  # A successful development delivery is deliberately not eligible.
        if len(matches) != 1 or matches[0]['size_in_bytes'] > 16384:
            raise ValueError('Invalid production delivery receipt artifact.')
        data = subprocess.run(['gh', 'api', f"repos/{repo}/actions/artifacts/{matches[0]['id']}/zip"],
                              capture_output=True, check=True).stdout
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            if names != ['production-sync-receipt.json'] or archive.getinfo(names[0]).file_size > 4096:
                raise ValueError('Invalid production delivery receipt archive.')
            proof = json.loads(archive.read(names[0]))
        return receipt_sha(proof, repo, run_id)
    if run.get('head_branch') != 'main' or run.get('event') not in ('push', 'workflow_dispatch'):
        return None
    return valid_sha(run['head_sha'])


def valid_sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError('A full source commit SHA is required.')
    return value


def receipt_sha(proof, repo, run_id):
    if (proof.get('repository') != repo or str(proof.get('run_id')) != str(run_id)
            or proof.get('branch') != 'main' or proof.get('environment') != 'production'
            or proof.get('success') is not True):
        raise ValueError('Delivery receipt does not prove this production run.')
    return valid_sha(proof.get('sha'))


def git(repo, *args, check=True):
    return command('git', '-C', str(repo), *args, check=check)


def sync_branch(repo, main_sha):
    """Only synthesize a normal merge of the two remote histories."""
    git(repo, 'fetch', 'origin', 'main', 'develop')
    if git(repo, 'rev-parse', 'origin/main') != main_sha:
        return None  # A newer production promotion owns synchronization.
    if subprocess.run(['git', '-C', str(repo), 'merge-base', '--is-ancestor', main_sha, 'origin/develop'],
                      capture_output=True).returncode == 0:
        return None
    branch = 'sync/main-to-develop-' + main_sha[:12]
    remote = git(repo, 'ls-remote', '--heads', 'origin', 'refs/heads/' + branch)
    if remote:
        git(repo, 'fetch', 'origin', branch)
        head = git(repo, 'rev-parse', 'FETCH_HEAD')
        unique = git(repo, 'rev-list', head, '--not', 'origin/main', 'origin/develop').split()
        for commit in unique:
            parents = git(repo, 'show', '-s', '--format=%P', commit).split()
            if len(parents) != 2:
                raise ValueError('Existing sync branch contains unexpected non-merge history.')
            expected = git(repo, 'merge-tree', '--write-tree', *parents)
            if git(repo, 'rev-parse', commit + '^{tree}') != expected:
                raise ValueError('Existing sync branch contains changes outside the history merge.')
        if subprocess.run(['git', '-C', str(repo), 'merge-base', '--is-ancestor', main_sha, head],
                          capture_output=True).returncode:
            raise ValueError('Existing sync branch does not include this main history.')
        git(repo, 'checkout', '--detach', head)
        git(repo, 'config', 'user.name', 'github-actions[bot]')
        git(repo, 'config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
        try:
            git(repo, 'merge', '--no-ff', '--no-edit', '-m', 'chore: refresh sync with current develop', 'origin/develop')
        except subprocess.CalledProcessError:
            git(repo, 'merge', '--abort', check=False)
            raise ValueError('Updated develop conflicts with synchronization; manual resolution is required.') from None
        head = git(repo, 'rev-parse', 'HEAD')
        if git(repo, 'ls-remote', 'origin', 'refs/heads/main').split()[0] != main_sha:
            return None
        git(repo, 'push', 'origin', 'HEAD:refs/heads/' + branch)
        return branch, head
    git(repo, 'checkout', '-b', branch, 'origin/develop')
    git(repo, 'config', 'user.name', 'github-actions[bot]')
    git(repo, 'config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
    try:
        git(repo, 'merge', '--no-ff', '--no-edit', '-m', 'chore: synchronize main history into develop', main_sha)
    except subprocess.CalledProcessError:
        git(repo, 'merge', '--abort', check=False)
        raise ValueError('Main/develop conflict: no branch was pushed; manual conflict resolution is required.') from None
    # Recheck main immediately before publishing; never force-push any branch.
    latest = git(repo, 'ls-remote', 'origin', 'refs/heads/main').split()[0]
    if latest != main_sha:
        return None
    git(repo, 'push', 'origin', 'HEAD:refs/heads/' + branch)
    return branch, git(repo, 'rev-parse', 'HEAD')


def prepare_owned_pr_ci(repo, number, branch, head, *, timeout=60):
    """Approve only CI for this verified synthetic-history PR, never foreign runs.

    Called only after sync_branch has proven every sync-only commit is a clean
    two-parent merge of protected main/develop history. Repository GITHUB_TOKEN
    may approve its own generated PR workflow without changing approval policy.
    """
    import time
    pull = api(f'repos/{repo}/pulls/{number}')
    if (pull.get('state') != 'open' or pull.get('head', {}).get('sha') != head
            or pull.get('head', {}).get('ref') != branch
            or pull.get('head', {}).get('repo', {}).get('full_name') != repo
            or pull.get('base', {}).get('ref') != 'develop'
            or not re.fullmatch(r'sync/main-to-develop-[a-f0-9]{12}', branch)):
        raise ValueError('PR does not match the verified owned history merge.')
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        runs = api(f'repos/{repo}/actions/workflows/ci.yml/runs?branch={branch}&event=pull_request&head_sha={head}&per_page=20')['workflow_runs']
        for run in runs:
            if (run.get('head_sha') != head or run.get('head_branch') != branch
                    or run.get('path') != '.github/workflows/ci.yml' or run.get('event') != 'pull_request'
                    or run.get('head_repository', {}).get('full_name') != repo):
                continue
            linked = any(item.get('number') == number and item.get('head', {}).get('sha') == head
                         and item.get('head', {}).get('ref') == branch
                         and item.get('base', {}).get('ref') == 'develop' for item in run.get('pull_requests', []))
            if not linked:
                continue
            if run.get('conclusion') == 'action_required':
                if (pull.get('user', {}).get('login') != 'github-actions[bot]'
                        or run.get('actor', {}).get('login') != 'github-actions[bot]'):
                    raise ValueError('Refusing to approve CI that was not created by the sync bot.')
                # Re-read the current PR immediately before approval; a queued
                # workflow may have appeared after the head was moved.
                current = api(f'repos/{repo}/pulls/{number}')
                if (current.get('state') != 'open' or current.get('head', {}).get('sha') != head
                        or current.get('head', {}).get('ref') != branch
                        or current.get('head', {}).get('repo', {}).get('full_name') != repo
                        or current.get('base', {}).get('ref') != 'develop'
                        or current.get('user', {}).get('login') != 'github-actions[bot]'):
                    raise ValueError('Sync PR moved before approval; refusing queued CI approval.')
                api(f"repos/{repo}/actions/runs/{run['id']}/approve", '--method', 'POST')
            return run['id']
        time.sleep(5)
    return None


def wait_for_dispatched_ci(repo, branch, head, *, timeout=2100, event="workflow_dispatch", run_id=None):
    """Only a successful real CI dispatch on the exact owned head qualifies.

    PR-event runs created by GITHUB_TOKEN may need human approval. Explicit
    dispatch runs avoid that recursion guard without approving unknown runs.
    """
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        runs = api(f'repos/{repo}/actions/workflows/ci.yml/runs?branch={branch}&event={event}&head_sha={head}&per_page=20')['workflow_runs']
        candidates = [run for run in runs if run.get('head_sha') == head and run.get('head_branch') == branch
                      and run.get('event') == event and run.get('path') == '.github/workflows/ci.yml'
                      and (run_id is None or run.get('id') == run_id)]
        if candidates:
            run = candidates[0]
            if run['status'] == 'completed' and run.get('conclusion') != 'action_required':
                if run.get('conclusion') != 'success':
                    raise ValueError('Dispatched synchronization CI did not succeed; no auto-merge enabled.')
                jobs = api(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100")['jobs']
                if not any(job.get('name') in ('ci-gate', 'verify') and job.get('conclusion') == 'success' for job in jobs):
                    raise ValueError('Dispatched CI has no successful trusted gate; no auto-merge enabled.')
                return run['id']
        time.sleep(15)
    raise ValueError('Timed out waiting for exact-head synchronization CI; no auto-merge enabled.')


def protected_auto_merge(repo, number, head):
    settings = api(f'repos/{repo}')
    if not settings.get('allow_auto_merge') or not settings.get('allow_merge_commit'):
        print('::notice::Sync PR requires manual merge: protected merge-commit auto-merge is not enabled.')
        return False
    # Branch metadata is readable with contents:read. The administration-only
    # protection endpoint is deliberately unavailable to GITHUB_TOKEN.
    branch = api(f'repos/{repo}/branches/develop')
    if not branch.get('protected'):
        print('::notice::Sync PR requires manual merge: develop is not protected.')
        return False
    pull = api(f'repos/{repo}/pulls/{number}')
    if pull.get('head', {}).get('sha') != head or pull.get('base', {}).get('ref') != 'develop':
        raise ValueError('Sync PR changed after validation; refusing auto-merge.')
    command('gh', 'pr', 'merge', str(number), '--repo', repo, '--auto', '--merge', '--match-head-commit', head)
    return True


def run(args):
    repo = os.environ['GITHUB_REPOSITORY']
    if args.find_production_run:
        main = api(f'repos/{repo}/git/ref/heads/main')['object']['sha']
        runs = api(f'repos/{repo}/actions/workflows/{args.workflow}/runs?status=success&per_page=100')['workflow_runs']
        sha = None
        for candidate in runs:
            # Nested delivery workflows have default-branch metadata; their
            # run-bound receipt is the only accepted production source proof.
            if not args.receipt and candidate.get('head_sha') != main:
                continue
            proof = source_sha(repo, args.workflow, str(candidate['id']), args.receipt)
            if proof == main:
                sha = proof
                break
    else:
        sha = (source_sha(repo, args.workflow, args.run_id, args.receipt) if args.run_id
               else valid_sha(args.sha))
    if sha is None:
        print('Not a verified production delivery; nothing to synchronize.')
        return
    try:
        api(f'repos/{repo}/git/ref/heads/develop')
        has_develop = True
    except subprocess.CalledProcessError as error:
        if '404' not in (error.stderr or ''):
            raise
        has_develop = False
    if not has_develop:
        print('Repository has no develop branch; synchronization is not applicable.')
        return
    result = sync_branch(Path(args.repository), sha)
    if result is None:
        print('Main is already included in develop, or a newer main superseded this delivery.')
        return
    branch, head = result
    owner = repo.split('/')[0]
    pulls = api(f'repos/{repo}/pulls?state=open&base=develop&head={owner}:{branch}')
    if len(pulls) > 1:
        raise ValueError('Multiple sync PRs unexpectedly exist.')
    if pulls:
        number = pulls[0]['number']
    else:
        pull = api(f'repos/{repo}/pulls', '--method', 'POST', '-f', 'base=develop', '-f', f'head={branch}',
                   '-f', 'title=chore: synchronize production main into develop', '-f',
                   'body=Preserve the deployed main history in develop through a normal merge commit. '
                   'No manual source edits or protected-branch pushes. Required checks and reviews still apply.')
        number = pull['number']
    # GITHUB_TOKEN suppresses PR event workflows. workflow_dispatch is the supported
    # explicit trigger and validates exactly this sync branch without deploying it.
    ci_run = prepare_owned_pr_ci(repo, number, branch, head)
    if ci_run is None:
        command('gh', 'workflow', 'run', 'ci.yml', '--repo', repo, '--ref', branch)
    current = api(f'repos/{repo}/git/ref/heads/main')['object']['sha']
    if current == sha and args.auto_merge:
        wait_for_dispatched_ci(repo, branch, head, event='pull_request' if ci_run else 'workflow_dispatch', run_id=ci_run)
        if api(f'repos/{repo}/git/ref/heads/main')['object']['sha'] != sha:
            print('Newer main superseded this validated sync PR; no auto-merge enabled.')
            return
        protected_auto_merge(repo, number, head)
    print(f'https://github.com/{repo}/pull/{number}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', default='repository')
    parser.add_argument('--workflow', default='ci.yml')
    parser.add_argument('--run-id', default='')
    parser.add_argument('--sha', default='')
    parser.add_argument('--receipt', action='store_true')
    parser.add_argument('--auto-merge', action='store_true')
    parser.add_argument('--find-production-run', action='store_true')
    run(parser.parse_args())
