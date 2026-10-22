import copy
import io
import json
import subprocess
import unittest
from unittest.mock import patch
import zipfile

import ci_qualification as q

REPO = 'pwngames/example'
HEAD, TREE, BLOB, IMPL, BASE = ('a'*40, 'b'*40, 'c'*40, 'd'*40, 'e'*40)
IDENTITY = {'repository': REPO, 'implementation': IMPL, 'workflow': '.github/workflows/ci.yml',
            'trusted_workflows': ['.github/workflows/ci.yml'], 'profile': 'py314-node20-ubuntu24-arm64-v1',
            'context': 'isolated-public-validation-v1', 'expected_jobs': ['image', 'test'], 'gate_names': {'.github/workflows/ci.yml': 'ci-gate'},
            'checkout_sha': HEAD, 'tree': TREE, 'workflow_blob': BLOB, 'workflow_blobs': {'.github/workflows/ci.yml': BLOB}}


def receipt():
    return {'schema': 1, **copy.deepcopy(IDENTITY), 'mode': 'full', 'results': {'image': 'success', 'test': 'success'}, 'run_id': 7, 'attempt': 1, 'source_event': 'push', 'run_head_sha': HEAD}


def run():
    return {'id': 7, 'run_attempt': 1, 'repository': {'full_name': REPO, 'id': 11}, 'head_repository': {'full_name': REPO, 'id': 11},
            'path': '.github/workflows/ci.yml', 'status': 'completed', 'conclusion': 'success', 'event': 'push', 'head_sha': HEAD}


def artifact():
    return {'id': 8, 'name': q.artifact_name(IDENTITY, 1), 'expired': False, 'size_in_bytes': 100,
            'workflow_run': {'id': 7, 'head_sha': HEAD, 'repository_id': 11, 'head_repository_id': 11}}


def zipped(value, filename='ci-qualification.json'):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as archive:
        archive.writestr(filename, json.dumps(value))
    return buf.getvalue()


class FakeAPI:
    def __init__(self):
        self.run = run()
        self.artifact = artifact()
        self.receipt = receipt()
        self.commit = {'sha': HEAD, 'tree': {'sha': TREE}, 'parents': [{'sha': BASE}]}
        self.blob = BLOB
        self.jobs = {'total_count': 1, 'jobs': [{'name': 'ci-gate', 'conclusion': 'success', 'check_run_url': f'https://api.github.com/repos/{REPO}/check-runs/9'}]}
        self.check = {'app': {'id': 15368}, 'conclusion': 'success', 'head_sha': HEAD}

    def get(self, endpoint, raw=False):
        if '/commits/' in endpoint and '/pulls?' in endpoint: return [{'number': 12}]
        if endpoint.endswith('/pulls/12'): return {'number': 12, 'head': {'sha': BASE, 'repo': {'full_name': REPO}}, 'base': {'repo': {'full_name': REPO}}}
        if '/git/commits/' in endpoint: return self.commit
        if '/contents/' in endpoint: return {'sha': self.blob}
        if endpoint.endswith('/jobs?per_page=100'): return self.jobs
        if '/check-runs/' in endpoint: return self.check
        if endpoint.endswith('/zip'): return zipped(self.receipt)
        if endpoint.endswith('/artifacts?per_page=100'): return {'total_count': 1, 'artifacts': [self.artifact]}
        if endpoint.endswith('/runs?status=success&per_page=10'): return {'workflow_runs': [self.run]}
        if endpoint.endswith('/actions/runs/7'): return self.run
        if endpoint.endswith('/actions/artifacts/8'): return self.artifact
        raise AssertionError(endpoint)


