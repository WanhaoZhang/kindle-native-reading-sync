"""Generated USB/config data only; never accesses the real device or a live account."""
from contextlib import closing
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import device_bridge as bridge

HERE=Path(__file__).resolve().parent


class DeviceBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'project';self.root.mkdir();self.private=self.root/'.weread';self.private.mkdir(mode=0o700)
        self.mount=Path(self.tmp.name)/'usb';(self.mount/'documents').mkdir(parents=True)
        self.auth={'vid':'synthetic-user','accessToken':'SYNTHETIC-ACCESS','refreshToken':'SYNTHETIC-REFRESH','deviceId':'mac-device'}
        self.write_auth('auth.json',self.auth)
        self.device={**self.auth,'deviceId':'device-only-id'};self.write_auth('device-auth.json',self.device)
        self.db=self.private/'sync-engine.sqlite'
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('CREATE TABLE attempts(batch_id TEXT,state TEXT,seconds INTEGER)')
            db.execute('CREATE TABLE plans(state TEXT)')
            db.execute('CREATE TABLE mappings(account TEXT,source_key TEXT,weread_id TEXT,title TEXT)')

    def write_auth(self,name,value):
        p=self.private/name;p.write_text(json.dumps(value));p.chmod(0o600)

    def add_attempt(self,state='needs_review'):
        with closing(sqlite3.connect(self.db)) as db, db:db.execute('INSERT INTO attempts VALUES (?,?,?)',('synthetic-id',state,13))

    def test_probe_disconnected_and_redacts_full_serial_from_untrusted_export(self):
        self.assertFalse(bridge.probe_usb(self.root)['connected'])
        folder=self.mount/'.kindle-native-sync-export';folder.mkdir()
        (folder/'probe.json').write_text(json.dumps({'firmware':'5.12.2.1.1','model_prefix':'B0D4FULLSERIAL','serial':'PRIVATE','accessToken':'PRIVATE'}))
        value=bridge.probe_usb(self.mount)
        self.assertTrue(value['connected']);self.assertNotIn('PRIVATE',json.dumps(value));self.assertNotIn('FULLSERIAL',json.dumps(value))

    def test_handoff_requires_confirmation_and_fixes_cutoff(self):
        with self.assertRaises(ValueError):bridge.prepare_handoff(self.root)
        one=bridge.prepare_handoff(self.root,confirmed=True,now_unix=1234)
        two=bridge.prepare_handoff(self.root,confirmed=True,now_unix=9999)
        self.assertEqual(one,two);self.assertEqual(two['bootstrap_cutoff_unix'],1234)
        self.assertTrue(bridge.status(self.root,self.mount)['mac_upload_fenced'])
        self.assertEqual((self.private/bridge.FENCE).stat().st_mode&0o777,0o600)

    def test_preserves_unknown_without_releasing_or_replaying_it(self):
        self.add_attempt()
        with self.assertRaises(ValueError):bridge.prepare_handoff(self.root,confirmed=True)
        fence=bridge.prepare_handoff(self.root,confirmed=True,preserve_pending=True)
        self.assertEqual(fence['old_pending_count'],1)
        with closing(sqlite3.connect(self.db)) as db, db:self.assertEqual(db.execute('SELECT state FROM attempts').fetchone()[0],'needs_review')
        self.assertTrue((self.private/'handoff-preserved-pending.json').is_file())
        bridge.export_config(self.root,self.mount)

    def test_active_dispatch_and_committing_block_even_preserve_mode(self):
        self.add_attempt('dispatching')
        with self.assertRaises(ValueError):bridge.prepare_handoff(self.root,confirmed=True,preserve_pending=True)
        with closing(sqlite3.connect(self.db)) as db, db:db.execute('DELETE FROM attempts');db.execute("INSERT INTO plans VALUES ('committing')")
        with self.assertRaises(ValueError):bridge.prepare_handoff(self.root,confirmed=True,preserve_pending=True)
        self.assertFalse((self.private/bridge.FENCE).exists())

    def test_export_requires_fence_distinct_device_and_same_account(self):
        with self.assertRaises((ValueError,FileNotFoundError)):bridge.export_config(self.root,self.mount)
        bridge.prepare_handoff(self.root,confirmed=True)
        self.write_auth('device-auth.json',self.auth)
        with self.assertRaises(ValueError):bridge.export_config(self.root,self.mount)
        self.write_auth('device-auth.json',{**self.device,'vid':'different'})
        with self.assertRaises(ValueError):bridge.export_config(self.root,self.mount)
        self.assertFalse((self.mount/bridge.INBOX).exists())

    def test_export_contract_hashes_default_validation_and_no_secret_in_result(self):
        bridge.prepare_handoff(self.root,confirmed=True,now_unix=1234)
        rows=[{'local_book_id':'Synthetic A','weread_book_id':'example-book','title':'Synthetic Book'}]
        result=bridge.export_config(self.root,self.mount,mappings=rows)
        self.assertNotIn('SYNTHETIC-ACCESS',json.dumps(result))
        inbox=self.mount/bridge.INBOX;config=json.loads((inbox/'config.json').read_text());manifest=json.loads((inbox/'handoff.json').read_text())
        self.assertEqual(config['bootstrap_cutoff_unix'],1234)
        self.assertEqual(config['device_id'],self.device['deviceId'])
        self.assertFalse(config['validation']['protocol_validation']);self.assertFalse(config['validation']['device_validation'])
        for name in ('auth','config'):self.assertEqual(manifest[name+'_sha256'],hashlib.sha256((inbox/(name+'.json')).read_bytes()).hexdigest())
        with self.assertRaises(ValueError):bridge.export_config(self.root,self.mount,mappings=rows)
        self.assertEqual(json.loads((self.private/'auth.json').read_text()),self.auth)

    def test_fat_staging_never_requires_hardlinks_or_chmod(self):
        bridge.prepare_handoff(self.root,confirmed=True)
        with patch.object(bridge.os,'link',side_effect=OSError('FAT has no hardlinks')),patch.object(bridge.os,'fchmod',side_effect=OSError('FAT has no chmod')):
            # The private Mac fence update still requires fchmod, unlike FAT staging.
            # Check FAT writer separately so its privacy limitations stay explicit.
            inbox=self.mount/'fat-inbox';inbox.mkdir()
            bridge._stage_write(inbox/'auth.json',self.device)
            self.assertEqual(json.loads((inbox/'auth.json').read_text()),self.device)
            with self.assertRaises(FileExistsError):bridge._stage_write(inbox/'auth.json',self.auth)

    def test_changed_old_pending_or_bad_mapping_never_exports(self):
        bridge.prepare_handoff(self.root,confirmed=True,preserve_pending=True)
        self.add_attempt()
        with self.assertRaises(ValueError):bridge.export_config(self.root,self.mount)
        self.assertFalse((self.mount/bridge.INBOX).exists())

    def test_symlink_inbox_and_wrong_firmware_refused(self):
        bridge.prepare_handoff(self.root,confirmed=True)
        (self.mount/bridge.INBOX).symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(ValueError):bridge.export_config(self.root,self.mount)
        (self.mount/bridge.INBOX).unlink();(self.mount/'system').mkdir();(self.mount/'system/version.txt').write_text('Kindle 5.12.2.2')
        with self.assertRaises(ValueError):bridge.export_config(self.root,self.mount)

    def test_status_never_trusts_nested_credentials(self):
        folder=self.mount/'.kindle-native-sync-export';folder.mkdir()
        (folder/'status.json').write_text(json.dumps({'state':'collecting','collector':{'auth':self.auth},'error':'SYNTHETIC-ACCESS','validation':{'device_validation':False,'token':'SYNTHETIC-ACCESS'},'capture':{'available':True,'reader_state':'idle','power_state':'awake','sealed_bytes':123,'sealed_files':1,'auth':self.auth}}))
        (folder/'service-probe.json').write_text(json.dumps({'process_count':1,'total_rss_kib':123,'collector_running':True,'processes':[{'pid':3,'rss_kib':123,'role':'collector','cmdline':'SYNTHETIC-ACCESS'}],'auth':self.auth}))
        value=bridge.status(self.root,self.mount)
        self.assertNotIn('SYNTHETIC-ACCESS',json.dumps(value))
        self.assertEqual(value['device_status']['capture']['sealed_bytes'],123)
        self.assertEqual(value['service_probe']['processes'],[{'pid':3,'rss_kib':123,'role':'collector'}])

    def test_worker_selftest_schema_is_visible_and_private_fields_are_redacted(self):
        folder=self.mount/'.kindle-native-sync-export';folder.mkdir()
        actual={'architecture':'arm','serial_prefix':'B0D4','supported_hardware':True,'supported_firmware':True,'kernel_release':'3.0.35-lab126','root':True,'auth':self.auth}
        result={'schema_version':1,'actual_device':actual,'private_filesystem_ok':True,'network_contacted':False,'acceptance_created':False}
        (folder/'selftest.json').write_text(json.dumps(result))
        value=bridge.status(self.root,self.mount)['self_test']
        self.assertTrue(value['actual_device']['supported_hardware']);self.assertEqual(value['actual_device']['serial_prefix'],'B0D4')
        self.assertTrue(value['private_filesystem_ok']);self.assertFalse(value['acceptance_created'])
        self.assertNotIn('SYNTHETIC-ACCESS',json.dumps(value))
        actual['serial_prefix']='B0D4FULLSERIAL'
        (folder/'selftest.json').write_text(json.dumps(result))
        self.assertNotIn('FULLSERIAL',json.dumps(bridge.status(self.root,self.mount)))

    def test_package_verifier_rejects_host_or_dynamic_binaries(self):
        spec=importlib.util.spec_from_file_location('package_check',HERE/'device/build/package.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        p=self.root/'binary';p.write_bytes(b'not an ELF')
        with self.assertRaises(ValueError):module.verify_elf(p)
        raw=bytearray(84);raw[:7]=b'\x7fELF\x01\x01\x01';struct.pack_into('<H',raw,18,40);struct.pack_into('<I',raw,28,52);struct.pack_into('<HH',raw,42,32,1);struct.pack_into('<I',raw,52,3);p.write_bytes(raw)
        with self.assertRaises(ValueError):module.verify_elf(p)
        struct.pack_into('<I',raw,52,1);p.write_bytes(raw);self.assertTrue(module.verify_elf(p)['static'])
        struct.pack_into('<I',raw,36,0x5000400);p.write_bytes(raw)
        with self.assertRaises(ValueError):module.verify_elf(p,soft_float=True)

    def test_device_scripts_parse_without_executing_them(self):
        for p in (HERE/'device/install').glob('*.sh'):
            subprocess.run(['sh','-n',str(p)],check=True,capture_output=True)
        subprocess.run(['sh','-n',str(HERE/'device/build/build.sh')],check=True,capture_output=True)
        cc=shutil.which('cc')
        if cc:subprocess.run([cc,'-std=c11','-Wall','-Wextra','-Werror','-fsyntax-only',str(HERE/'device/install/ksync-event-adapter.c')],check=True,capture_output=True)

    def test_zig_wrappers_preserve_flags_and_separate_calling_abi(self):
        import os,sys
        fake=self.root/'fake-zig'
        fake.write_text('#!'+sys.executable+'\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n');fake.chmod(0o700)
        env={**os.environ,'KSYNC_ZIG':str(fake)}
        for name,target in [('zig-soft-cc.py','arm-linux-musleabi'),('zig-musl-cc.py','arm-linux-musleabihf')]:
            args=[sys.executable,str(HERE/'device/build'/name),'-DNORMAL=1']
            if name=='zig-musl-cc.py':args.append('--target=arm-unknown-linux-musleabihf')
            r=subprocess.run(args,env=env,capture_output=True,text=True,check=True)
            actual=json.loads(r.stdout)
            self.assertEqual(actual,['cc','-target',target,'-mcpu=cortex_a9','-DNORMAL=1'])



class EventAdapterTests(unittest.TestCase):
    """Compile explicit host-test mode; fake LIPC helpers block on FIFOs, never real LIPC."""
    def setUp(self):
        import os
        self.os=os
        cc=shutil.which('cc')
        if not cc:self.skipTest('C compiler unavailable')
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.commands=self.base/'commands';self.commands.mkdir()
        self.root=self.base/'private';self.root.mkdir(mode=0o700)
        self.sources=['com.lab126.powerd','com.lab126.wifid','com.lab126.volumd','com.lab126.hal']
        self.fifos={};self.fds=[]
        for source in self.sources:
            p=self.base/source;os.mkfifo(p,0o600);self.fifos[source]=os.open(p,os.O_RDWR|os.O_NONBLOCK);self.fds.append(self.fifos[source])
        os.mkfifo(self.root/'control.fifo',0o600)
        self.control=os.open(self.root/'control.fifo',os.O_RDWR|os.O_NONBLOCK);self.fds.append(self.control)
        self.addCleanup(lambda:[os.close(fd) for fd in self.fds])
        for prop,value in [('state','active'),('cmState','CONNECTED'),('driveModeState','0'),('userstoreIsAvailable','1')]:
            (self.base/prop).write_text(value)
        import sys
        header='#!'+sys.executable+'\n'
        wait=header+'''import os,sys
from pathlib import Path
base=Path('''+repr(str(self.base))+''')
source=sys.argv[-2]
(base/(source+'.pid')).write_text(str(os.getpid()))
with (base/source).open() as stream:
 for line in stream:
  if line.strip()=='EXIT':break
  sys.stdout.write(line);sys.stdout.flush()
'''
        get="#!/bin/sh\nBASE="+str(self.base)+"\n"+'''
case "$2" in state|cmState|driveModeState|userstoreIsAvailable) ;; *) exit 1;; esac
printf '%s\n' "$2" >> "$BASE/queries"
IFS= read -r value < "$BASE/$2"
printf '%s\n' "$value"
'''
        for name,body in [('lipc-wait-event',wait),('lipc-get-prop',get)]:
            p=self.commands/name;p.write_text(body);p.chmod(0o700)
        self.binary=self.base/'adapter'
        subprocess.run([cc,'-std=c11','-Wall','-Wextra','-Werror','-DKSYNC_TESTING',str(HERE/'device/install/ksync-event-adapter.c'),'-o',str(self.binary)],check=True,capture_output=True)
        self.process=None;self.addCleanup(self.stop)

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait()

    def wait_for(self,predicate):
        import time
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            if predicate():return
            time.sleep(.02)
        self.fail('Timed out waiting for synthetic event adapter')

    def queries(self):
        p=self.base/'queries';return p.read_text().splitlines() if p.exists() else []

    def start(self):
        for source in self.sources:(self.base/(source+'.pid')).unlink(missing_ok=True)
        self.process=subprocess.Popen([str(self.binary),str(self.root),str(self.commands)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.wait_for(lambda:all((self.base/(s+'.pid')).exists() for s in self.sources))
        self.wait_for(lambda:len(self.queries())>=8)
        return [int((self.base/(s+'.pid')).read_text()) for s in self.sources]

    def send(self,source,line):self.os.write(self.fifos[source],line.encode())

    def alive(self,pid):
        try:self.os.kill(pid,0);return True
        except ProcessLookupError:return False

    def test_irrelevant_and_repeated_state_events_do_not_fork_getters(self):
        import time
        self.start();before=len(self.queries())
        self.send(self.sources[1],('signalStrength "4/5"\nscanComplete\n'*100))
        self.send(self.sources[3],'irrelevantHalEvent 1\n'*100)
        self.send(self.sources[0],'battLevelChanged 50\n'*100)
        time.sleep(.15)
        self.assertEqual(len(self.queries()),before)
        # Partial and coalesced lines must not trigger until a complete known event exists.
        self.send(self.sources[1],'cmDiscon');time.sleep(.04);self.assertEqual(len(self.queries()),before)
        self.send(self.sources[1],'nected\ncmConnected\n')
        self.wait_for(lambda:len(self.queries())==before+1)
        self.send(self.sources[1],'cmConnected\ncmStateChange "CONNECTED"\n'*30)
        time.sleep(.15);self.assertEqual(len(self.queries()),before+1)

    def test_each_listener_failure_is_conservative_and_reaped_on_restart(self):
        for i,source in enumerate(self.sources):
            with self.subTest(source=source):
                (self.base/'queries').unlink(missing_ok=True)
                pids=self.start()
                self.send(source,'EXIT\n')
                self.assertEqual(self.process.wait(timeout=5),1)
                self.assertTrue(all(not self.alive(pid) for pid in pids))
                # Drain emitted control states and verify conservative terminal transition.
                data=b''
                while True:
                    try:data+=self.os.read(self.control,8192)
                    except BlockingIOError:break
                self.assertIn(b'network 0\n',data);self.assertIn(b'usb 1\n',data);self.assertIn(b'power sleeping\n',data)

    def test_collector_fifo_replacement_forces_unchanged_state_snapshot(self):
        self.start();before=len(self.queries())
        # The adapter stays alive while a restarted collector replaces its FIFO.
        # Keeping the old read handle open makes this exercise the new pathname.
        old_pid=self.process.pid
        (self.root/'control.fifo').unlink();self.os.mkfifo(self.root/'control.fifo',0o600)
        fd=self.os.open(self.root/'control.fifo',self.os.O_RDWR|self.os.O_NONBLOCK);self.fds.append(fd)
        commands=self.os.open(self.root/'adapter-control.fifo',self.os.O_WRONLY|self.os.O_NONBLOCK)
        try:self.os.write(commands,b'refresh\n')
        finally:self.os.close(commands)
        self.wait_for(lambda:len(self.queries())==before+4)
        output=[]
        def complete():
            try:output.append(self.os.read(fd,8192))
            except BlockingIOError:pass
            return all(item in b''.join(output) for item in (b'power awake\n',b'network 1\n',b'usb 0\n'))
        self.wait_for(complete)
        self.assertTrue(b''.join(output).startswith(b'usb 1\npower sleeping\nnetwork 0\n'))
        self.assertEqual(self.process.pid,old_pid);self.assertIsNone(self.process.poll())
        self.assertEqual(len(self.queries()),before+4)

    def test_adapter_control_rejects_symlink_and_world_writable_fifo(self):
        path=self.root/'adapter-control.fifo'
        target=self.root/'other';target.write_text('do not change')
        path.symlink_to(target)
        self.process=subprocess.Popen([str(self.binary),str(self.root),str(self.commands)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.assertEqual(self.process.wait(timeout=3),1);self.assertEqual(target.read_text(),'do not change')
        path.unlink();self.os.mkfifo(path,0o600);path.chmod(0o666)
        self.process=subprocess.Popen([str(self.binary),str(self.root),str(self.commands)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.assertEqual(self.process.wait(timeout=3),1)



class BootstrapProbeTests(unittest.TestCase):
    def setUp(self):
        import os
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.commands=self.base/'commands';self.commands.mkdir()
        self.env={**os.environ,'PATH':str(self.commands)+':/bin:/usr/bin'}
        self.package=self.base/'package';(self.package/'bin').mkdir(parents=True);(self.package/'install').mkdir()
        self.out=self.base/'export';self.wifi=self.base/'wifi';self.wifi.write_text('CONNECTED\n')
        self.command('id',"printf '0\\n'\n")
        self.command('uname',"case \"$1\" in -m) printf 'armv7l\\n';; *) printf '3.0.35-lab126\\n';;esac\n")
        self.command('df',"printf 'Filesystem 1024-blocks Used Available Capacity Mounted\\n/dev/test 65536 12345 50000 20%% /var/local\\n'\n")
        self.command('date',"case \"$1\" in +%s) printf '1793578500\\n';; -u) printf '2026-11-02T00:15:00Z\\n';; *) printf '2026-11-02T08:15:00+0800\\n';;esac\n")
        self.command('lipc-get-prop',"case \"$2\" in state) printf 'active\\n';; cmState) cat "+str(self.wifi)+";; driveModeState) printf '0\\n';; userstoreIsAvailable) printf '1\\n';; *) exit 1;;esac\n")
        for name in ['initctl','lipc-wait-event','gzip','sha256sum','sync']:self.command(name,'exit 0\n')
        private=self.base/'private';private.mkdir()
        common=(HERE/'device/install/common.sh').read_text().replace('ROOT=/var/local/kindle-native-sync','ROOT='+str(private))
        (self.package/'install/common.sh').write_text(common+'\nfirmware_ok() { return 0; }\n')
        usid=self.base/'usid';usid.write_text('B0D4SYNTHETIC_FULL_SERIAL_NEVER_EXPORT')
        mem=self.base/'meminfo';mem.write_text('MemTotal: 254652 kB\n')
        mounts=self.base/'mounts';mounts.write_text('rootfs / rootfs rw 0 0\n/dev/root / ext3 ro,noatime 0 0\n')
        probe=(HERE/'device/install/probe.sh').read_text().replace('/proc/usid',str(usid)).replace('/proc/meminfo',str(mem)).replace('/proc/mounts',str(mounts))
        self.probe=self.package/'install/probe.sh';self.probe.write_text(probe)
        bootstrap=(HERE/'device/kual/bootstrap.sh').read_text().replace('PATH=/sbin:/usr/sbin:/bin:/usr/bin','PATH='+str(self.commands)+':/bin:/usr/bin').replace('PACKAGE=/mnt/us/kindle-native-sync-package','PACKAGE='+str(self.package)).replace('OUT=/mnt/us/.kindle-native-sync-export','OUT='+str(self.out))
        self.bootstrap=self.base/'bootstrap.sh';self.bootstrap.write_text(bootstrap)
        self.calls=self.base/'worker-calls'
        self.worker=self.package/'bin/ksync-worker'
        self.worker.write_text('#!/bin/sh\nprintf \"%s\\n\" \"$*\" >> '+str(self.calls)+"\nprintf '{\"schema_version\":1,\"acceptance_created\":false}\\n'\n")
        self.worker.chmod(0o700)

    def command(self,name,body):
        p=self.commands/name;p.write_text('#!/bin/sh\n'+body);p.chmod(0o700)

    def run_bootstrap(self,action):
        return subprocess.run(['sh',str(self.bootstrap),action],env=self.env,text=True,capture_output=True)

    def test_readonly_probe_reports_budget_clock_states_without_full_serial(self):
        r=subprocess.run(['sh',str(self.probe)],env=self.env,text=True,capture_output=True,check=True)
        value=json.loads(r.stdout)
        self.assertEqual((value['var_local_total_kib'],value['var_local_used_kib'],value['var_local_free_kib']),(65536,12345,50000))
        self.assertTrue(value['hardware_supported']);self.assertTrue(value['supported']);self.assertTrue(value['installation_supported'])
        self.assertEqual(value['mem_total_kib'],254652);self.assertEqual(value['root_mount_state'],'read_only')
        self.assertEqual(value['clock_local'],'2026-11-02T08:15:00+0800');self.assertEqual(value['clock_utc'],'2026-11-02T00:15:00Z')
        self.assertEqual(value['power_state'],'active');self.assertEqual(value['wifi_state'],'CONNECTED')
        self.assertEqual(value['usb_drive_mode'],'0');self.assertEqual(value['userstore_available'],'1')
        self.assertNotIn('SYNTHETIC_FULL_SERIAL',r.stdout)
        self.command('lipc-get-prop',"printf 'unexpected\"secret\\n'\n")
        r=subprocess.run(['sh',str(self.probe)],env=self.env,text=True,capture_output=True,check=True)
        value=json.loads(r.stdout)
        self.assertTrue(all(value[k]=='unknown' for k in ['power_state','wifi_state','usb_drive_mode','userstore_available']))
        self.assertNotIn('secret',r.stdout)

    def test_bootstrap_offline_and_explicit_online_flags_are_distinct(self):
        r=self.run_bootstrap('self-test');self.assertEqual(r.returncode,0,r.stderr)
        self.assertTrue((self.out/'selftest.json').is_file());self.assertNotIn('--online-self-test',self.calls.read_text())
        self.assertFalse((self.out/'selftest.json.partial').exists())
        r=self.run_bootstrap('tls-self-test');self.assertEqual(r.returncode,0,r.stderr)
        self.assertTrue((self.out/'tls-selftest.json').is_file());self.assertIn('--online-self-test',self.calls.read_text())
        self.assertFalse(json.loads((self.out/'tls-selftest.json').read_text())['acceptance_created'])
        before=self.calls.read_text();self.wifi.write_text('NA\n')
        r=self.run_bootstrap('tls-self-test');self.assertNotEqual(r.returncode,0)
        self.assertEqual(self.calls.read_text(),before)
        self.assertFalse(json.loads((self.out/'tls-selftest.json').read_text())['network_contacted'])

    def test_candidate_failure_is_exported_without_raising_validation(self):
        self.worker.write_text('#!/bin/sh\nexit 139\n')
        r=self.run_bootstrap('self-test');self.assertEqual(r.returncode,0,r.stderr)
        value=json.loads((self.out/'selftest.json').read_text())
        self.assertEqual(value['worker_exit_code'],139);self.assertFalse(value['acceptance_created'])
        self.assertFalse((self.out/'selftest.json.partial').exists())

    def test_install_outcome_survives_reconnection_without_enabling_upload(self):
        script=self.package/'install/install.sh'
        for code,expected in [(0,'installed_collection_only'),(19,'installation_failed')]:
            script.write_text('#!/bin/sh\nexit '+str(code)+'\n')
            r=self.run_bootstrap('install');self.assertEqual(r.returncode,0,r.stderr)
            value=json.loads((self.out/'install-result.json').read_text())
            self.assertEqual(value['state'],expected);self.assertEqual(value['exit_code'],code)
            self.assertFalse(value['auto_upload_enabled'])
            self.assertFalse((self.out/'install-result.json.partial').exists())

    def test_service_probe_covers_descendants_without_exporting_process_arguments(self):
        proc=self.base/'proc';proc.mkdir();(proc/'uptime').write_text('1800.25 3.5\n')
        for pid,parent,rss in [(10,1,100),(20,1,200),(30,20,300),(40,1,9999),(50,40,9999)]:
            folder=proc/str(pid);folder.mkdir()
            fields=[str(pid),'(synthetic process (safe))','S',str(parent)]+['0']*20
            fields[13]='10';fields[14]='2';fields[21]='100'
            (folder/'stat').write_text(' '.join(fields)+'\n')
            (folder/'status').write_text('VmRSS: '+str(rss)+' kB\nPRIVATE_SYNTHETIC_ACCESS_TOKEN\n')
            (folder/'cmdline').write_text('PRIVATE_SYNTHETIC_ACCESS_TOKEN')
        self.command('initctl', 'case "$2" in kindle-native-sync) echo "job start/running, process 10";; *) echo "job start/running, process 20";;esac\n')
        script=self.package/'install/service-probe.sh'
        script.write_text((HERE/'device/install/service-probe.sh').read_text().replace('/proc/',str(proc)+'/'))
        with (self.package/'install/common.sh').open('a') as f:f.write('\nrequire_device() { return 0; }\n')
        r=subprocess.run(['sh',str(script)],env=self.env,text=True,capture_output=True,check=True)
        value=json.loads(r.stdout)
        self.assertEqual({p['pid'] for p in value['processes']},{10,20,30})
        self.assertEqual(value['total_rss_kib'],600);self.assertEqual(value['process_count'],3)
        self.assertTrue(value['rss_complete']);self.assertTrue(value['collector_running']);self.assertTrue(value['adapter_running'])
        self.assertEqual(value['uptime_seconds'],1800);self.assertFalse(value['acceptance_created'])
        self.assertNotIn('PRIVATE',r.stdout);self.assertTrue(all(p['cpu_ticks']==12 for p in value['processes']))

    def test_low_space_worker_failure_still_exports_fresh_probe_and_service_state(self):
        private=self.base/'private';(private/'bin').mkdir()
        worker=private/'bin/ksync-worker';worker.write_text('#!/bin/sh\nexit 17\n');worker.chmod(0o700)
        with (self.package/'install/common.sh').open('a') as f:f.write('\nrequire_device() { return 0; }\n')
        for name in ('probe.sh','service-probe.sh'):
            script=self.package/'install'/name;script.write_text('#!/bin/sh\necho \'{"schema_version":1,"fresh":true}\'\n');script.chmod(0o700)
        control=self.package/'install/control.sh'
        control.write_text((HERE/'device/install/control.sh').read_text().replace('out=/mnt/us/.kindle-native-sync-export','out='+str(self.out)))
        subprocess.run(['sh',str(control),'export'],env=self.env,text=True,capture_output=True,check=True)
        self.assertTrue(json.loads((self.out/'probe.json').read_text())['fresh'])
        self.assertTrue(json.loads((self.out/'service-probe.json').read_text())['fresh'])
        value=json.loads((self.out/'status.json').read_text())
        self.assertEqual(value['state'],'worker_status_unavailable');self.assertEqual(value['worker_exit_code'],17)
        self.assertFalse(value['auto_sync_enabled'])

if __name__=='__main__':unittest.main()
