"""Execute copied installer scripts against temporary files + fake Upstart/mount tools.
No original /etc, /var/local, Kindle mount or live service is touched.
"""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

SOURCE=Path(__file__).resolve().parent


class InstallTransactions(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.root=self.base/'private';self.root.mkdir(mode=0o700)
        self.package=self.base/'package';self.install=self.package/'install';self.install.mkdir(parents=True)
        (self.package/'bin').mkdir();(self.package/'upstart').mkdir()
        self.jobs=self.base/'upstart';self.jobs.mkdir();self.states=self.base/'states';self.states.mkdir()
        self.commands=self.base/'commands';self.commands.mkdir();self.calls=self.base/'calls'
        self.env={**os.environ,'PATH':str(self.commands)+':/usr/bin:/bin','TEST_BASE':str(self.base)}
        for p in SOURCE.glob('*.sh'):
            body=p.read_text().replace('JOBS=/etc/upstart','JOBS='+str(self.jobs))
            (self.install/p.name).write_text(body)
        common=(SOURCE/'common.sh').read_text().replace('ROOT=/var/local/kindle-native-sync','ROOT='+str(self.root))
        common+='\nrequire_device() { :; }\nspace_kib() { printf \"131072\\n\"; }\nroot_rw() { fake-mount rw; }\nroot_ro() { fake-mount ro; }\n'
        (self.install/'common.sh').write_text(common)
        for name in ('ksync-collector','ksync-worker','ksync-event-adapter'):
            body=bytearray(64);body[:7]=b'\x7fELF\x01\x01\x01';body[18:20]=b'\x28\x00'
            (self.package/'bin'/name).write_bytes(body)
        (self.package/'manifest.json').write_text('{"candidate":true}\n')
        for job in ('kindle-native-sync','kindle-native-sync-events'):(self.package/'upstart'/(job+'.conf')).write_text('new '+job)
        (self.package/'SHA256SUMS').write_text('test-fixture\n')
        tool='#!'+sys.executable+'''\nimport os,sys,json,shutil
from pathlib import Path
base=Path(os.environ['TEST_BASE']);states=base/'states';name=Path(sys.argv[0]).name;args=sys.argv[1:]
with (base/'calls').open('a') as log:log.write(json.dumps([name,*args])+'\\n')
failure=os.environ.get('TEST_FAIL','');marker=base/('failed-'+failure.replace('/','_'))
def fail_once(condition):
 if condition and not marker.exists():marker.write_text('once');raise SystemExit(9)
if name=='fake-mount':
 fail_once(failure=='mount_'+args[0]);(base/'root-mode').write_text(args[0]);raise SystemExit(0)
if name=='cp':
 # Inject failure after staging has completed and active files are being applied.
 fail_once(failure=='copy_stage' and '/.install-stage/' in args[-1])
 fail_once(failure=='copy_backup' and '/upgrade.previous/' in args[-1])
 fail_once(failure=='copy_job' and str(base/'upstart')+'/' in args[-1])
 os.execv('/bin/cp',['/bin/cp',*args])
if name=='sha256sum':raise SystemExit(0)
if name=='sync':
 snapshot={'backup_complete':(base/'private/upgrade.previous/complete').exists(),
           'backup_evidence':(base/'private/upgrade.previous/validation-device.json').exists(),
           'active_evidence':(base/'private/validation-device.json').exists(),
           'old_binary':(base/'private/bin/ksync-worker').exists() and (base/'private/bin/ksync-worker').read_bytes()==b'OLD WORKER'}
 with (base/'sync-states').open('a') as log:log.write(json.dumps(snapshot)+'\\n')
 raise SystemExit(0)
if name=='mv':os.execv('/bin/mv',['/bin/mv',*args])
if name=='status':
 path=states/args[0]
 if path.exists() and path.read_text()=='running':print(args[0]+' start/running, process 123');raise SystemExit(0)
 print(args[0]+' stop/waiting');raise SystemExit(1)
if name=='start':
 path=states/args[0]
 if path.exists() and path.read_text()=='running':raise SystemExit(1)
 fail_once(failure=='start_collector' and args[0]=='kindle-native-sync')
 if not (base/'upstart'/(args[0]+'.conf')).exists():raise SystemExit(1)
 path.write_text('running')
 if args[0]=='kindle-native-sync' and (base/'upstart/kindle-native-sync-events.conf').exists():(states/'kindle-native-sync-events').write_text('running')
 raise SystemExit(0)
if name=='stop':
 (states/args[0]).write_text('stopped');raise SystemExit(0)
if name=='initctl':
 fail_once(failure=='reload');raise SystemExit(0)
raise SystemExit(1)
'''
        for name in ('fake-mount','cp','mv','sync','sha256sum','status','start','stop','initctl'):
            p=self.commands/name;p.write_text(tool);p.chmod(0o700)
        self.private={'auth.json':'PRIVATE_AUTH','config.json':'PRIVATE_CONFIG','ledger.sqlite':'PRIVATE_LEDGER','other-data':'PRIVATE_DATA'}
        for name,body in self.private.items():(self.root/name).write_text(body)
        (self.base/'root-mode').write_text('ro')

    def old(self,collector=True,events=True):
        (self.root/'bin').mkdir();(self.root/'bin/ksync-worker').write_text('OLD WORKER')
        (self.root/'install').mkdir();(self.root/'install/old.sh').write_text('OLD SCRIPT')
        (self.root/'package-manifest.json').write_text('OLD MANIFEST')
        (self.root/'validation-device.json').write_text('OLD EVIDENCE')
        (self.root/'ca-bundle.crt').write_text('OLD CA')
        for job,run in [('kindle-native-sync',collector),('kindle-native-sync-events',events)]:
            (self.jobs/(job+'.conf')).write_text('OLD '+job)
            (self.states/job).write_text('running' if run else 'stopped')
        self.old_snapshot=self.snapshot()

    def snapshot(self):
        return {str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def execute(self,script='install.sh',failure=''):
        env={**self.env,'TEST_FAIL':failure}
        cmd=['sh',str(self.install/script)]
        if script=='install.sh':cmd.append(str(self.package))
        return subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)

    def assert_private(self):
        for name,body in self.private.items():self.assertEqual((self.root/name).read_text(),body)
        self.assertEqual((self.base/'root-mode').read_text(),'ro')

    def assert_restored(self,collector=True,events=True):
        self.assertEqual(self.snapshot(),self.old_snapshot)
        for job,run in [('kindle-native-sync',collector),('kindle-native-sync-events',events)]:
            self.assertEqual((self.states/job).read_text(),'running' if run else 'stopped')
            self.assertEqual((self.jobs/(job+'.conf')).read_text(),'OLD '+job)
        self.assert_private()

    def test_upgrade_preserves_one_complete_old_set_and_invalidates_active_acceptance(self):
        self.old();r=self.execute();self.assertEqual(r.returncode,0,r.stderr)
        backup=self.root/'upgrade.previous'
        self.assertEqual((backup/'bin/ksync-worker').read_text(),'OLD WORKER')
        self.assertEqual((backup/'install/old.sh').read_text(),'OLD SCRIPT')
        self.assertEqual((backup/'package-manifest.json').read_text(),'OLD MANIFEST')
        self.assertEqual((backup/'validation-device.json').read_text(),'OLD EVIDENCE')
        self.assertEqual((backup/'jobs/kindle-native-sync.conf').read_text(),'OLD kindle-native-sync')
        self.assertFalse((self.root/'validation-device.json').exists());self.assert_private()
        calls=[json.loads(x) for x in self.calls.read_text().splitlines()]
        self.assertFalse(any(c==['start','kindle-native-sync-events'] for c in calls))
        self.assertEqual((self.states/'kindle-native-sync-events').read_text(),'running')
        # Another upgrade never overwrites the complete historical recovery set.
        r=self.execute();self.assertNotEqual(r.returncode,0);self.assertEqual((backup/'bin/ksync-worker').read_text(),'OLD WORKER')

    def test_upgrade_mount_copy_reload_and_start_failure_restore_every_old_component(self):
        for failure in ['mount_rw','copy_backup','copy_stage','copy_job','mount_ro','reload','start_collector']:
            with self.subTest(failure=failure):
                # Separate fixtures avoid leaking mocked failure history across phases.
                case=InstallTransactions('test_upgrade_preserves_one_complete_old_set_and_invalidates_active_acceptance');case.setUp()
                try:
                    case.old(collector=True,events=False)
                    r=case.execute(failure=failure);self.assertNotEqual(r.returncode,0,r.stdout)
                    case.assert_restored(collector=True,events=False)
                finally:case.doCleanups()

    def test_acceptance_is_durably_invalidated_before_any_binary_cutover(self):
        self.old();r=self.execute();self.assertEqual(r.returncode,0,r.stderr)
        snapshots=[json.loads(line) for line in (self.base/'sync-states').read_text().splitlines()]
        self.assertGreaterEqual(len(snapshots),2)
        self.assertTrue(snapshots[0]['backup_complete']);self.assertTrue(snapshots[0]['backup_evidence'])
        self.assertTrue(snapshots[0]['active_evidence']);self.assertTrue(snapshots[0]['old_binary'])
        self.assertTrue(snapshots[1]['backup_complete']);self.assertTrue(snapshots[1]['backup_evidence'])
        self.assertFalse(snapshots[1]['active_evidence']);self.assertTrue(snapshots[1]['old_binary'])
        calls=[json.loads(line) for line in self.calls.read_text().splitlines()]
        barriers=[i for i,c in enumerate(calls) if c==['sync']]
        cutovers=[i for i,c in enumerate(calls) if c[0]=='mv' and c[1].endswith('/.install-stage/bin')]
        self.assertEqual(len(cutovers),1);self.assertLess(barriers[1],cutovers[0])

    def test_first_install_failure_keeps_selftest_ledger_and_removes_only_new_components(self):
        before=self.snapshot();r=self.execute(failure='start_collector');self.assertNotEqual(r.returncode,0)
        self.assertEqual(self.snapshot(),before);self.assert_private()
        self.assertEqual(list(self.jobs.iterdir()),[])

    def test_disabled_and_unresolved_residue_are_rejected_without_touching_components(self):
        self.old();(self.root/'disabled').write_text('');before=self.snapshot()
        r=self.execute();self.assertNotEqual(r.returncode,0);self.assertEqual(self.snapshot(),before)
        (self.root/'disabled').unlink();(self.root/'bin.next').mkdir();before=self.snapshot()
        r=self.execute();self.assertNotEqual(r.returncode,0);self.assertEqual(self.snapshot(),before)
        self.assert_private()

    def test_uninstall_failure_restores_jobs_and_service_state(self):
        self.old();r=self.execute('uninstall.sh',failure='reload');self.assertNotEqual(r.returncode,0)
        self.assert_restored()

    def budget(self):
        script='. "$1"; if ksync_storage_budget "$2" 42322;then code=0;else code=$?;fi; printf "%s %s %s %s %s\\n" "$code" "$storage_budget_valid" "$storage_budget_blocked" "$required_runtime_free_kib" "$required_install_free_kib"'
        r=subprocess.run(['sh','-c',script,'budget',str(self.install/'common.sh'),str(self.package)],env=self.env,capture_output=True,text=True,check=True)
        return r.stdout.split()

    def test_storage_budget_uses_remaining_allowances_and_rejects_oversize_ledger(self):
        code,valid,reason,runtime,install=self.budget()
        self.assertEqual((code,valid,reason),('0','true','none'))
        self.assertGreater(int(install),int(runtime));self.assertLess(int(install),42322)
        baseline=int(runtime)
        (self.root/'events').mkdir();(self.root/'events/active.log').write_bytes(b'X'*1024*1024)
        current=self.budget();self.assertLess(int(current[3]),baseline)
        (self.root/'ledger.sqlite').write_bytes(b'X'*(4*1024*1024+1))
        self.assertEqual(self.budget()[:3],['1','false','ledger_over_limit'])
        before=self.snapshot();r=self.execute();self.assertNotEqual(r.returncode,0);self.assertEqual(self.snapshot(),before)
        self.assertIn('ledger_over_limit',r.stderr)

    def test_full_old_recovery_set_is_included_in_upgrade_peak(self):
        original=self.budget();self.old()
        after=self.budget();self.assertGreater(int(after[4]),int(original[4]))
        # Make a realistic ~5 MiB old component and ~5 MiB new candidate. A
        # 41 MiB-free device must not silently consume runtime/system reserves.
        (self.root/'bin/ksync-worker').write_bytes(b'X'*(5*1024*1024))
        (self.package/'bin/ksync-worker').write_bytes(b'X'*(5*1024*1024))
        measured=self.budget();self.assertGreater(int(measured[4]),42322)
        # A completed previous backup is already included in df, but is still
        # measured/reported and may never be overwritten by the transaction.
        (self.root/'upgrade.previous').mkdir();(self.root/'upgrade.previous/held').write_bytes(b'X'*1024)
        r=self.execute();self.assertNotEqual(r.returncode,0)
        self.assertEqual((self.root/'upgrade.previous/held').read_bytes(),b'X'*1024)


    def test_uninstall_cleans_project_runtime_and_staging_but_keeps_private_data(self):
        self.old();(self.root/'upgrade.previous').mkdir();(self.root/'upgrade.previous/old.bin').write_text('OLD')
        (self.root/'bin.next').mkdir();(self.root/'bin.next/staged').write_text('STAGED')
        r=self.execute('uninstall.sh');self.assertEqual(r.returncode,0,r.stderr)
        self.assert_private();self.assertFalse((self.root/'disabled').exists())
        for name in ['bin','install','upgrade.previous','bin.next','.install-stage','.uninstall-backup','.install-transaction']:
            self.assertFalse((self.root/name).exists(),name)
        self.assertEqual((self.root/'validation-device.json').read_text(),'OLD EVIDENCE')
        self.assertEqual(list(self.jobs.iterdir()),[])


if __name__=='__main__':unittest.main()
