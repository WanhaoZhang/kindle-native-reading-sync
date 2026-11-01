"""USB configuration handoff only. No jailbreak, device deployment, or network calls."""
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import tempfile
import time

from weread_readonly import load_auth, BridgeError

PROFILE = 'pw2-5.12.2.1.1'
FENCE = 'device-handoff.json'
INBOX = '.kindle-native-sync-inbox'
UNRESOLVED = ('dispatching', 'unknown', 'needs_review', 'failed')


def _sha(raw): return hashlib.sha256(raw).hexdigest()
def _encoded(value): return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)+'\n').encode()


def _directory(path):
    path = Path(path)
    if path.is_symlink(): raise ValueError('目录不能是符号链接。')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _read(path, max_bytes=1024*1024):
    if path.is_symlink(): raise ValueError('拒绝符号链接。')
    if path.stat().st_size>max_bytes:raise ValueError('文件超出允许大小。')
    raw = path.read_bytes()
    if len(raw)>max_bytes: raise ValueError('文件超出允许大小。')
    return json.loads(raw)


def _write(path, value, exclusive=False):
    _directory(path.parent)
    if path.is_symlink(): raise ValueError('目标不能是符号链接。')
    fd, name = tempfile.mkstemp(prefix='.handoff-',dir=path.parent)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'wb') as stream:
            stream.write(_encoded(value));stream.flush();os.fsync(stream.fileno())
        if exclusive: os.link(name,path)
        else: os.replace(name,path)
    finally: Path(name).unlink(missing_ok=True)


def _stage_write(path,value):
    """FAT-safe exclusive staging; manifest is written last as the completeness marker.

    FAT has neither hard links nor enforceable chmod privacy. Credential exposure
    is explicit in the API warning; only the device's later private import protects it.
    """
    flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0)
    fd=os.open(path,flags,0o600)
    with os.fdopen(fd,'wb') as stream:
        stream.write(_encoded(value));stream.flush();os.fsync(stream.fileno())


def probe_usb(mount='/Volumes/Kindle'):
    """Read only safe device metadata. Never reads /proc/usid or full serial numbers."""
    mount=Path(mount)
    connected=mount.is_dir() and (mount/'documents').is_dir() and not mount.is_symlink() and not (mount/'documents').is_symlink()
    answer={'connected':connected,'supported_profile':PROFILE,'root_probe':None,
            'jailbreak_performed':False,'installed_by_this_call':False}
    if not connected:return answer
    version=mount/'system/version.txt'
    if version.is_file() and not version.is_symlink():
        import re
        raw=version.read_bytes()[:8192].decode('utf-8',errors='replace')
        matches=re.findall(r'\b5\.\d+(?:\.\d+){1,5}\b',raw)
        answer['firmware']=matches[0] if matches else None
        answer['firmware_supported']=answer['firmware']=='5.12.2.1.1'
    folder=mount/'.kindle-native-sync-export'
    probe=folder/'probe.json'
    if probe.is_file() and not folder.is_symlink():
        value=_read(probe)
        # Treat USB output as untrusted; return only documented non-credential fields.
        keys=('schema_version','profile','firmware','model_prefix','kernel','machine','libc',
              'root','upstart','lipc','gzip','var_local_free_kib','supported')
        answer['root_probe']={k:value[k] for k in keys if k in value and
                              isinstance(value[k],(str,int,bool,type(None)))}
        import re
        for k in ('profile','firmware','model_prefix','kernel','machine','libc'):
            v=answer['root_probe'].get(k)
            limit=4 if k=='model_prefix' else 100
            if not isinstance(v,str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,'+str(limit)+'}',v):
                answer['root_probe'].pop(k,None)
    return answer


@contextmanager
def _locked_ledger(private):
    path=private/'sync-engine.sqlite'
    if path.is_symlink():raise ValueError('账本不能是符号链接。')
    db=sqlite3.connect(path,timeout=10)
    try:
        db.execute('BEGIN IMMEDIATE')
        yield db
        db.commit()
    except BaseException:db.rollback();raise
    finally:db.close()


