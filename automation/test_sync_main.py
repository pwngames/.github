import argparse
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import sync_main as sync


class PolicyTests(unittest.TestCase):
    def test_receipt_requires_exact_production_run(self):
        proof = dict(repository='pwngames/app', run_id='12', branch='main', environment='production', success=True, sha='a'*40)
        self.assertEqual(sync.receipt_sha(proof,'pwngames/app','12'),'a'*40)
        for key, value in [('repository','fork/app'),('run_id','13'),('branch','develop'),('environment','development'),('success','true'),('sha','abc')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                sync.receipt_sha({**proof,key:value},'pwngames/app','12')

    def test_only_trusted_successful_configured_workflow_is_accepted(self):
        run = dict(conclusion='success',path='.github/workflows/ci.yml',head_repository={'full_name':'pwngames/app'},head_branch='main',event='push',head_sha='a'*40)
        for change in [{'conclusion':'failure'},{'path':'other'},{'head_repository':{'full_name':'fork/app'}}]:
            with patch.object(sync,'api',return_value={**run,**change}), self.assertRaises(ValueError):
                sync.source_sha('pwngames/app','ci.yml','12')
        with patch.object(sync,'api',return_value={**run,'head_branch':'develop'}):
            self.assertIsNone(sync.source_sha('pwngames/app','ci.yml','12'))

    def test_nested_development_delivery_without_receipt_is_ineligible(self):
        run=dict(conclusion='success',path='.github/workflows/deliver.yml',head_repository={'full_name':'pwngames/app'})
        with patch.object(sync,'api',side_effect=[run,{'artifacts':[]}]):
            self.assertIsNone(sync.source_sha('pwngames/app','deliver.yml','12',receipt=True))

    def test_duplicate_pr_reused_and_ci_explicitly_triggered(self):
        args=argparse.Namespace(find_production_run=False,run_id='',sha='a'*40,repository='unused',auto_merge=False)
        with patch.dict(os.environ,{'GITHUB_REPOSITORY':'pwngames/app'}),patch.object(sync,'api',side_effect=[{'ref':'refs/heads/develop'},[{'number':17}],{'object':{'sha':'a'*40}}]) as api,patch.object(sync,'sync_branch',return_value=('sync/main-to-develop-aaaaaaaaaaaa','b'*40)),patch.object(sync,'command') as command,patch.object(sync,'prepare_owned_pr_ci',return_value=None),patch.object(sync,'find_dispatched_ci',return_value=sync.PreparedCI(22)),patch.object(sync,'verify_ci_launch'):
            sync.run(args)
            self.assertEqual(api.call_count,3)
            command.assert_called_once_with('gh','workflow','run','ci.yml','--repo','pwngames/app','--ref','sync/main-to-develop-aaaaaaaaaaaa')

    def test_no_develop_branch_is_not_created(self):
        args=argparse.Namespace(find_production_run=False,run_id='',sha='a'*40,repository='unused',auto_merge=False)
        with patch.dict(os.environ,{'GITHUB_REPOSITORY':'pwngames/app'}),patch.object(sync,'api',side_effect=subprocess.CalledProcessError(1,['gh'],stderr='HTTP 404')),patch.object(sync,'sync_branch') as branch:
            sync.run(args)
            branch.assert_not_called()


class HistoryTests(unittest.TestCase):
    def setUp(self):
        scratch=Path(__file__).resolve().parents[1]/'temp';scratch.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=scratch);self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.remote=self.root/'origin.git';self.work=self.root/'work'
        subprocess.run(['git','init','--bare',str(self.remote)],capture_output=True,check=True)
        subprocess.run(['git','clone',str(self.remote),str(self.work)],capture_output=True,check=True)
        sync.git(self.work,'config','user.name','test');sync.git(self.work,'config','user.email','test@example.com')
        sync.git(self.work,'checkout','-b','main');self.commit('base','base');sync.git(self.work,'push','-u','origin','main')
        sync.git(self.work,'checkout','-b','develop');self.commit('feature','feature');sync.git(self.work,'push','-u','origin','develop')
        sync.git(self.work,'checkout','main');self.commit('release','release');sync.git(self.work,'push','origin','main')
        self.main=sync.git(self.work,'rev-parse','HEAD');self.develop=sync.git(self.work,'rev-parse','origin/develop')

    def commit(self,name,value):
        (self.work/name).write_text(value);sync.git(self.work,'add',name);sync.git(self.work,'commit','-m','fixture')

    def test_only_new_sync_branch_pushed_and_merge_history_preserved(self):
        branch,head=sync.sync_branch(self.work,self.main)
        self.assertEqual(sync.git(self.work,'show','-s','--format=%P',head).split(),[self.develop,self.main])
        self.assertEqual((self.work/'feature').read_text(),'feature');self.assertEqual((self.work/'release').read_text(),'release')
        for name,sha in [('main',self.main),('develop',self.develop)]:
            self.assertEqual(sync.git(self.work,'ls-remote','origin','refs/heads/'+name).split()[0],sha)
        self.assertEqual(sync.sync_branch(self.work,self.main),(branch,head))

    def test_stale_main_does_not_push(self):
        self.assertIsNone(sync.sync_branch(self.work,'a'*40))
        self.assertEqual(sync.git(self.work,'ls-remote','--heads','origin','refs/heads/sync/*'),'')

    def test_conflict_fails_without_pushing(self):
        sync.git(self.work,'checkout','develop');self.commit('base','develop');sync.git(self.work,'push','origin','develop')
        sync.git(self.work,'checkout','main');self.commit('base','main');sync.git(self.work,'push','origin','main')
        with self.assertRaisesRegex(ValueError,'conflict'):
            sync.sync_branch(self.work,sync.git(self.work,'rev-parse','HEAD'))
        self.assertEqual(sync.git(self.work,'ls-remote','--heads','origin','refs/heads/sync/*'),'')


