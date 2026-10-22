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

    def test_no_auto_merge_without_verified_protections(self):
        for settings,protection in [({'allow_auto_merge':False},{}),({'allow_auto_merge':True,'allow_merge_commit':True},{}),({'allow_auto_merge':True,'allow_merge_commit':True},{'required_status_checks':{'contexts':['ci-gate']},'required_linear_history':{'enabled':True}})]:
            with patch.object(sync,'api',side_effect=[settings,protection]),patch.object(sync,'command') as command:
                self.assertFalse(sync.protected_auto_merge('pwngames/app',1,'a'*40))
                command.assert_not_called()

    def test_auto_merge_matches_head_without_bypass(self):
        with patch.object(sync,'api',side_effect=[{'allow_auto_merge':True,'allow_merge_commit':True},{'required_status_checks':{'contexts':['ci-gate']}}]),patch.object(sync,'command') as command:
            self.assertTrue(sync.protected_auto_merge('pwngames/app',1,'a'*40))
            self.assertEqual(command.call_args.args,('gh','pr','merge','1','--repo','pwngames/app','--auto','--merge','--match-head-commit','a'*40))

    def test_duplicate_pr_reused_and_ci_explicitly_triggered(self):
        args=argparse.Namespace(run_id='',sha='a'*40,repository='unused',auto_merge=False)
        with patch.dict(os.environ,{'GITHUB_REPOSITORY':'pwngames/app'}),patch.object(sync,'api',side_effect=[[{'name':'develop'}],[{'number':17}],{'object':{'sha':'a'*40}}]) as api,patch.object(sync,'sync_branch',return_value=('sync/main-to-develop-aaaaaaaaaaaa','b'*40)),patch.object(sync,'command') as command:
            sync.run(args)
            self.assertEqual(api.call_count,3)
            command.assert_called_once_with('gh','workflow','run','ci.yml','--repo','pwngames/app','--ref','sync/main-to-develop-aaaaaaaaaaaa')

    def test_no_develop_branch_is_not_created(self):
        args=argparse.Namespace(run_id='',sha='a'*40,repository='unused',auto_merge=False)
        with patch.dict(os.environ,{'GITHUB_REPOSITORY':'pwngames/app'}),patch.object(sync,'api',return_value=[{'name':'main'}]),patch.object(sync,'sync_branch') as branch:
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
