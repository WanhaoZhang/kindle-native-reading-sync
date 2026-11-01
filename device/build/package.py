#!/usr/bin/env python3
"""Verify target ELFs and assemble a source-auditable, checksummed Kindle package."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess


def verify_elf(path, *, soft_float=False):
    raw=Path(path).read_bytes()
    if len(raw)<52 or raw[:7]!=b'\x7fELF\x01\x01\x01':raise ValueError('Require a 32-bit little-endian ELF: '+Path(path).name)
    if struct.unpack_from('<H',raw,18)[0]!=40:raise ValueError('Require ARM machine, never a host binary: '+Path(path).name)
    flags=struct.unpack_from('<I',raw,36)[0]
    if soft_float and flags&0x400:raise ValueError('C helper must use soft-float calling ABI.')
    off=struct.unpack_from('<I',raw,28)[0];size,count=struct.unpack_from('<HH',raw,42)
    if size<32 or off+size*count>len(raw):raise ValueError('Malformed program headers.')
    if any(struct.unpack_from('<I',raw,off+i*size)[0] in (2,3) for i in range(count)):
        raise ValueError('Device helpers must be fully static (no PT_DYNAMIC / PT_INTERP).')
    return {'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'machine':'ARM','elf_class':32,'static':True}


def main():
    a=argparse.ArgumentParser();a.add_argument('--collector',required=True);a.add_argument('--worker',required=True);a.add_argument('--adapter',required=True);a.add_argument('--out',required=True)
    a.add_argument('--c-profile',choices=['niluje-kindlepw2','experimental-static-musl'],default='niluje-kindlepw2')
    args=a.parse_args();root=Path(__file__).resolve().parents[2];out=Path(args.out).resolve()
    inputs={'ksync-collector':args.collector,'ksync-worker':args.worker,'ksync-event-adapter':args.adapter}
    manifest={'schema_version':1,'profile':'pw2-5.12.2.1.1','firmware':'5.12.2.1.1','device_tested':False,'upload_enabled':False,'c_toolchain_profile':args.c_profile,'binaries':{}}
    source_paths = sorted(set(
        list((root/'device').rglob('*.c')) + list((root/'device').rglob('*.rs')) +
        list((root/'device/install').rglob('*.sh')) + list((root/'device/install/upstart').glob('*.conf')) +
        [root/'device/worker/Cargo.toml', root/'device/worker/Cargo.lock']))
    # Generated Cargo build sources are not the corresponding project source.
    source_paths = [p for p in source_paths if 'target' not in p.relative_to(root).parts]
    manifest['source_files_sha256'] = {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}
    commit = subprocess.run(['git','rev-parse','HEAD'],cwd=root,capture_output=True,text=True)
    manifest['source_commit'] = commit.stdout.strip() if commit.returncode == 0 else None
    # Verify every binary before creating a package that could be mistaken for installable.
    for name,p in inputs.items():manifest['binaries'][name]=verify_elf(p,soft_float=name!='ksync-worker')
    if out.exists():raise ValueError('Refuse to overwrite a package; select a new --out path.')
    out.mkdir(parents=True);(out/'bin').mkdir();(out/'install').mkdir();(out/'upstart').mkdir()
    for name,p in inputs.items():shutil.copy2(p,out/'bin'/name);(out/'bin'/name).chmod(0o700)
    for p in (root/'device/install').glob('*.sh'):shutil.copy2(p,out/'install'/p.name)
    for p in (root/'device/install/upstart').glob('*.conf'):shutil.copy2(p,out/'upstart'/p.name)
    shutil.copytree(root/'device/kual',out/'kual')
    shutil.copy2(root/'LICENSE',out/'LICENSE')
    shutil.copy2(root/'THIRD_PARTY_NOTICES.md',out/'THIRD_PARTY_NOTICES.md')
    shutil.copytree(root/'licenses',out/'licenses')
    for name in ('readinglog','kindle-reading-dashboard'):
        shutil.copy2(root/'vendor'/name/'LICENSE',out/'licenses'/(name+'-LICENSE.txt'))
    ref = manifest['source_commit'] or 'main'
    (out/'SOURCE.txt').write_text('Corresponding source and build instructions:\nhttps://github.com/WanhaoZhang/kindle-native-reading-sync/tree/'+ref+'\nVerify source_files_sha256 in manifest.json against the source tree used to build this candidate.\nThis package is experimental, not device-validated.\n')
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    (out/'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+str(p.relative_to(out))+'\n' for p in sorted(out.rglob('*')) if p.is_file() and p.name!='SHA256SUMS'))
    print('Packaged target ELFs. NOT device-validated; automatic upload remains disabled.')


if __name__=='__main__':main()