class AdditionalSafetyTests(HistoryTests):
    def test_existing_branch_with_manual_tree_change_is_rejected(self):
        branch, head = sync.sync_branch(self.work, self.main)
        (self.work/'surprise').write_text('unrelated change')
        sync.git(self.work,'add','surprise')
        sync.git(self.work,'commit','--amend','--no-edit')
        # Fixture models someone modifying the automation-owned branch. The
        # implementation itself never force pushes or repairs this condition.
        sync.git(self.work,'push','--force','origin',branch)
        with self.assertRaisesRegex(ValueError,'outside the history merge'):
            sync.sync_branch(self.work,self.main)

    def test_develop_advancement_refreshes_same_branch_without_losing_work(self):
        branch, old = sync.sync_branch(self.work,self.main)
        sync.git(self.work,'checkout','develop')
        self.commit('later-feature','keep this')
        sync.git(self.work,'push','origin','develop')
        name, current = sync.sync_branch(self.work,self.main)
        self.assertEqual(name,branch)
        self.assertNotEqual(current,old)
        self.assertEqual((self.work/'later-feature').read_text(),'keep this')
        self.assertEqual(sync.sync_branch(self.work,self.main),(branch,current))


class NativeQueueTests(unittest.TestCase):
    repo = 'pwngames/app'
    branch = 'sync/main-to-develop-aaaaaaaaaaaa'
    head = 'a'*40
    main = 'c'*40

    def pull(self, **changes):
        return dict(state='open', merged=False, head={'sha': self.head, 'ref': self.branch,
                    'repo': {'full_name': self.repo}}, base={'ref': 'develop'}, **changes)

    def rule(self, **changes):
        return {'data': {'repository': {'ref': {'branchProtectionRule': {
            'requiresStatusChecks': True, 'requiredStatusChecks': [dict(context='ci-gate',
            app={'databaseId': 15368, 'slug': 'github-actions'}, **changes)]}}}}}

    def responses(self, rule=None, protected=True):
        return [{'allow_auto_merge': True, 'allow_merge_commit': True}, {'protected': protected},
                self.rule() if rule is None else rule, {'object': {'sha': self.head}},
                {'object': {'sha': self.main}}]

    def queue(self):
        return sync.protected_auto_merge(self.repo, 7, self.branch, self.head, main_sha=self.main)

    def test_pending_checks_queue_native_auto_merge_without_waiting(self):
        with patch.object(sync, 'read_owned_pull', return_value=self.pull()), patch.object(sync, 'api', side_effect=self.responses()), patch.object(sync, 'command') as command, patch('time.sleep') as sleep:
            self.assertEqual(self.queue(), 'awaiting_checks')
            command.assert_called_once_with('gh', 'pr', 'merge', '7', '--repo', self.repo,
                    '--auto', '--merge', '--match-head-commit', self.head)
            sleep.assert_not_called()

    def test_clean_status_uses_only_protected_exact_head_merge(self):
        failure = subprocess.CalledProcessError(1, ['gh'], stderr='GraphQL: Pull request is in clean status (enablePullRequestAutoMerge)')
        merged = {**self.pull(), 'state': 'closed', 'merged': True, 'merge_commit_sha': 'f'*40}
        checks = {'check_runs': [{'head_sha': self.head, 'name': 'ci-gate', 'conclusion': 'success', 'app': {'id': 15368, 'slug': 'github-actions'}}]}
        responses = self.responses() + [{'object': {'sha': self.head}}, {'object': {'sha': self.main}}, {'protected': True}, self.rule(), checks]
        with patch.object(sync, 'read_owned_pull', side_effect=[self.pull()]*4+[merged]), patch.object(sync, 'api', side_effect=responses), patch.object(sync, 'command', side_effect=[failure, '']) as command:
            self.assertEqual(self.queue(), 'merged')
            self.assertEqual(command.call_count, 2)
            self.assertNotIn('--auto', command.call_args.args)
            self.assertIn('--match-head-commit', command.call_args.args)

    def test_other_merge_errors_never_fall_back(self):
        failure = subprocess.CalledProcessError(1, ['gh'], stderr='HTTP 403 forbidden')
        with patch.object(sync, 'read_owned_pull', return_value=self.pull()), patch.object(sync, 'api', side_effect=self.responses()), patch.object(sync, 'command', side_effect=failure) as command:
            with self.assertRaises(subprocess.CalledProcessError): self.queue()
            self.assertEqual(command.call_count, 1)

    def test_clean_status_without_trusted_green_gate_never_merges(self):
        for checks in [[], [{'head_sha': self.head, 'name': 'ci-gate', 'conclusion': 'success', 'app': {'id': 1, 'slug': 'foreign'}}]]:
            failure = subprocess.CalledProcessError(1, ['gh'], stderr='Pull request is in clean status')
            responses = self.responses()+[{'object': {'sha': self.head}}, {'object': {'sha': self.main}}, {'protected': True}, self.rule(), {'check_runs': checks}]
            with patch.object(sync, 'read_owned_pull', return_value=self.pull()), patch.object(sync, 'api', side_effect=responses), patch.object(sync, 'command', side_effect=failure) as command:
                with self.assertRaisesRegex(ValueError, 'successful trusted gate'): self.queue()
                self.assertEqual(command.call_count, 1)

    def test_no_auto_merge_without_native_settings_or_protected_branch(self):
        for responses in [[{'allow_auto_merge': False}],
                          [{'allow_auto_merge': True, 'allow_merge_commit': True}, {'protected': False}]]:
            with patch.object(sync, 'read_owned_pull', return_value=self.pull()), patch.object(sync, 'api', side_effect=responses), patch.object(sync, 'command') as command:
                self.assertEqual(self.queue(), 'manual_merge_required')
                command.assert_not_called()

    def test_missing_disabled_or_wrong_app_gate_never_enables_auto_merge(self):
        disabled = self.rule(); disabled['data']['repository']['ref']['branchProtectionRule']['requiresStatusChecks'] = False
        rules = [{}, {'data': {'repository': {'ref': None}}}, disabled]
        for check in [{'context': 'unrelated', 'app': {'databaseId': 15368, 'slug': 'github-actions'}},
                      {'context': 'ci-gate', 'app': None},
                      {'context': 'ci-gate', 'app': {'databaseId': 1, 'slug': 'github-actions'}},
                      {'context': 'ci-gate', 'app': {'databaseId': 15368, 'slug': 'foreign'}}]:
            rule = self.rule(); rule['data']['repository']['ref']['branchProtectionRule']['requiredStatusChecks'] = [check]; rules.append(rule)
        for rule in rules:
            with self.subTest(rule=rule), patch.object(sync, 'read_owned_pull', return_value=self.pull()), patch.object(sync, 'api', side_effect=self.responses(rule)), patch.object(sync, 'command') as command:
                self.assertEqual(self.queue(), 'manual_merge_required'); command.assert_not_called()

    def test_inaccessible_gate_rule_fails_closed(self):
        for failure in [subprocess.CalledProcessError(1, ['gh'], stderr='HTTP 403'), {'errors': [{'message': 'forbidden'}]}]:
            with patch.object(sync, 'read_owned_pull', return_value=self.pull()), patch.object(sync, 'api', side_effect=[self.responses()[0], self.responses()[1], failure]), patch.object(sync, 'command') as command:
                self.assertEqual(self.queue(), 'manual_merge_required')
                command.assert_not_called()

    def test_public_branch_payload_requires_explicit_everyone_app_bound_gate(self):
        import copy
        good={'protected':True,'protection':{'enabled':True,'required_status_checks':{
            'enforcement_level':'everyone','checks':[{'app_id':15368,'context':'ci-gate'}], 'contexts':['ci-gate']}}}
        with patch.object(sync,'api') as api:
            self.assertTrue(sync.has_trusted_required_gate(self.repo, good));api.assert_not_called()
        mutations=[lambda p:p['protection'].update(enabled=False),
                   lambda p:p['protection'].pop('enabled'),
                   lambda p:p['protection']['required_status_checks'].update(enforcement_level='non_admins'),
                   lambda p:p['protection']['required_status_checks'].pop('checks'),
                   lambda p:p['protection']['required_status_checks']['checks'][0].pop('app_id'),
                   lambda p:p['protection']['required_status_checks']['checks'][0].update(app_id=1),
                   lambda p:p['protection']['required_status_checks']['checks'][0].update(context='foreign')]
        for mutation in mutations:
            value=copy.deepcopy(good);mutation(value)
            with patch.object(sync,'api',return_value={}) as api:
                self.assertFalse(sync.has_trusted_required_gate(self.repo,value));api.assert_called_once()

    def test_branch_proof_queues_without_graphql_permission(self):
        branch={'protected':True,'protection':{'enabled':True,'required_status_checks':{
            'enforcement_level':'everyone','checks':[{'app_id':15368,'context':'verify'}]}}}
        responses=[self.responses()[0],branch,{'object':{'sha':self.head}},{'object':{'sha':self.main}}]
        with patch.object(sync,'read_owned_pull',return_value=self.pull()),patch.object(sync,'api',side_effect=responses) as api,patch.object(sync,'command') as command:
            self.assertEqual(self.queue(),'awaiting_checks');command.assert_called_once()
            self.assertFalse(any(call.args[0]=='graphql' for call in api.call_args_list))

    def test_denied_protection_diagnostics_are_sanitized(self):
        import contextlib,io
        secret='sensitive-provider-error'
        output=io.StringIO()
        with contextlib.redirect_stdout(output),patch.object(sync,'api',side_effect=subprocess.CalledProcessError(1,['gh'],stderr=secret)):
            self.assertFalse(sync.has_trusted_required_gate(self.repo,{'protected':True}))
        self.assertNotIn(secret,output.getvalue())
        self.assertIn('manual merge',output.getvalue())

    def test_malformed_nested_protection_metadata_fails_closed(self):
        rest=[[], {'protection':[]}, {'protection':{'required_status_checks':'invalid'}},
              {'protection':{'enabled':True,'required_status_checks':{'enforcement_level':'everyone','checks':[None,'invalid']}}}]
        for value in rest:
            with patch.object(sync,'api',return_value={}):
                self.assertFalse(sync.has_trusted_required_gate(self.repo,value))
        graphql=[{'data':[]},{'data':{'repository':1}},{'data':{'repository':{'ref':'invalid'}}},
                 {'data':{'repository':{'ref':{'branchProtectionRule':[]}}}},
                 {'data':{'repository':{'ref':{'branchProtectionRule':{'requiresStatusChecks':True,'requiredStatusChecks':[None,{'app':'invalid'}]}}}}}]
        for value in graphql:
            with patch.object(sync,'api',return_value=value):
                self.assertFalse(sync.has_trusted_required_gate(self.repo))

    def test_verify_gate_is_supported(self):
        rule=self.rule(); rule['data']['repository']['ref']['branchProtectionRule']['requiredStatusChecks'][0]['context']='verify'
        with patch.object(sync, 'api', return_value=rule): self.assertTrue(sync.has_trusted_required_gate(self.repo))

    def test_pr_binding_or_remote_head_race_cannot_enable_auto_merge(self):
        with patch.object(sync, 'read_owned_pull', side_effect=[self.pull(), ValueError('binding changed')]), patch.object(sync, 'api', side_effect=self.responses()), patch.object(sync, 'command') as command:
            with self.assertRaises(ValueError): self.queue()
            command.assert_not_called()
        responses=self.responses(); responses[3]={'object': {'sha': 'b'*40}}
        with patch.object(sync, 'read_owned_pull', return_value=self.pull()), patch.object(sync, 'api', side_effect=responses), patch.object(sync, 'command') as command:
            with self.assertRaises(ValueError): self.queue()
            command.assert_not_called()

    def test_newer_main_never_enables_stale_auto_merge(self):
        responses=self.responses(); responses[4]={'object': {'sha': 'd'*40}}
        with patch.object(sync, 'read_owned_pull', return_value=self.pull()), patch.object(sync, 'api', side_effect=responses), patch.object(sync, 'command') as command:
            self.assertEqual(self.queue(), 'superseded'); command.assert_not_called()

    def test_fast_native_merge_is_reported_only_after_actual_confirmation(self):
        merged={**self.pull(), 'state': 'closed', 'merged': True, 'merge_commit_sha': 'f'*40}
        with patch.object(sync, 'read_owned_pull', side_effect=[self.pull(), self.pull(), merged]), patch.object(sync, 'api', side_effect=self.responses()), patch.object(sync, 'command'):
            self.assertEqual(self.queue(), 'merged')
        with patch.object(sync, 'read_owned_pull', return_value=merged), patch.object(sync, 'command') as command:
            self.assertEqual(self.queue(), 'merged'); command.assert_not_called()

    def test_native_merge_winning_command_race_is_idempotent(self):
        merged={**self.pull(), 'state': 'closed', 'merged': True, 'merge_commit_sha': 'f'*40}
        error=subprocess.CalledProcessError(1, ['gh'])
        with patch.object(sync, 'read_owned_pull', side_effect=[self.pull(), self.pull(), merged]), patch.object(sync, 'api', side_effect=self.responses()), patch.object(sync, 'command', side_effect=error):
            self.assertEqual(self.queue(), 'merged')
        with patch.object(sync, 'read_owned_pull', return_value=self.pull()), patch.object(sync, 'api', side_effect=self.responses()), patch.object(sync, 'command', side_effect=error):
            with self.assertRaises(subprocess.CalledProcessError): self.queue()


