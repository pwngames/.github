"""Create a protected, history-preserving main -> develop pull request.

No source code is executed, no protected branch is pushed and no merge bypass is
available. Only verified owned PR CI is launched or approved; protected native
auto-merge waits for required checks without holding this runner open.
"""
import argparse
import io
import json
import os
import re
import subprocess
import zipfile
from pathlib import Path
from typing import NamedTuple

SHA = re.compile(r"[a-f0-9]{40}\Z")
RECEIPT = 'production-sync-receipt'


class PreparedCI(NamedTuple):
    run_id: int
    approval_accepted: bool = False


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


def read_owned_pull(repo, number, branch, head, *, attempts=15, allow_merged=False):
    """Wait only for stale PR head metadata after a proven remote branch push.

    Immutable binding changes fail immediately. A genuinely moved remote head
    also fails immediately; no foreign or stale-head workflow is approved.
    """
    import time
    for attempt in range(attempts):
        pull = api(f'repos/{repo}/pulls/{number}')
        merged = allow_merged and pull.get('state') == 'closed' and pull.get('merged') is True
        if ((pull.get('state') != 'open' and not merged) or pull.get('head', {}).get('ref') != branch
                or pull.get('head', {}).get('repo', {}).get('full_name') != repo
                or pull.get('base', {}).get('ref') != 'develop'
                or not re.fullmatch(r'sync/main-to-develop-[a-f0-9]{12}', branch)):
            raise ValueError('PR does not match the verified owned history merge.')
        if pull.get('head', {}).get('sha') == head:
            return pull
        if merged:
            raise ValueError('Merged PR does not match the verified sync head.')
        remote = api(f'repos/{repo}/git/ref/heads/{branch}')
        if remote.get('object', {}).get('sha') != head:
            raise ValueError('Remote sync head moved; refusing stale PR approval.')
        if attempt + 1 < attempts:
            time.sleep(2)
    raise ValueError('PR head metadata did not converge to the verified remote head.')


def prepare_owned_pr_ci(repo, number, branch, head, *, timeout=60):
    """Approve only CI for this verified synthetic-history PR, never foreign runs.

    Called only after sync_branch has proven every sync-only commit is a clean
    two-parent merge of protected main/develop history. Repository GITHUB_TOKEN
    may approve its own generated PR workflow without changing approval policy.
    """
    import time
    pull = read_owned_pull(repo, number, branch, head)
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
            approved = False
            if run.get('conclusion') == 'action_required':
                if (pull.get('user', {}).get('login') != 'github-actions[bot]'
                        or run.get('actor', {}).get('login') != 'github-actions[bot]'):
                    raise ValueError('Refusing to approve CI that was not created by the sync bot.')
                # Re-read the current PR immediately before approval; a queued
                # workflow may have appeared after the head was moved.
                current = read_owned_pull(repo, number, branch, head)
                if current.get('user', {}).get('login') != 'github-actions[bot]':
                    raise ValueError('Sync PR author changed before approval; refusing queued CI approval.')
                remote = api(f'repos/{repo}/git/ref/heads/{branch}')
                if remote.get('object', {}).get('sha') != head:
                    raise ValueError('Remote sync head moved before approval; refusing stale run approval.')
                api(f"repos/{repo}/actions/runs/{run['id']}/approve", '--method', 'POST')
                approved = True
            return PreparedCI(run['id'], approved)
        time.sleep(5)
    return None