def _unresolved(db, private, preserve_pending=False):
    pending=[]
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'plans' in tables and db.execute("SELECT COUNT(*) FROM plans WHERE state='committing'").fetchone()[0]:
        raise ValueError('Mac 正在提交，等待结束后才能交接。')
    if 'attempts' in tables:
        pending=[{'ledger':'sync-engine','id':r[0],'state':r[1],'seconds':r[2]} for r in db.execute(
            "SELECT batch_id,state,seconds FROM attempts WHERE state IN (?,?,?,?)",UNRESOLVED)]
        if any(r['state']=='dispatching' for r in pending):raise ValueError('Mac 提交仍处于发送状态，不能交接。')
    old=private/'upload-ledger.sqlite'
    if old.exists():
        if old.is_symlink():raise ValueError('旧账本不能是符号链接。')
        with closing(sqlite3.connect('file:'+str(old)+'?mode=ro',uri=True)) as legacy:
            try:
                old_pending=[{'ledger':'legacy','id':r[0],'state':r[1],'seconds':r[2]} for r in legacy.execute(
                    "SELECT sample_id,state,seconds FROM attempts WHERE state != 'confirmed'")]
            except sqlite3.Error:raise ValueError('无法核验旧上传账本。') from None
            if any(r['state']=='dispatching' for r in old_pending):raise ValueError('旧试传仍在发送，不能交接。')
            pending.extend(old_pending)
    if pending and not preserve_pending:raise ValueError('Mac 账本有未核验提交；需明确选择保留旧待核验后交接。')
    return pending


def prepare_handoff(project_root, *, confirmed=False, preserve_pending=False, now_unix=None):
    """Fence Mac writes before exporting a fresh-device credential; not reversible automatically."""
    if confirmed is not True:raise ValueError('交接会停用此 Mac 实例的写入；必须明确确认。')
    private=_directory(Path(project_root).resolve()/'.weread')
    with _locked_ledger(private) as db:
        pending=_unresolved(db,private,preserve_pending)
        fence_path=private/FENCE
        if fence_path.exists():return _read(fence_path)
        auth_path=private/'auth.json'
        auth=load_auth(auth_path) if auth_path.exists() else None
        cutoff=int(time.time()) if now_unix is None else now_unix
        if type(cutoff) is not int or cutoff<=0:raise ValueError('起始时间无效。')
        fence={'schema_version':1,'kind':'device-upload-fence','state':'prepared',
               'handoff_id':secrets.token_hex(16),'bootstrap_cutoff_unix':cutoff,
               'account_scope':_sha(auth['vid'].encode()) if auth else None,
               'mac_device_hash':_sha(auth['deviceId'].encode()) if auth else None,
               'created_at':datetime.now(timezone.utc).isoformat(),
               'preserve_pending':preserve_pending,'old_pending_count':len(pending),
               'old_pending_sha256':_sha(_encoded(pending)),
               'note':'设备只从交接后采集；不重新上传旧日志。Mac写入保持封锁，需人工核验后才能解除。'}
        if pending:_write(private/'handoff-preserved-pending.json',pending,exclusive=True)
        _write(fence_path,fence,exclusive=True)
        return fence