class LaunchProofTests(unittest.TestCase):
    repo='pwngames/app'; branch='sync/main-to-develop-aaaaaaaaaaaa'; head='a'*40

    def run_fixture(self, **changes):
        result=dict(id=22,head_sha=self.head,head_branch=self.branch,event='pull_request',path='.github/workflows/ci.yml',status='queued',conclusion=None,head_repository={'full_name':self.repo},pull_requests=[{'number':7,'head':{'sha':self.head,'ref':self.branch},'base':{'ref':'develop'}}]);result.update(changes);return result

    def test_queued_ci_is_accepted_without_completion_poll(self):
        with patch.object(sync,'api',return_value=self.run_fixture()) as api,patch('time.sleep') as sleep:
            sync.verify_ci_launch(self.repo,7,self.branch,self.head,sync.PreparedCI(22))
            api.assert_called_once();sleep.assert_not_called()

    def test_foreign_missing_or_wrong_ci_cannot_qualify(self):
        for change in [{'id':23},{'head_sha':'b'*40},{'head_branch':'foreign'},{'path':'.github/workflows/foreign.yml'},{'event':'push'},{'head_repository':{'full_name':'fork/app'}},{'pull_requests':[]}]:
            with self.subTest(change=change),patch.object(sync,'api',return_value=self.run_fixture(**change)),self.assertRaises(ValueError):
                sync.verify_ci_launch(self.repo,7,self.branch,self.head,sync.PreparedCI(22))

    def test_completed_failure_or_missing_real_gate_cannot_qualify(self):
        for conclusion in ['failure','cancelled','timed_out','skipped','neutral']:
            with patch.object(sync,'api',return_value=self.run_fixture(status='completed',conclusion=conclusion)),self.assertRaises(ValueError):
                sync.verify_ci_launch(self.repo,7,self.branch,self.head,sync.PreparedCI(22))
        for jobs in [[],[{'name':'ci-gate','conclusion':'skipped'}],[{'name':'unrelated','conclusion':'success'}]]:
            with patch.object(sync,'api',side_effect=[self.run_fixture(status='completed',conclusion='success'),{'jobs':jobs}]),self.assertRaises(ValueError):
                sync.verify_ci_launch(self.repo,7,self.branch,self.head,sync.PreparedCI(22))
        with patch.object(sync,'api',side_effect=[self.run_fixture(status='completed',conclusion='success'),{'jobs':[{'name':'verify','conclusion':'success'}]}]):
            sync.verify_ci_launch(self.repo,7,self.branch,self.head,sync.PreparedCI(22))

    def test_dispatch_registration_accepts_pending_but_never_foreign_run(self):
        run=self.run_fixture(event='workflow_dispatch')
        with patch.object(sync,'api',return_value={'workflow_runs':[run]}),patch('time.sleep') as sleep:
            self.assertEqual(sync.find_dispatched_ci(self.repo,self.branch,self.head),sync.PreparedCI(22));sleep.assert_not_called()
        for change in [{'head_sha':'b'*40},{'path':'foreign'},{'head_repository':{'full_name':'fork/app'}}]:
            with patch.object(sync,'api',return_value={'workflow_runs':[{**run,**change}]}),patch('time.monotonic',side_effect=[0,0,2]),patch('time.sleep'),self.assertRaises(ValueError):
                sync.find_dispatched_ci(self.repo,self.branch,self.head,timeout=1)


