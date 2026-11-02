"""Synthetic collector integration tests; never touches a Kindle or network."""
import gzip
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest

BINARY=Path(__file__).resolve().parent/'ksync-collector'

class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name);self.root=self.path/'state';self.root.mkdir(mode=0o700)
        self.logs=self.path/'logs';self.logs.mkdir();self.rotated=self.path/'rotated';self.rotated.mkdir()
        self.log=self.logs/'messages';self.log.write_text('old irrelevant log\n')
        self.proc=None;self.error=(self.path/'stderr').open('w+');self.addCleanup(self.error.close)
        self.start()
    def tearDown(self):self.stop()
    def wait(self,predicate,seconds=4):
        until=time.monotonic()+seconds
        while time.monotonic()<until:
            if self.proc and self.proc.poll() is not None:
                self.error.seek(0);self.fail('collector exited: '+self.error.read())
            try:
                if predicate():return
            except (FileNotFoundError,json.JSONDecodeError):pass
            time.sleep(.025)
        self.error.seek(0);self.fail('timeout: '+self.error.read())
    def start(self,*extra):
        self.proc=subprocess.Popen([str(BINARY),'--root',str(self.root),'--log',str(self.log),'--rotated-dir',str(self.rotated),'--poll-ms','40','--reserve-bytes','0',*extra],stdout=subprocess.DEVNULL,stderr=self.error)
        self.wait(lambda:(self.root/'runtime.json').exists())
        time.sleep(.07)
    def stop(self,hard=False):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL if hard else signal.SIGTERM);self.proc.wait(timeout=3)
        self.proc=None
    def event(self,value):
        subprocess.run([str(BINARY),'--root',str(self.root),'--emit',value],check=True)
        time.sleep(.08)
    def line(self,event='NextPage',tag='ReadingTimerController',suffix='TotalTime:20000,TotalWords:100;'):
        return time.strftime('%y%m%d:%H%M%S')+' cvm: I '+tag+':Information::'+event+','+suffix+'\n'
    def append(self,text):
        with self.log.open('a') as f:f.write(text)
    def data(self):
        return ''.join(p.read_text() for p in sorted((self.root/'events').rglob('*.log')))
    def state(self):return json.loads((self.root/'runtime.json').read_text())
    def open_book(self):
        self.append(self.line('OpenBook'));self.wait(lambda:self.state()['reader_state']=='active')
    def test_no_history_and_no_initial_mid_book_capture(self):
        self.append(self.line());time.sleep(.12);self.assertEqual(self.data(),'')
        self.open_book();self.append(self.line());self.append(self.line('CloseBook'))
        self.wait(lambda:self.state()['reader_state']=='idle');self.assertIn('OpenBook',self.data())
        self.assertTrue(list((self.root/'events'/'sealed').glob('segment-*.log')))
    def test_partial_line_completed_once(self):
        self.open_book();line=self.line(suffix='TotalTime:21000,TotalWords:101;')
        self.append(line[:28]);time.sleep(.1);self.assertNotIn('21000',self.data())
        self.append(line[28:]);self.wait(lambda:'21000' in self.data());self.assertEqual(self.data().count('21000'),1)
    def test_rename_rotation_and_copy_truncate(self):
        self.open_book();first=self.line(suffix='TotalTime:22000,TotalWords:102;');self.append(first)
        self.wait(lambda:'22000' in self.data());self.log.rename(self.rotated/'messages_001.log');self.log.write_text(self.line(suffix='TotalTime:23000,TotalWords:103;'))
        self.wait(lambda:'23000' in self.data());self.assertEqual(self.data().count('22000'),1)
        self.log.write_text(self.line('CloseBook',suffix=''))
        self.wait(lambda:self.state()['reader_state']=='idle');self.assertIn('CloseBook',self.data())
    def test_restart_replays_partial_line_and_keeps_bootstrap(self):
        self.open_book();boot=self.state()['bootstrap_unix'];line=self.line(suffix='TotalTime:24000,TotalWords:104;')
        self.append(line[:31]);time.sleep(.1);self.stop();self.append(line[31:]);self.start()
        self.wait(lambda:'24000' in self.data());self.assertEqual(self.data().count('24000'),1)
        self.assertEqual(self.state()['bootstrap_unix'],boot);self.assertEqual(self.state()['power_state'],'unknown')
    def test_hard_restart_does_not_duplicate_durable_lines(self):
        self.open_book();self.append(self.line('CloseBook',suffix='TotalTime:25000;'))
        self.wait(lambda:bool(list((self.root/'events'/'sealed').glob('*.log'))));self.stop(hard=True);self.start()
        time.sleep(.15);self.assertEqual(self.data().count('25000'),1)
    def test_quota_halts_without_deleting_queue_and_manual_retry(self):
        self.stop();self.start('--queue-bytes','500');self.open_book()
        for i in range(20):self.append(self.line(suffix=f'TotalTime:{26000+i},'+('x'*50)))
        self.wait(lambda:self.state()['capture_blocked']);self.assertLessEqual(sum(p.stat().st_size for p in (self.root/'events').rglob('*.log')),500)
        self.assertTrue(self.data());self.assertTrue((self.root/'collector.checkpoint').exists())
    def test_reserve_and_overflow_recovery_are_explicit(self):
        self.event('rescan now');self.assertTrue(self.state()['coverage_gap'])
        self.event('maintenance acknowledge-gap');self.assertFalse(self.state()['coverage_gap'])
        self.stop();self.start('--reserve-bytes',str(1<<60));self.append(self.line('OpenBook'))
        self.wait(lambda:self.state()['capture_blocked']);self.assertEqual(self.data(),'')
        self.assertEqual(self.state()['reader_state'],'unknown')

    def test_long_page_never_marks_reader_idle(self):
        self.open_book();self.event('power awake');self.event('network 1');time.sleep(.2)
        self.assertEqual(self.state()['reader_state'],'active');self.assertEqual(self.state()['idle_since_unix'],0)
    def test_sleep_usb_and_network_control(self):
        self.open_book();self.event('power awake');self.event('network 1');self.event('power sleeping')
        self.assertEqual(self.state()['power_state'],'sleeping');self.assertTrue(list((self.root/'events'/'sealed').glob('*.log')))
        self.event('usb 1');before=self.data();self.append(self.line(suffix='TotalTime:27000;'));time.sleep(.15);self.assertEqual(before,self.data())
        self.event('usb 0');self.wait(lambda:'27000' in self.data());self.assertFalse(self.state()['usb_export'])
    def test_storage_guards_fresh_space_rounding_metadata_floor_and_cancellation(self):
        source=Path(__file__).resolve().parent/'test_storage.c'
        unit=self.path/'storage-tests'
        subprocess.run([os.environ.get('CC','cc'),'-O2','-std=c11','-Wall','-Wextra','-Werror',str(source),'-o',str(unit)],check=True)
        home=self.path/'storage-unit';home.mkdir(mode=0o700)
        result=subprocess.run([str(unit),str(home)],capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('cancellation: passed',result.stdout)

    def test_startup_and_respawn_request_adapter_state_replay(self):
        path=self.root/'adapter-control.fifo';os.mkfifo(path,0o600)
        fd=os.open(path,os.O_RDWR|os.O_NONBLOCK);self.addCleanup(os.close,fd)
        boot=self.state()['bootstrap_unix']
        self.stop();self.start()
        self.assertEqual(os.read(fd,100),b'refresh\n')
        self.assertEqual(self.state()['power_state'],'unknown');self.assertFalse(self.state()['network_connected'])
        self.stop(hard=True);self.start()
        self.assertEqual(os.read(fd,100),b'refresh\n')
        self.assertEqual(self.state()['bootstrap_unix'],boot)
        self.assertEqual(self.state()['reader_state'],'unknown')
    def test_adapter_handshake_rejects_symlinks_and_loose_permissions(self):
        channel=self.root/'adapter-control.fifo';other=self.root/'real-adapter.fifo'
        os.mkfifo(other,0o600);fd=os.open(other,os.O_RDWR|os.O_NONBLOCK);self.addCleanup(os.close,fd)
        channel.symlink_to(other);self.stop();self.start()
        with self.assertRaises(BlockingIOError):os.read(fd,100)
        channel.unlink();os.mkfifo(channel,0o600);channel.chmod(0o640)
        fd2=os.open(channel,os.O_RDWR|os.O_NONBLOCK);self.addCleanup(os.close,fd2)
        self.stop();self.start()
        with self.assertRaises(BlockingIOError):os.read(fd2,100)

    def test_single_instance_lock(self):
        r=subprocess.run([str(BINARY),'--root',str(self.root),'--log',str(self.log),'--poll-ms','40'],capture_output=True,timeout=2)
        self.assertNotEqual(r.returncode,0);self.assertIn(b'already running',r.stderr)
    def booklet(self,message):
        return time.strftime('%y%m%d:%H%M%S')+' cvm[3982]: I ReaderBooklet:Information::'+message+'\n'
    def test_pw2_booklet_lifecycle_and_isolated_end_do_not_invent_idle(self):
        self.open_book();self.append(self.booklet('ReaderBooklet start'))
        sdk=time.strftime('%y%m%d:%H%M%S')+' cvm[3982]: I ReaderSDKImpl:Information::Closing book in controller due to low memory/registration change events/reader sync events\n'
        self.append(sdk);self.append(self.booklet('End of stop method'));time.sleep(.13)
        self.assertEqual(self.state()['reader_state'],'active')
        self.append(self.booklet('ReaderBooklet stop'));self.append(self.booklet('ReaderBooklet start'))
        self.append(self.booklet('End of stop method'));time.sleep(.13)
        self.assertEqual(self.state()['reader_state'],'active')
        self.append(self.booklet('ReaderBooklet stop'));self.append(self.booklet('End of stop method'))
        self.wait(lambda:self.state()['reader_state']=='idle')
        self.assertTrue(list((self.root/'events'/'sealed').glob('*.log')))
        self.assertIn('ReaderBooklet stop',self.data());self.assertIn('End of stop method',self.data())
    def test_lifecycle_tokens_inside_title_are_not_reader_events(self):
        self.append(self.line('OpenBook',suffix='StoredBookData:null,Title:CloseBook,Volume One;CloseBook,Volume Two;'))
        self.wait(lambda:self.state()['reader_state']=='active')
        self.append(self.line('NextPage',suffix='Title:CloseBook,Volume One;CloseBook,Volume Two;'))
        time.sleep(.13);self.assertEqual(self.state()['reader_state'],'active')
    def test_more_than_256_rotations_reap_replaced_inodes_without_false_gap(self):
        self.open_book()
        for i in range(270):
            value=str(40000+i);self.append(self.line(suffix='TotalTime:'+value+';'))
            self.wait(lambda:value in self.data())
            old=self.rotated/'messages_rolling.log';self.log.rename(old)
            self.log.write_text('')
            packed=self.rotated/'messages_rolling.log.gz'
            with gzip.open(packed,'wb') as f:f.write(old.read_bytes())
            old.unlink();packed.unlink()
        self.append(self.line('NextPage',suffix='TotalTime:49999;'))
        self.wait(lambda:'49999' in self.data())
        self.assertFalse(self.state()['capture_blocked']);self.assertFalse(self.state()['coverage_gap'])
        for i in range(270):self.assertEqual(self.data().count('TotalTime:'+str(40000+i)+';'),1)
        cursors=(self.root/'collector.checkpoint').read_text().splitlines()[1:]
        self.assertLess(len(cursors),5)

    def test_utc_and_buffered_identity_after_close_are_retained(self):
        self.open_book();self.append(self.line('CloseBook'));self.wait(lambda:self.state()['reader_state']=='idle')
        self.append(self.line(tag='JSONJournalSaver',suffix='annotationType=LastPositionReadAnnotation,modificationDate=2026-11-01T14:00:00Z'))
        self.append(self.line(tag='BufferedLogger',suffix='TotalTime:28000;'));self.wait(lambda:'28000' in self.data())
        self.assertIn('modificationDate',self.data())
    def test_gzip_rotation_catchup_on_idle(self):
        self.open_book();self.append(self.line('CloseBook'));self.wait(lambda:self.state()['reader_state']=='idle')
        with gzip.open(self.rotated/'messages_002.log.gz','wt') as f:f.write(self.line(tag='BufferedLogger',suffix='TotalTime:29000;'))
        self.wait(lambda:'29000' in self.data());time.sleep(.2);self.assertEqual(self.data().count('29000'),1)
    def test_worker_requires_explicit_idle_twenty_seconds_and_fresh_runtime(self):
        binpath=self.root/'bin';binpath.mkdir();worker=binpath/'ksync-worker'
        calls=self.root/'worker-calls'
        worker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "'+str(calls)+'"\n')
        worker.chmod(0o700)
        self.open_book();self.append(self.booklet('ReaderBooklet start'))
        self.event('power awake');self.event('network 0')
        self.append(self.booklet('ReaderBooklet stop'));self.append(self.booklet('End of stop method'))
        self.wait(lambda:self.state()['reader_state']=='idle')
        time.sleep(.2);self.assertFalse(calls.exists())
        self.wait(lambda:calls.exists(),seconds=23)
        self.assertTrue(calls.read_text().strip().endswith(' collect'))
        self.assertLess(time.time()-(self.root/'runtime.json').stat().st_mtime,3)
        self.event('network 1');self.wait(lambda:' sync' in calls.read_text(),seconds=3)
        self.assertEqual(len(calls.read_text().splitlines()),2)

    def test_worker_is_not_started_when_state_unknown_or_active(self):
        binpath=self.root/'bin';binpath.mkdir();worker=binpath/'ksync-worker';worker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "'+str(self.root/'worker-calls')+'"\n');worker.chmod(0o700)
        self.event('power awake');self.event('network 1');time.sleep(.15);self.assertFalse((self.root/'worker-calls').exists())
        self.open_book();time.sleep(.2);self.assertFalse((self.root/'worker-calls').exists())

if __name__=='__main__':unittest.main(verbosity=2)