def export_config(project_root, mount, *, credentials_path=None, mappings=None):
    """Stage private config on USB only after explicit local fencing. FAT staging is NOT private."""
    root=Path(project_root).resolve();private=root/'.weread';mount=Path(mount)
    probe=probe_usb(mount)
    if not probe['connected']:raise ValueError('Kindle 未连接。')
    if probe.get('firmware_supported') is False:raise ValueError('设备固件不匹配，拒绝导出配置。')
    fence_path=private/FENCE
    with _locked_ledger(private) as db:
        fence=_read(fence_path)
        pending=_unresolved(db,private,fence.get('preserve_pending') is True)
        if _sha(_encoded(pending))!=fence.get('old_pending_sha256'):raise ValueError('旧待核验记录变化，请核对交接。')
        if fence.get('kind')!='device-upload-fence' or fence.get('state') not in ('prepared','exported'):
            raise ValueError('必须先交接 Mac 上传权限。')
        auth_path=Path(credentials_path) if credentials_path else private/'device-auth.json'
        auth=load_auth(auth_path)
        account=_sha(auth['vid'].encode())
        if fence.get('account_scope') and fence['account_scope']!=account:raise ValueError('设备账号与交接账号不一致。')
        if fence.get('mac_device_hash')==_sha(auth['deviceId'].encode()):raise ValueError('必须为设备单独扫码，不能复用 Mac deviceId。')
        rows=mappings
        if rows is None:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            rows=[dict(local_book_id=r[0],weread_book_id=r[1],title=r[2] or '') for r in db.execute(
                'SELECT source_key,weread_id,title FROM mappings WHERE account=?',(account,))] if 'mappings' in tables else []
        if not isinstance(rows,list):raise ValueError('书籍映射须为列表。')
        cleaned=[];seen=set()
        for r in rows:
            if not isinstance(r,dict) or not all(isinstance(r.get(k),str) and r[k] and '\x00' not in r[k] for k in ('local_book_id','weread_book_id')):
                raise ValueError('书籍映射格式无效。')
            if r['local_book_id'] in seen:raise ValueError('重复书籍映射。')
            seen.add(r['local_book_id']);cleaned.append({k:r.get(k,'') for k in ('local_book_id','weread_book_id','title')})
        config={'schema_version':1,'device_id':auth['deviceId'],'account_vid':auth['vid'],
                'profile':PROFILE,'firmware':'5.12.2.1.1','timezone':'Asia/Shanghai',
                'bootstrap_cutoff_unix':fence['bootstrap_cutoff_unix'],'mappings':cleaned,
                'validation':{'protocol_validation':False,'device_validation':False,'binding_sha256':''}}
        inbox=mount/INBOX
        if inbox.exists():raise ValueError('USB暂存目录已存在；先在设备完成导入，勿覆盖凭据。')
        _directory(inbox)
        try:
            _stage_write(inbox/'config.json',config)
            _stage_write(inbox/'auth.json',auth)
            manifest={'schema_version':1,'kind':'kindle-native-sync-handoff',
                      'config_sha256':_sha((inbox/'config.json').read_bytes()),
                      'auth_sha256':_sha((inbox/'auth.json').read_bytes())}
            _stage_write(inbox/'handoff.json',manifest)
        except BaseException:
            # Remove only files created by this call, never arbitrary existing user data.
            for name in ('handoff.json','auth.json','config.json'):(inbox/name).unlink(missing_ok=True)
            inbox.rmdir();raise
        fence.update(state='exported',account_scope=account,device_hash=_sha(auth['deviceId'].encode()))
        _write(fence_path,fence)
    return {'state':'exported','inbox':str(inbox),'mappings_count':len(cleaned),'auto_upload_enabled':False,
            'warning':'USB/FAT 暂存不具备文件权限保护。安全弹出后立即在 Kindle 导入；设备成功导入后会删除暂存凭据。'}


def status(project_root, mount='/Volumes/Kindle'):
    private=Path(project_root).resolve()/'.weread';fence_path=private/FENCE
    fence=_read(fence_path) if fence_path.exists() else None
    value={'usb':probe_usb(mount),'handoff':fence,'mac_upload_fenced':bool(fence and fence.get('state') in ('prepared','exported','active')),
           'device_status':None}
    path=Path(mount)/'.kindle-native-sync-export/status.json'
    if path.is_file() and not path.parent.is_symlink():
        exported=_read(path)
        # Only expose this wrapper after the worker provides its sanitized status command.
        value['device_status']={k:exported[k] for k in ('schema_version','pending_seconds','confirmed_seconds','unknown_batches')
                                if type(exported.get(k)) is int and exported[k]>=0}
        import re
        if isinstance(exported.get('state'),str) and re.fullmatch(r'[a-z_]{1,40}',exported['state']):
            value['device_status']['state']=exported['state']
        if isinstance(exported.get('validation'),dict):
            value['device_status']['validation']={k:exported['validation'][k] for k in ('protocol_validation','device_validation') if type(exported['validation'].get(k)) is bool}
        for key in ('configured','collect_only_default','auto_sync_enabled'):
            if type(exported.get(key)) is bool:value['device_status'][key]=exported[key]
        if isinstance(exported.get('states'),dict):
            value['device_status']['states']={
                key:{field:counts[field] for field in ('sessions','seconds') if type(counts.get(field)) is int and counts[field]>=0}
                for key,counts in exported['states'].items()
                if isinstance(key,str) and re.fullmatch(r'[a-z_]{1,40}',key) and isinstance(counts,dict)}
    return value