class OutcomeTests(unittest.TestCase):
    def args(self):
        return argparse.Namespace(find_production_run=False,run_id='',sha='a'*40,repository='unused',auto_merge=True)

    def test_run_queues_and_returns_truthful_state_without_completion_poll(self):
        with patch.dict(os.environ,{'GITHUB_REPOSITORY':'pwngames/app'}),patch.object(sync,'api',side_effect=[{},[{'number':7}],{'object':{'sha':'a'*40}}]),patch.object(sync,'sync_branch',return_value=('sync/main-to-develop-aaaaaaaaaaaa','b'*40)),patch.object(sync,'prepare_owned_pr_ci',return_value=sync.PreparedCI(22)),patch.object(sync,'verify_ci_launch') as proof,patch.object(sync,'protected_auto_merge',return_value='awaiting_checks') as native,patch('time.sleep') as sleep:
            result=sync.run(self.args())
            self.assertEqual(result,{'state':'awaiting_checks','pull_request_url':'https://github.com/pwngames/app/pull/7','head_sha':'b'*40,'ci_run_id':22})
            proof.assert_called_once_with('pwngames/app',7,'sync/main-to-develop-aaaaaaaaaaaa','b'*40,sync.PreparedCI(22))
            native.assert_called_once_with('pwngames/app',7,'sync/main-to-develop-aaaaaaaaaaaa','b'*40,main_sha='a'*40)
            sleep.assert_not_called()

    def test_new_main_after_ci_launch_is_superseded_without_auto_merge(self):
        with patch.dict(os.environ,{'GITHUB_REPOSITORY':'pwngames/app'}),patch.object(sync,'api',side_effect=[{},[{'number':7}],{'object':{'sha':'c'*40}}]),patch.object(sync,'sync_branch',return_value=('sync/main-to-develop-aaaaaaaaaaaa','b'*40)),patch.object(sync,'prepare_owned_pr_ci',return_value=sync.PreparedCI(22)),patch.object(sync,'verify_ci_launch'),patch.object(sync,'protected_auto_merge') as native:
            self.assertEqual(sync.run(self.args())['state'],'superseded');native.assert_not_called()

    def test_native_merge_winning_during_ci_preparation_is_reported_idempotently(self):
        pull={'state':'closed','merged':True,'merge_commit_sha':'f'*40}
        with patch.dict(os.environ,{'GITHUB_REPOSITORY':'pwngames/app'}),patch.object(sync,'api',side_effect=[{},[{'number':7}]]),patch.object(sync,'sync_branch',return_value=('sync/main-to-develop-aaaaaaaaaaaa','b'*40)),patch.object(sync,'prepare_owned_pr_ci',side_effect=ValueError('already closed')),patch.object(sync,'read_owned_pull',return_value=pull),patch.object(sync,'command') as command:
            self.assertEqual(sync.run(self.args())['state'],'merged');command.assert_not_called()

    def test_invalid_closed_pr_cannot_be_reported_as_merged(self):
        for pull in [{'state':'closed','merged':False},{'state':'closed','merged':True,'merge_commit_sha':'invalid'}]:
            with patch.dict(os.environ,{'GITHUB_REPOSITORY':'pwngames/app'}),patch.object(sync,'api',side_effect=[{},[{'number':7}]]),patch.object(sync,'sync_branch',return_value=('sync/main-to-develop-aaaaaaaaaaaa','b'*40)),patch.object(sync,'prepare_owned_pr_ci',side_effect=ValueError('closed')),patch.object(sync,'read_owned_pull',return_value=pull),self.assertRaises(ValueError):
                sync.run(self.args())

    def test_structured_output_never_promises_pending_merge(self):
        scratch=Path(__file__).resolve().parents[1]/'temp';scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder,patch.dict(os.environ,{'GITHUB_OUTPUT':str(Path(folder)/'outputs')}):
            result=sync.emit_state('awaiting_checks',ci_run_id=22)
            self.assertEqual(result['state'],'awaiting_checks')
            self.assertEqual((Path(folder)/'outputs').read_text(),'state=awaiting_checks\nci_run_id=22\n')

    def test_unapproved_action_required_metadata_cannot_qualify(self):
        case=LaunchProofTests();run=case.run_fixture(status='completed',conclusion='action_required')
        with patch.object(sync,'api',return_value=run),self.assertRaises(ValueError):
            sync.verify_ci_launch(case.repo,7,case.branch,case.head,sync.PreparedCI(22))
        with patch.object(sync,'api',return_value=run):
            sync.verify_ci_launch(case.repo,7,case.branch,case.head,sync.PreparedCI(22,True))


