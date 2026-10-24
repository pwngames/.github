import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]

class PushHooks(unittest.TestCase):
 def setUp(self):
  configured=subprocess.run(['git','-C',str(ROOT),'config','--get','pwn.prepushScratchRoot'],text=True,capture_output=True).stdout.strip()
  scratch=Path(os.environ.get('PWN_PREPUSH_SCRATCH_ROOT') or configured or ROOT/'temp')
  scratch.mkdir(parents=True,exist_ok=True)
  self.tmp=tempfile.TemporaryDirectory(dir=scratch,prefix='prepush-contract-')
  self.addCleanup(self.tmp.cleanup)
  self.base=Path(self.tmp.name); self.repo=self.base/'source';self.repo.mkdir();self.remote=self.base/'remote.git'
  self.run_git('init','-q','--initial-branch=feature/test')
  self.run_git('config','user.name','Hook tests');self.run_git('config','user.email','hooks@example.invalid')
  (self.repo/'.githooks').mkdir();(self.repo/'scripts').mkdir()
  shutil.copy(ROOT/'.githooks/pre-push',self.repo/'.githooks/pre-push')
  shutil.copy(ROOT/'scripts/pre-push.py',self.repo/'scripts/pre-push.py')
  (self.repo/'.gitignore').write_text('.env\n')
  self.gate('test ! -f .env\ntest -z "${PROVIDER_SECRET:-}"\nprintf passed > "'+str(self.base/'result')+'"\n')
  self.run_git('add','.');self.run_git('commit','-qm','initial')
  shutil.copy(ROOT/'scripts/install-git-hooks.sh',self.repo/'scripts/install-git-hooks.sh')
  self.commit()
  subprocess.run(['bash','scripts/install-git-hooks.sh'],cwd=self.repo,check=True,capture_output=True)
  self.commit()  # Record executable permissions set by the real installer.
  self.run_git('config','pwn.prepushScratchRoot',str(self.base/'scratch'))
  subprocess.run(['git','init','--bare','-q',str(self.remote)],check=True)
  self.run_git('remote','add','origin',str(self.remote))
 def run_git(self,*args,check=True):
  return subprocess.run(['git','-C',str(self.repo),*args],text=True,capture_output=True,check=check)
 def gate(self,body):
  (self.repo/'.githooks/checks.sh').write_text('#!/usr/bin/env bash\nset -euo pipefail\n'+body)
 def commit(self):
  self.run_git('add','.');self.run_git('commit','-qm','change')
 def test_real_push_passes_isolated_without_local_secret_or_inherited_secret(self):
  (self.repo/'.env').write_text('local secret fixture')
  result=subprocess.run(['git','-C',str(self.repo),'push','origin','HEAD:refs/heads/feature/pass'],env={**os.environ,'PROVIDER_SECRET':'sentinel'},text=True,capture_output=True)
  self.assertEqual(result.returncode,0,result.stderr)
  self.assertEqual((self.base/'result').read_text(),'passed')
  self.assertEqual(list((self.base/'scratch').iterdir()),[])
 def test_failed_checks_prevent_remote_update(self):
  self.gate('exit 42\n');self.commit()
  result=self.run_git('push','origin','HEAD:refs/heads/feature/fail',check=False)
  self.assertNotEqual(result.returncode,0)
  self.assertEqual(self.run_git('ls-remote','--heads','origin').stdout,'')
 def test_dirty_checkout_blocks(self):
  (self.repo/'uncommitted').write_text('pending')
  self.assertNotEqual(self.run_git('push','origin','HEAD:refs/heads/feature/dirty',check=False).returncode,0)
  self.assertFalse((self.base/'result').exists())
 def test_push_of_another_commit_blocks(self):
  old=self.run_git('rev-parse','HEAD').stdout.strip()
  (self.repo/'new').write_text('next');self.commit()
  self.assertNotEqual(self.run_git('push','origin',old+':refs/heads/feature/old',check=False).returncode,0)
 def test_direct_protected_branch_push_blocks(self):
  for target in ['develop','main','master']:
   self.assertNotEqual(self.run_git('push','origin','HEAD:refs/heads/'+target,check=False).returncode,0)
  self.assertFalse((self.base/'result').exists())
 def test_missing_gate_fails_closed(self):
  (self.repo/'.githooks/checks.sh').unlink();self.commit()
  self.assertNotEqual(self.run_git('push','origin','HEAD:refs/heads/feature/missing',check=False).returncode,0)
 def test_deletion_only_does_not_run_checks(self):
  text='(delete) '+'0'*40+' refs/heads/feature/old '+'1'*40+'\n'
  result=subprocess.run([str(self.repo/'.githooks/pre-push')],cwd=self.repo,input=text,text=True,capture_output=True)
  self.assertEqual(result.returncode,0,result.stderr);self.assertFalse((self.base/'result').exists())
 def test_branch_without_hook_source_still_blocks(self):
  self.run_git('rm','-rf','.githooks','scripts');self.run_git('commit','-qm','branch without checks')
  result=self.run_git('push','origin','HEAD:refs/heads/feature/no-hook',check=False)
  self.assertNotEqual(result.returncode,0)
  self.assertEqual(self.run_git('ls-remote','--heads','origin').stdout,'')
 def test_malformed_input_blocks(self):
  result=subprocess.run([str(self.repo/'.githooks/pre-push')],cwd=self.repo,input='bad input\n',text=True,capture_output=True)
  self.assertNotEqual(result.returncode,0)

if __name__=='__main__':unittest.main(verbosity=2)