def find_dispatched_ci(repo, branch, head, *, timeout=60):
    """Wait briefly for dispatch registration, never for CI completion."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        runs = api(f'repos/{repo}/actions/workflows/ci.yml/runs?branch={branch}&event=workflow_dispatch&head_sha={head}&per_page=20')['workflow_runs']
        for run in runs:
            if (run.get('head_sha') == head and run.get('head_branch') == branch
                    and run.get('event') == 'workflow_dispatch' and run.get('path') == '.github/workflows/ci.yml'
                    and run.get('head_repository', {}).get('full_name') == repo):
                return PreparedCI(run['id'])
        time.sleep(5)
    raise ValueError('Exact-head trusted CI dispatch did not appear; no auto-merge enabled.')


def verify_ci_launch(repo, number, branch, head, prepared):
    """Bind a real CI run to this PR/head; pending runs are intentionally valid."""
    run_id = prepared.run_id
    run = api(f'repos/{repo}/actions/runs/{run_id}')
    if (run.get('id') != run_id or run.get('head_sha') != head or run.get('head_branch') != branch
            or run.get('path') != '.github/workflows/ci.yml'
            or run.get('head_repository', {}).get('full_name') != repo
            or run.get('event') not in ('pull_request', 'workflow_dispatch')):
        raise ValueError('CI does not match the exact owned synchronization head.')
    if run['event'] == 'pull_request' and not any(
            item.get('number') == number and item.get('head', {}).get('sha') == head
            and item.get('head', {}).get('ref') == branch and item.get('base', {}).get('ref') == 'develop'
            for item in run.get('pull_requests', [])):
        raise ValueError('CI is not linked to the owned synchronization PR.')
    if run.get('status') == 'completed':
        if run.get('conclusion') == 'action_required' and prepared.approval_accepted:
            # The tightly scoped approval API may have succeeded before run
            # metadata converges. Required native checks still prevent merging.
            return
        if run.get('conclusion') != 'success':
            raise ValueError('Synchronization CI already failed; no auto-merge enabled.')
        jobs = api(f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100")['jobs']
        if not any(job.get('name') in ('ci-gate', 'verify') and job.get('conclusion') == 'success' for job in jobs):
            raise ValueError('Completed CI has no successful trusted gate; no auto-merge enabled.')


def has_trusted_required_gate(repo):
    """Classic protection must require a gate bound to the Actions app.

    GraphQL rule metadata is read-only. Inaccessible or missing evidence must
    never degrade to the less specific branch `protected` flag.
    """
    owner, name = repo.split('/')
    query = """query($owner: String!, $name: String!) {
      repository(owner: $owner, name: $name) {
        ref(qualifiedName: "refs/heads/develop") { branchProtectionRule {
          requiresStatusChecks
          requiredStatusChecks { context app { databaseId slug } }
        } }
      }
    }"""
    result = api('graphql', '-f', 'query=' + query, '-f', 'owner=' + owner, '-f', 'name=' + name)
    if result.get('errors'):
        raise ValueError('Required gate protection could not be verified.')
    rule = (((result.get('data') or {}).get('repository') or {}).get('ref') or {}).get('branchProtectionRule') or {}
    return rule.get('requiresStatusChecks') is True and any(
        check.get('context') in ('ci-gate', 'verify')
        and (check.get('app') or {}).get('databaseId') == 15368
        and (check.get('app') or {}).get('slug') == 'github-actions'
        for check in rule.get('requiredStatusChecks', []))


def merged_state(pull):
    if pull.get('state') == 'closed' and pull.get('merged') is True:
        valid_sha(pull.get('merge_commit_sha', ''))
        return 'merged'
    return None


def protected_auto_merge(repo, number, branch, head, *, main_sha):
    pull = read_owned_pull(repo, number, branch, head, allow_merged=True)
    if merged_state(pull):
        return 'merged'
    settings = api(f'repos/{repo}')
    if not settings.get('allow_auto_merge') or not settings.get('allow_merge_commit'):
        print('::notice::Sync PR requires manual merge: protected merge-commit auto-merge is not enabled.')
        return 'manual_merge_required'
    branch_metadata = api(f'repos/{repo}/branches/develop')
    if not branch_metadata.get('protected') or not has_trusted_required_gate(repo):
        print('::notice::Sync PR requires manual merge: no verified required Actions gate on develop.')
        return 'manual_merge_required'
    # Recheck immutable PR bindings and the real branch immediately before the
    # write. `--match-head-commit` also protects against races after this read.
    pull = read_owned_pull(repo, number, branch, head, allow_merged=True)
    if merged_state(pull):
        return 'merged'
    remote = api(f'repos/{repo}/git/ref/heads/{branch}')
    if remote.get('object', {}).get('sha') != head:
        raise ValueError('Remote sync head moved; refusing stale auto-merge.')
    if api(f'repos/{repo}/git/ref/heads/main')['object']['sha'] != main_sha:
        return 'superseded'
    try:
        command('gh', 'pr', 'merge', str(number), '--repo', repo, '--auto', '--merge', '--match-head-commit', head)
    except subprocess.CalledProcessError as error:
        # A previously enabled native merge may win while this command starts.
        # Accept only an actually merged PR with all original head bindings.
        if merged_state(read_owned_pull(repo, number, branch, head, allow_merged=True)):
            return 'merged'
        # GitHub refuses --auto when the PR became clean before this call.
        # Only that documented state permits a normal protected merge fallback.
        message = str(error.stderr or '') + str(error.output or '')
        if 'Pull request is in clean status' not in message:
            raise
        pull = read_owned_pull(repo, number, branch, head, allow_merged=True)
        if merged_state(pull):
            return 'merged'
        if api(f'repos/{repo}/git/ref/heads/{branch}')['object']['sha'] != head:
            raise ValueError('Remote sync head moved before protected merge.')
        if api(f'repos/{repo}/git/ref/heads/main')['object']['sha'] != main_sha:
            return 'superseded'
        if not api(f'repos/{repo}/branches/develop').get('protected') or not has_trusted_required_gate(repo):
            raise ValueError('Required protection changed before protected merge.')
        checks = api(f'repos/{repo}/commits/{head}/check-runs?per_page=100')['check_runs']
        if not any(check.get('head_sha') == head and check.get('name') in ('ci-gate', 'verify')
                and check.get('conclusion') == 'success'
                and check.get('app', {}).get('id') == 15368
                and check.get('app', {}).get('slug') == 'github-actions' for check in checks):
            raise ValueError('No exact-head successful trusted gate before protected merge.')
        command('gh', 'pr', 'merge', str(number), '--repo', repo, '--merge', '--match-head-commit', head)
    pull = read_owned_pull(repo, number, branch, head, allow_merged=True)
    return merged_state(pull) or 'awaiting_checks'


def emit_state(state, **details):
    result = {'state': state, **details}
    print(json.dumps(result, sort_keys=True))
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            for key, value in result.items():
                output.write(f'{key}={value}\n')
    return result


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
        return emit_state('not_applicable')
    try:
        api(f'repos/{repo}/git/ref/heads/develop')
        has_develop = True
    except subprocess.CalledProcessError as error:
        if '404' not in (error.stderr or ''):
            raise
        has_develop = False
    if not has_develop:
        print('Repository has no develop branch; synchronization is not applicable.')
        return emit_state('not_applicable')
    result = sync_branch(Path(args.repository), sha)
    if result is None:
        print('Main is already included in develop, or a newer main superseded this delivery.')
        current = api(f'repos/{repo}/git/ref/heads/main')['object']['sha']
        return emit_state('no_changes' if current == sha else 'superseded')
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
    details = {'pull_request_url': f'https://github.com/{repo}/pull/{number}', 'head_sha': head}
    try:
        ci_run = prepare_owned_pr_ci(repo, number, branch, head)
    except ValueError:
        # Native auto-merge enabled by an earlier run may already have finished.
        if merged_state(read_owned_pull(repo, number, branch, head, allow_merged=True)):
            return emit_state('merged', **details)
        raise
    if ci_run is None:
        command('gh', 'workflow', 'run', 'ci.yml', '--repo', repo, '--ref', branch)
        ci_run = find_dispatched_ci(repo, branch, head)
    verify_ci_launch(repo, number, branch, head, ci_run)
    details['ci_run_id'] = ci_run.run_id
    if api(f'repos/{repo}/git/ref/heads/main')['object']['sha'] != sha:
        return emit_state('superseded', **details)
    state = protected_auto_merge(repo, number, branch, head, main_sha=sha) if args.auto_merge else 'manual_merge_required'
    return emit_state(state, **details)


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