class OwnedApprovalTests(unittest.TestCase):
    repo='pwngames/app'
    branch='sync/main-to-develop-aaaaaaaaaaaa'
    head='a'*40

    def fixtures(self):
        pull=dict(state='open',user={'login':'github-actions[bot]'},head={'sha':self.head,'ref':self.branch,'repo':{'full_name':self.repo}},base={'ref':'develop'})
        run=dict(id=22,head_sha=self.head,head_branch=self.branch,path='.github/workflows/ci.yml',event='pull_request',head_repository={'full_name':self.repo},conclusion='action_required',actor={'login':'github-actions[bot]'},pull_requests=[{'number':7,'head':{'sha':self.head,'ref':self.branch},'base':{'ref':'develop'}}])
        return pull,run

    def test_only_exact_owned_bot_run_is_approved(self):
        pull,run=self.fixtures()
        with patch.object(sync,'api',side_effect=[pull,{'workflow_runs':[run]},pull,{'object':{'sha':self.head}},{}]) as api:
            self.assertEqual(sync.prepare_owned_pr_ci(self.repo,7,self.branch,self.head),sync.PreparedCI(22,True))
            self.assertEqual(api.call_args.args,(f'repos/{self.repo}/actions/runs/22/approve','--method','POST'))

    def test_other_actor_or_pr_author_cannot_be_approved(self):
        for actor in ['run','pull']:
            pull,run=self.fixtures()
            (run if actor=='run' else pull)['actor' if actor=='run' else 'user']={'login':'someone-else'}
            with patch.object(sync,'api',side_effect=[pull,{'workflow_runs':[run]}]) as api,self.assertRaises(ValueError):
                sync.prepare_owned_pr_ci(self.repo,7,self.branch,self.head)
            self.assertEqual(api.call_count,2)

    def test_moved_or_foreign_pr_never_queries_or_approves_runs(self):
        for key,value in [('ref','foreign'),('repo',{'full_name':'fork/app'})]:
            pull,run=self.fixtures();pull['head'][key]=value
            with patch.object(sync,'api',return_value=pull) as api,self.assertRaises(ValueError):
                sync.prepare_owned_pr_ci(self.repo,7,self.branch,self.head)
            api.assert_called_once()

    def test_foreign_workflow_head_or_missing_pr_link_is_never_approved(self):
        for change in [{'path':'.github/workflows/untrusted.yml'},{'head_sha':'b'*40},{'head_repository':{'full_name':'fork/app'}},{'pull_requests':[]}]:
            pull,run=self.fixtures();run.update(change)
            with patch.object(sync,'api',side_effect=[pull,{'workflow_runs':[run]}]) as api,patch('time.monotonic',side_effect=[0,0,2]),patch('time.sleep'):
                self.assertIsNone(sync.prepare_owned_pr_ci(self.repo,7,self.branch,self.head,timeout=1))
            self.assertEqual(api.call_count,2)

    def test_existing_normal_pr_ci_is_reused_without_approval_or_extra_dispatch(self):
        pull,run=self.fixtures();run['conclusion']=None;run['status']='in_progress'
        with patch.object(sync,'api',side_effect=[pull,{'workflow_runs':[run]}]) as api:
            self.assertEqual(sync.prepare_owned_pr_ci(self.repo,7,self.branch,self.head),sync.PreparedCI(22))
            self.assertEqual(api.call_count,2)


    def test_head_moved_immediately_before_approval_is_rejected(self):
        pull,run=self.fixtures();moved={**pull,'head':{**pull['head'],'sha':'b'*40}}
        with patch.object(sync,'api',side_effect=[pull,{'workflow_runs':[run]},moved,{'object':{'sha':'b'*40}}]) as api,self.assertRaises(ValueError):
            sync.prepare_owned_pr_ci(self.repo,7,self.branch,self.head)
        self.assertEqual(api.call_count,4)


    def test_cached_matching_pr_cannot_approve_a_moved_remote_head(self):
        pull,run=self.fixtures()
        with patch.object(sync,'api',side_effect=[pull,{'workflow_runs':[run]},pull,{'object':{'sha':'b'*40}}]) as api,self.assertRaises(ValueError):
            sync.prepare_owned_pr_ci(self.repo,7,self.branch,self.head)
        self.assertEqual(api.call_count,4)


