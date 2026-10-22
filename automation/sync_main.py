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
        parents = git(repo, 'show', '-s', '--format=%P', head).split()
        if len(parents) != 2 or parents[1] != main_sha:
            raise ValueError('Existing sync branch has unexpected history; refusing to change it.')
        if subprocess.run(['git', '-C', str(repo), 'merge-base', '--is-ancestor', parents[0], 'origin/develop'],
                          capture_output=True).returncode:
            raise ValueError('Existing sync branch is not based on develop.')
        expected = git(repo, 'merge-tree', '--write-tree', *parents)
        if git(repo, 'rev-parse', head + '^{tree}') != expected:
            raise ValueError('Existing sync branch contains changes outside the history merge.')
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


def protected_auto_merge(repo, number, head):
    settings = api(f'repos/{repo}')
    if not settings.get('allow_auto_merge') or not settings.get('allow_merge_commit'):
        print('::notice::Sync PR requires manual merge: protected merge-commit auto-merge is not enabled.')
        return False
    # Fail closed for missing/inaccessible protection or ruleset-only policies.
    try:
        protection = api(f'repos/{repo}/branches/develop/protection')
    except subprocess.CalledProcessError:
        print('::notice::Sync PR requires manual merge: branch protection could not be verified.')
        return False
    checks = protection.get('required_status_checks') or {}
    if not (checks.get('contexts') or checks.get('checks')) or protection.get('required_linear_history', {}).get('enabled'):
        print('::notice::Sync PR requires manual merge: verified required checks and merge history are necessary.')
        return False
    command('gh', 'pr', 'merge', str(number), '--repo', repo, '--auto', '--merge', '--match-head-commit', head)
    return True


def run(args):
    repo = os.environ['GITHUB_REPOSITORY']
    sha = (source_sha(repo, args.workflow, args.run_id, args.receipt) if args.run_id
           else valid_sha(args.sha))
    if sha is None:
        print('Not a verified production delivery; nothing to synchronize.')
        return
    branches = api(f'repos/{repo}/branches?per_page=100')
    if not any(item['name'] == 'develop' for item in branches):
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
    command('gh', 'workflow', 'run', 'ci.yml', '--repo', repo, '--ref', branch)
    current = api(f'repos/{repo}/git/ref/heads/main')['object']['sha']
    if current == sha and args.auto_merge:
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
    run(parser.parse_args())