class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()

    def verify(self):
        return q.verify_proof(self.api, IDENTITY, self.api.run, self.api.artifact, self.api.receipt)

    def test_same_tree_different_commit_can_reuse(self):
        identity = {**IDENTITY, 'checkout_sha': IMPL}
        self.assertEqual(q.verify_proof(self.api, identity, self.api.run, self.api.artifact, self.api.receipt)['checkout_sha'], HEAD)

    def test_reusable_caller_gate_names_share_canonical_identity(self):
        identity = copy.deepcopy(IDENTITY)
        caller = '.github/workflows/deploy.yml'
        identity['trusted_workflows'].append(caller)
        identity['workflow_blobs'][caller] = BLOB
        identity['gate_names'][caller] = 'ci / ci-gate'
        self.api.receipt.update(copy.deepcopy(identity))
        self.api.run['path'] = caller
        self.api.jobs['jobs'][0]['name'] = 'ci / ci-gate'
        self.assertEqual(q.verify_proof(self.api, identity, self.api.run, self.api.artifact, self.api.receipt)['run_id'], 7)
        self.api.jobs['jobs'][0]['name'] = 'ci-gate'
        with self.assertRaises(ValueError): q.verify_proof(self.api, identity, self.api.run, self.api.artifact, self.api.receipt)

    def test_dirty_tracked_checkout_rejected(self):
        with patch.object(q, 'git', return_value=' M app.py'):
            with self.assertRaises(ValueError): q.local_identity(IDENTITY, {'GITHUB_SHA': HEAD})

    def test_untracked_files_are_not_part_of_tracked_dirty_check(self):
        def clean_git(*args):
            if args[0] == 'status':
                self.assertIn('--untracked-files=no', args)
                return ''
            return HEAD if args[1] == 'HEAD' else TREE if args[1] == 'HEAD^{tree}' else BLOB
        with patch.object(q, 'git', side_effect=clean_git):
            self.assertEqual(q.local_identity(IDENTITY, {'GITHUB_SHA': HEAD})['tree'], TREE)

    def test_full_successful_exact_tree_can_be_reused(self):
        self.assertEqual(q.plan(self.api, IDENTITY, ['src/app.py'])['mode'], 'reuse')

    def test_docs_are_narrow(self):
        self.assertTrue(q.docs_only(['README.md', 'README-fr.md', 'docs/operations/ci.md']))
        for paths in ([], ['src/readme.md'], ['public/help.md'], ['.github/workflows/ci.yml'], ['README.rst'], ['docs/../src/x'], ['/docs/x']):
            self.assertFalse(q.docs_only(paths), paths)

    def test_docs_do_not_query_api(self):
        self.assertEqual(q.plan(None, IDENTITY, ['docs/guide.md'])['mode'], 'docs')

    def test_unknown_changes_require_full(self):
        self.assertEqual(q.plan(None, IDENTITY, None)['mode'], 'full')

    def test_qualified_identical_policy_tree_can_reuse(self):
        for name in ['.github/workflows/ci.yml', 'next.config.ts', 'web/tsconfig.json', 'package.json', '.nvmrc', 'Dockerfile']:
            self.assertEqual(q.plan(self.api, IDENTITY, [name])['mode'], 'reuse', name)

    def test_new_policy_without_exact_proof_runs_full(self):
        self.api.artifact['name'] = 'legacy-coverage'
        self.assertEqual(q.plan(self.api, IDENTITY, ['.github/workflows/ci.yml'])['mode'], 'full')

    def test_malformed_api_listing_requires_full(self):
        with patch.object(self.api, 'get', return_value=None):
            self.assertEqual(q.plan(self.api, IDENTITY, ['src/x'])['mode'], 'full')

    def test_reuse_record_revalidates_exact_configuration_tree(self):
        selected = q.plan(self.api, IDENTITY, ['src/x'])
        outcomes = {'plan': {'result': 'success'}, 'image': {'result': 'skipped'}, 'test': {'result': 'skipped'}}
        self.assertIsNone(q.record(self.api, IDENTITY, selected, outcomes, ['.github/workflows/ci.yml']))
        self.api.blob = BASE
        with self.assertRaises(ValueError): q.record(self.api, IDENTITY, selected, outcomes, ['.github/workflows/ci.yml'])

    def test_missing_legacy_receipt_requires_full(self):
        self.api.artifact['name'] = 'coverage'
        self.assertEqual(q.plan(self.api, IDENTITY, ['src/app.py'])['mode'], 'full')

    def test_api_failure_requires_full(self):
        with patch.object(self.api, 'get', side_effect=subprocess.CalledProcessError(1, 'gh')):
            self.assertEqual(q.plan(self.api, IDENTITY, ['src/app.py'])['mode'], 'full')

    def test_foreign_repository_rejected(self):
        self.api.run['repository']['full_name'] = 'foreign/repo'
        with self.assertRaises(ValueError): self.verify()

    def test_fork_head_rejected(self):
        self.api.run['head_repository']['full_name'] = 'foreign/repo'
        with self.assertRaises(ValueError): self.verify()

    def test_failed_or_uncompleted_run_rejected(self):
        for key, value in [('conclusion', 'failure'), ('status', 'in_progress'), ('event', 'workflow_run'), ('path', '.github/workflows/untrusted.yml')]:
            with self.subTest(key=key):
                self.api = FakeAPI(); self.api.run[key] = value
                with self.assertRaises(ValueError): self.verify()

    def test_changed_identity_rejected(self):
        for key in ('profile', 'context', 'tree', 'workflow_blob', 'implementation'):
            with self.subTest(key=key):
                self.api = FakeAPI(); self.api.receipt[key] = 'foreign'
                with self.assertRaises(ValueError): self.verify()

    def test_docs_and_reuse_receipts_cannot_chain(self):
        for mode in ('docs', 'reuse'):
            self.api.receipt['mode'] = mode
            with self.assertRaises(ValueError): self.verify()

    def test_foreign_artifact_run_or_head_rejected(self):
        for key, value in [('id', 99), ('head_sha', BASE), ('repository_id', 99), ('head_repository_id', 99)]:
            with self.subTest(key=key):
                self.api = FakeAPI(); self.api.artifact['workflow_run'][key] = value
                with self.assertRaises(ValueError): self.verify()

    def test_expired_or_oversize_artifact_rejected(self):
        for key, value in [('expired', True), ('size_in_bytes', q.MAX_ARTIFACT+1), ('name', 'foreign')]:
            self.api = FakeAPI(); self.api.artifact[key] = value
            with self.assertRaises(ValueError): self.verify()

    def test_future_or_wrong_attempt_rejected(self):
        for value in (0, 2, True, '1'):
            self.api.receipt['attempt'] = value
            with self.assertRaises(ValueError): self.verify()

    def test_prior_full_attempt_can_qualify_successful_same_run(self):
        self.api.run['run_attempt'] = 2
        self.assertEqual(self.verify()['attempt'], 1)

    def test_receipt_run_id_must_match(self):
        self.api.receipt['run_id'] = 999
        with self.assertRaises(ValueError): self.verify()

    def test_source_tree_and_commit_bound(self):
        for key in ('sha', 'tree'):
            self.api = FakeAPI(); self.api.commit[key] = {'sha': BASE} if key == 'tree' else BASE
            with self.assertRaises(ValueError): self.verify()

    def test_source_workflow_blob_must_match(self):
        self.api.blob = BASE
        with self.assertRaises(ValueError): self.verify()

    def test_failed_missing_duplicated_or_truncated_gate_rejected(self):
        for jobs in [[], self.api.jobs['jobs']*2, [{'name': 'ci-gate', 'conclusion': 'failure'}]]:
            self.api = FakeAPI(); self.api.jobs['jobs'] = jobs
            with self.assertRaises(ValueError): self.verify()
        self.api = FakeAPI(); self.api.jobs['total_count'] = 101
        with self.assertRaises(ValueError): self.verify()

    def test_foreign_app_check_head_or_failed_check_rejected(self):
        for key, value in [('app', {'id': 1}), ('head_sha', BASE), ('conclusion', 'failure')]:
            self.api = FakeAPI(); self.api.check[key] = value
            with self.assertRaises(ValueError): self.verify()

    def test_untrusted_check_url_rejected(self):
        self.api.jobs['jobs'][0]['check_run_url'] = 'https://evil.example/check-runs/9'
        with self.assertRaises(ValueError): self.verify()

    def test_pr_synthetic_merge_tree_bound_to_actual_parents(self):
        self.api.run.update(event='pull_request', head_sha=BASE, pull_requests=[{'number': 12, 'head': {'sha': BASE}, 'base': {'sha': BLOB}}])
        self.api.receipt.update(source_event='pull_request', run_head_sha=BASE, source_pr={'number': 12, 'head': BASE, 'base': BLOB})
        self.api.artifact['workflow_run']['head_sha'] = BASE
        self.api.commit['parents'] = [{'sha': BLOB}, {'sha': BASE}]
        self.assertEqual(self.verify()['checkout_sha'], HEAD)
        self.api.commit['parents'][0]['sha'] = IMPL
        with self.assertRaises(ValueError): self.verify()

    def test_merged_pr_missing_run_link_uses_exact_head_association(self):
        self.api.run.update(event='pull_request', head_sha=BASE, pull_requests=[])
        self.api.receipt.update(source_event='pull_request', run_head_sha=BASE, source_pr={'number': 12, 'head': BASE, 'base': BLOB})
        self.api.artifact['workflow_run']['head_sha'] = BASE
        self.api.commit['parents'] = [{'sha': BLOB}, {'sha': BASE}]
        self.assertEqual(self.verify()['checkout_sha'], HEAD)
        with patch.object(self.api, 'get', side_effect=lambda endpoint, **kwargs: [] if '/commits/' in endpoint and '/pulls?' in endpoint else FakeAPI.get(self.api, endpoint, **kwargs)):
            with self.assertRaises(ValueError): self.verify()

    def test_head_only_pr_checkout_cannot_qualify_merge_tree(self):
        self.api.run.update(event='pull_request', pull_requests=[])
        with self.assertRaises(ValueError): self.verify()

    def test_invalid_receipt_archives_rejected(self):
        for payload in (b'invalid', zipped({}, '../ci-qualification.json'), zipped({'huge': 'a'*q.MAX_RECEIPT})):
            with self.assertRaises((ValueError, zipfile.BadZipFile)): q.read_receipt(payload)

    def test_full_record_requires_every_expected_job_success(self):
        selected = {'schema': 1, 'identity': IDENTITY, 'mode': 'full'}
        good = {'plan': {'result': 'success'}, 'image': {'result': 'success'}, 'test': {'result': 'success'}}
        self.assertEqual(q.record(self.api, IDENTITY, selected, good, ['src/x'])['mode'], 'full')
        for value in ('skipped', 'failure', 'cancelled'):
            bad = copy.deepcopy(good); bad['test']['result'] = value
            with self.assertRaises(ValueError): q.record(self.api, IDENTITY, selected, bad, ['src/x'])
        bad = copy.deepcopy(good); bad['extra'] = {'result': 'success'}
        with self.assertRaises(ValueError): q.record(self.api, IDENTITY, selected, bad, ['src/x'])

    def test_docs_gate_requires_expected_skips_and_rechecks_paths(self):
        selected = q.plan(None, IDENTITY, ['docs/guide.md'])
        outcomes = {'plan': {'result': 'success'}, 'image': {'result': 'skipped'}, 'test': {'result': 'skipped'}}
        self.assertIsNone(q.record(self.api, IDENTITY, selected, outcomes, ['docs/guide.md']))
        with self.assertRaises(ValueError): q.record(self.api, IDENTITY, selected, outcomes, ['src/x'])
        outcomes['test']['result'] = 'success'
        with self.assertRaises(ValueError): q.record(self.api, IDENTITY, selected, outcomes, ['docs/guide.md'])

    def test_reuse_gate_revalidates_proof_and_never_emits_receipt(self):
        selected = q.plan(self.api, IDENTITY, ['src/x'])
        outcomes = {'plan': {'result': 'success'}, 'image': {'result': 'skipped'}, 'test': {'result': 'skipped'}}
        self.assertIsNone(q.record(self.api, IDENTITY, selected, outcomes, ['src/x']))
        self.api.run['conclusion'] = 'failure'
        with self.assertRaises(ValueError): q.record(self.api, IDENTITY, selected, outcomes, ['src/x'])


if __name__ == '__main__':
    unittest.main()