class HeadConvergenceTests(unittest.TestCase):
    def fixtures(self):
        return dict(state='open',head={'sha':'b'*40,'ref':'sync/main-to-develop-aaaaaaaaaaaa','repo':{'full_name':'pwngames/app'}},base={'ref':'develop'})

    def test_stale_pr_metadata_waits_only_when_remote_is_expected_head(self):
        stale=self.fixtures();current={**stale,'head':{**stale['head'],'sha':'a'*40}}
        with patch.object(sync,'api',side_effect=[stale,{'object':{'sha':'a'*40}},current]) as api,patch('time.sleep') as sleep:
            self.assertEqual(sync.read_owned_pull('pwngames/app',7,'sync/main-to-develop-aaaaaaaaaaaa','a'*40),current)
            sleep.assert_called_once_with(2)
            self.assertEqual(api.call_count,3)

    def test_real_remote_movement_fails_without_waiting_or_approval(self):
        with patch.object(sync,'api',side_effect=[self.fixtures(),{'object':{'sha':'c'*40}}]),patch('time.sleep') as sleep,self.assertRaises(ValueError):
            sync.read_owned_pull('pwngames/app',7,'sync/main-to-develop-aaaaaaaaaaaa','a'*40)
        sleep.assert_not_called()

    def test_convergence_timeout_is_bounded_and_read_only(self):
        with patch.object(sync,'api',side_effect=[self.fixtures(),{'object':{'sha':'a'*40}}]*2) as api,patch('time.sleep') as sleep,self.assertRaises(ValueError):
            sync.read_owned_pull('pwngames/app',7,'sync/main-to-develop-aaaaaaaaaaaa','a'*40,attempts=2)
        self.assertEqual(api.call_count,4)
        sleep.assert_called_once_with(2)

    def test_immutable_binding_changes_never_wait(self):
        stale=self.fixtures();stale['base']['ref']='main'
        with patch.object(sync,'api',return_value=stale) as api,patch('time.sleep') as sleep,self.assertRaises(ValueError):
            sync.read_owned_pull('pwngames/app',7,'sync/main-to-develop-aaaaaaaaaaaa','a'*40)
        api.assert_called_once();sleep.assert_not_called()


class MergedBindingTests(unittest.TestCase):
    def test_only_exact_owned_merged_pr_is_idempotent(self):
        pull={'state':'closed','merged':True,'merge_commit_sha':'f'*40,'head':{'sha':'a'*40,'ref':'sync/main-to-develop-aaaaaaaaaaaa','repo':{'full_name':'pwngames/app'}},'base':{'ref':'develop'}}
        with patch.object(sync,'api',return_value=pull):
            self.assertEqual(sync.read_owned_pull('pwngames/app',7,'sync/main-to-develop-aaaaaaaaaaaa','a'*40,allow_merged=True),pull)
        for change in [{'merged':False},{'head':{**pull['head'],'sha':'b'*40}},{'base':{'ref':'main'}},{'head':{**pull['head'],'repo':{'full_name':'fork/app'}}}]:
            with self.subTest(change=change),patch.object(sync,'api',return_value={**pull,**change}) as api,self.assertRaises(ValueError):
                sync.read_owned_pull('pwngames/app',7,'sync/main-to-develop-aaaaaaaaaaaa','a'*40,allow_merged=True)
            api.assert_called_once()
