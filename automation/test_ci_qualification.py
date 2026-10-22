import copy
import io
import json
import subprocess
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile

import ci_qualification as q

REPO = 'pwngames/example'
HEAD, TREE, BLOB, IMPL, BASE = ('a'*40, 'b'*40, 'c'*40, 'd'*40, 'e'*40)
IDENTITY = {'repository': REPO, 'implementation': IMPL, 'workflow': '.github/workflows/ci.yml',
            'trusted_workflows': ['.github/workflows/ci.yml'], 'profile': 'py314-node20-ubuntu24-arm64-v1',
            'context': 'isolated-public-validation-v1', 'required_artifacts': [], 'expected_jobs': ['image', 'test'], 'gate_names': {'.github/workflows/ci.yml': 'ci-gate'},
            'checkout_sha': HEAD, 'tree': TREE, 'workflow_blob': BLOB, 'workflow_blobs': {'.github/workflows/ci.yml': BLOB}}


def receipt():
    return {'schema': 1, **copy.deepcopy(IDENTITY), 'mode': 'full', 'results': {'image': 'success', 'test': 'success'}, 'run_id': 7, 'attempt': 1, 'source_event': 'push', 'run_head_sha': HEAD}


def run():
    return {'id': 7, 'run_attempt': 1, 'repository': {'full_name': REPO, 'id': 11}, 'head_repository': {'full_name': REPO, 'id': 11},
            'head_branch': 'main', 'path': '.github/workflows/ci.yml', 'status': 'completed', 'conclusion': 'success', 'event': 'push', 'head_sha': HEAD}


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
        self.files = [{"filename": "docs/guide.md", "status": "modified"}]
        self.extra_artifacts = []
        self.check = {'app': {'id': 15368}, 'conclusion': 'success', 'head_sha': HEAD}

    def get(self, endpoint, raw=False):
        if '/commits/' in endpoint and '/pulls?' in endpoint: return [{'number': 12}]
        if endpoint.endswith('/pulls/12'): return {'number': 12, 'head': {'sha': BASE, 'repo': {'full_name': REPO}}, 'base': {'repo': {'full_name': REPO}}}
        if '/compare/' in endpoint: return {'merge_base_commit': {'sha': BASE}, 'total_commits': 1, 'files': self.files}
        if '/git/commits/' in endpoint: return self.commit
        if '/contents/' in endpoint: return {'sha': self.blob}
        if endpoint.endswith('/jobs?per_page=100'): return self.jobs
        if '/check-runs/' in endpoint: return self.check
        if endpoint.endswith('/zip'): return zipped(self.receipt, 'ci-verdict.json' if self.artifact['name'].startswith('ci-verdict-') else 'ci-qualification.json')
        if endpoint.endswith('/artifacts?per_page=100'): return {'total_count': 1 + len(self.extra_artifacts), 'artifacts': [self.artifact] + self.extra_artifacts}
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

    def docs_verdict(self):
        self.api.artifact['name'] = 'ci-verdict-attempt-1'
        self.api.receipt.update(mode='docs', results={'image': 'skipped', 'test': 'skipped'}, paths=['docs/guide.md'], source_base=BASE)
        return q.verify_proof(self.api, IDENTITY, self.api.run, self.api.artifact, self.api.receipt, kind='verdict')

    def test_docs_source_verdict_is_not_full_qualification(self):
        self.assertEqual(self.docs_verdict()['run_id'], 7)
        with self.assertRaises(ValueError): self.verify()

    def test_docs_verdict_rejects_actual_runtime_or_rename_changes(self):
        for files in [[{'filename': 'src/app.py', 'status': 'modified'}], [{'filename': 'docs/guide.md', 'previous_filename': 'src/app.py', 'status': 'renamed'}]]:
            self.api.files = files
            with self.assertRaises(ValueError): self.docs_verdict()

    def test_docs_verdict_cannot_use_unrelated_base(self):
        with patch.object(self.api, 'get', side_effect=lambda endpoint, **kwargs: {'merge_base_commit': {'sha': IMPL}, 'total_commits': 1, 'files': self.api.files} if '/compare/' in endpoint else FakeAPI.get(self.api, endpoint, **kwargs)):
            with self.assertRaises(ValueError): self.docs_verdict()

    def test_docs_verdict_rejects_truncated_source_diff(self):
        self.api.files = [{'filename': 'docs/guide.md'}] * 300
        with self.assertRaises(ValueError): self.docs_verdict()

    def test_required_build_outputs_missing_or_expired_force_full(self):
        identity = {**IDENTITY, 'required_artifacts': ['site-out']}
        self.api.receipt.update(required_artifacts=['site-out'])
        self.assertEqual(q.plan(self.api, identity, ['src/x'])['mode'], 'full')
        output = copy.deepcopy(self.api.artifact); output.update(id=10, name='site-out')
        self.api.extra_artifacts = [output]
        self.assertEqual(q.plan(self.api, identity, ['src/x'])['mode'], 'reuse')
        output['expired'] = True
        self.assertEqual(q.plan(self.api, identity, ['src/x'])['mode'], 'full')

    def test_record_cli_emits_verdict_all_modes_but_qualification_only_full(self):
        for mode in ('full', 'docs', 'reuse'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
                root = Path(directory); event = root/'event.json'; event.write_text(json.dumps({'before': BASE}))
                selected = q.plan(self.api, IDENTITY, ['docs/guide.md'] if mode == 'docs' else ['src/x'])
                selected['mode'] = mode
                statuses = {'plan': {'result': 'success'}, 'image': {'result': 'success' if mode == 'full' else 'skipped'}, 'test': {'result': 'success' if mode == 'full' else 'skipped'}}
                env = {'CI_PLAN': json.dumps(selected), 'CI_RESULTS': json.dumps(statuses), 'GITHUB_RUN_ID': '99', 'GITHUB_RUN_ATTEMPT': '1', 'RUNNER_TEMP': directory, 'GITHUB_OUTPUT': str(root/'outputs'), 'GITHUB_EVENT_NAME': 'push', 'GITHUB_EVENT_PATH': str(event), 'CI_RECORD_RUNTIME_NOOP': 'true', 'GITHUB_REF_NAME': 'main', 'RUNNER_TEMP': directory, 'GITHUB_RUN_ID': '99'}
                with patch.dict(q.os.environ, env), patch.object(q, 'inputs', return_value=IDENTITY), patch.object(q, 'local_identity', return_value=IDENTITY), patch.object(q, 'changed_paths', return_value=['docs/guide.md'] if mode == 'docs' else ['src/x']), patch.object(q, 'GitHub', return_value=self.api), patch.object(q.sys, 'argv', ['ci_qualification.py', 'record']):
                    q.main()
                self.assertTrue((root/'ci-verdict.json').is_file())
                self.assertEqual((root/'ci-qualification.json').is_file(), mode == 'full')
                self.assertEqual((root/'production-sync-receipt.json').is_file(), mode == 'docs')
                if mode == 'docs':
                    noop = json.loads((root/'production-sync-receipt.json').read_text())
                    self.assertEqual(noop['outcome'], 'no_runtime_change')
                    self.assertEqual(noop['sha'], HEAD)
                verdict = json.loads((root/'ci-verdict.json').read_text())
                self.assertEqual(verdict['mode'], mode)
                self.assertEqual(verdict['source_base'], BASE)

    def test_downstream_verdict_cli_returns_no_runtime_only_for_verified_docs(self):
        self.docs_verdict()
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            output = Path(directory)/'outputs'
            env = {'CI_SOURCE_RUN': '7', 'CI_SOURCE_SHA': HEAD, 'GITHUB_OUTPUT': str(output), 'GITHUB_EVENT_NAME': 'workflow_dispatch'}
            with patch.dict(q.os.environ, env), patch.object(q, 'inputs', return_value=IDENTITY), patch.object(q, 'local_identity', return_value=IDENTITY), patch.object(q, 'changed_paths', return_value=None), patch.object(q, 'GitHub', return_value=self.api), patch.object(q.sys, 'argv', ['ci_qualification.py', 'verdict']):
                q.main()
            self.assertIn('mode=docs', output.read_text())
            self.assertIn('outcome=no_runtime_change', output.read_text())
            self.api.run['head_sha'] = BASE
            with patch.dict(q.os.environ, env), patch.object(q, 'inputs', return_value=IDENTITY), patch.object(q, 'local_identity', return_value=IDENTITY), patch.object(q, 'changed_paths', return_value=None), patch.object(q, 'GitHub', return_value=self.api), patch.object(q.sys, 'argv', ['ci_qualification.py', 'verdict']):
                with self.assertRaises(ValueError): q.main()

    def test_workflow_run_verdict_is_bound_to_exact_trigger(self):
        self.docs_verdict()
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory); event = root/'event.json'; output = root/'outputs'
            trigger = {'workflow_run': {'id': 7, 'head_sha': HEAD, 'head_repository': {'full_name': REPO}}}
            event.write_text(json.dumps(trigger))
            env = {'CI_SOURCE_RUN': '7', 'CI_SOURCE_SHA': HEAD, 'GITHUB_OUTPUT': str(output), 'GITHUB_EVENT_NAME': 'workflow_run', 'GITHUB_EVENT_PATH': str(event), 'CI_RECORD_RUNTIME_NOOP': 'true', 'GITHUB_REF_NAME': 'main', 'RUNNER_TEMP': directory, 'GITHUB_RUN_ID': '99'}
            def invoke():
                with patch.dict(q.os.environ, env), patch.object(q, 'inputs', return_value=IDENTITY), patch.object(q, 'local_identity', return_value=IDENTITY), patch.object(q, 'changed_paths', return_value=None), patch.object(q, 'GitHub', return_value=self.api), patch.object(q.sys, 'argv', ['ci_qualification.py', 'verdict']):
                    q.main()
            invoke()
            trigger['workflow_run']['id'] = 99; event.write_text(json.dumps(trigger))
            with self.assertRaises(ValueError): invoke()
            trigger['workflow_run']['id'] = 7
            trigger['workflow_run']['head_repository']['full_name'] = 'foreign/repo'; event.write_text(json.dumps(trigger))
            with self.assertRaises(ValueError): invoke()

    def test_runtime_noop_receipts_distinguish_environment_and_never_claim_deployed(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            env = {'GITHUB_RUN_ID': '99', 'RUNNER_TEMP': directory}
            for branch in ('main', 'develop'):
                outputs = q.write_runtime_noop(IDENTITY, 7, 1, branch, env)
                payload = json.loads(Path(outputs['noop_file']).read_text())
                self.assertEqual(payload['run_id'], '99')
                self.assertEqual(payload['source_ci_run'], '7')
                self.assertEqual(payload['environment'], 'production' if branch == 'main' else 'development')
                self.assertEqual(payload['outcome'], 'no_runtime_change')
                self.assertNotIn('deployed', payload)
            with self.assertRaises(ValueError): q.write_runtime_noop(IDENTITY, 7, 1, 'feature', env)
            with self.assertRaises(ValueError): q.write_runtime_noop(IDENTITY, True, 1, 'main', env)

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
