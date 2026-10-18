import base64
import hashlib
import io
import threading
import zipfile
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app import execution_protocol as protocol, judge_bridge, packages
from app.config import get_settings
from app.db import SessionLocal
from app.file_limits import MAX_FILE_BYTES
from app.schema import SourceFile
from app.storage import blob_path, put_blob, put_blob_file
from test_judge_bridge import fixture_data, record
from test_polygon_import import package

MIB = 1024**2


def test_upload_and_download_real_32_mib_testcase(owner, problem):
    content = b'1234567\n' * (4 * MIB)
    response = owner.post(f'/api/problems/{problem}/blobs', content=iter([content[:16*MIB], content[16*MIB:]]))
    assert response.status_code == 201, response.text
    assert response.json() == {'sha256': hashlib.sha256(content).hexdigest(), 'size': 32*MIB}
    downloaded = owner.get(f'/api/problems/{problem}/blobs/{response.json()["sha256"]}')
    assert downloaded.status_code == 200 and downloaded.content == content
    assert owner.get('/api/imports/limits').json()['entry_bytes'] == MAX_FILE_BYTES


def test_upload_exact_128_mib_and_reject_next_byte(owner, problem):
    response = owner.post(f'/api/problems/{problem}/blobs', content=iter([b'x'*MIB] * 128))
    assert response.status_code == 201 and response.json()['size'] == MAX_FILE_BYTES
    with blob_path(response.json()['sha256']).open('rb') as stored:
        assert hashlib.file_digest(stored, 'sha256').hexdigest() == response.json()['sha256']
    rejected = owner.post(f'/api/problems/{problem}/blobs', content=iter([b'x'*MIB]*128 + [b'!']))
    assert rejected.status_code == 413 and '128MiB' in rejected.text
    SourceFile(name='large.cpp', sha256=response.json()['sha256'], size=MAX_FILE_BYTES, language='cpp17', kind='source')
    with pytest.raises(ValidationError):
        SourceFile(name='large.cpp', sha256=response.json()['sha256'], size=MAX_FILE_BYTES+1, language='cpp17', kind='source')


def test_lower_configured_limit_is_enforced_for_upload_and_storage(owner, problem, monkeypatch):
    monkeypatch.setattr(get_settings(), 'max_upload_bytes', MIB)
    rejected = owner.post(f'/api/problems/{problem}/blobs', content=b'0'*(MIB+1))
    assert rejected.status_code == 413 and '1MiB' in rejected.text
    with SessionLocal() as db, pytest.raises(HTTPException) as error:
        put_blob_file(db, problem, io.BytesIO(b'0'*(MIB+1)))
    assert error.value.status_code == 413


def test_package_import_preserves_a_real_32_mib_entry(owner):
    raw, _ = package()
    stream = io.BytesIO(raw)
    content = b'abcdefgh'*(4*MIB)
    with zipfile.ZipFile(stream, 'a') as archive:
        archive.writestr('large/inputs.bin', content)
    response = owner.post('/api/imports?name=Large&slug=large-file-entry', content=stream.getvalue())
    assert response.status_code == 201, response.text
    assert blob_path(hashlib.sha256(content).hexdigest()).read_bytes() == content
    doc = owner.get('/api/problems/'+response.json()['id']).json()['document']
    assert any(file['size'] == len(content) for file in doc['files'].values())


def test_execution_protocol_round_trips_more_than_old_64_mib_total():
    files = {'.input': {'base64': '', 'executable': False}}
    for index in range(3):
        files[f'data-{index}'] = {'base64': base64.b64encode(bytes([index])*24*MIB).decode(), 'executable': False}
    value = {'format': protocol.FORMAT, 'operation_id': 'a'*32, 'job_kind': 'prepare', 'runtime': 'runtime',
             'config': {'command':['true'], 'cpu_ms':1000,'memory_mb':256,'output_bytes':MAX_FILE_BYTES,
                        'wall_seconds':8,'output_file':'stdout','compile_language':None,'export_files':[]}, 'files': files}
    result = protocol.unpack(protocol.pack(value))
    assert result == value
    assert protocol.MAX_FILES_BYTES == 512*MIB
    assert protocol.MAX_WIRE_BYTES == 768*MIB


def test_large_judge_batches_preserve_all_indices_and_expectations(problem, tmp_path, monkeypatch):
    pipeline = fixture_data(problem, tmp_path, tag='WA')
    with SessionLocal() as db:
        for index, test in enumerate(pipeline.result['testsets']['tests'], 1):
            test['input_sha'] = put_blob(db, problem, bytes([index])*17*MIB)['sha256']
        db.commit()
    base, _ = judge_bridge.judge_files(pipeline, 'main.cpp')
    monkeypatch.setattr(judge_bridge, 'MAX_JUDGE_CONTENT_BYTES', sum(p.stat().st_size for p in base.values())+35*MIB)
    calls = []
    def remote(method, path, user, package=None, *args, **kwargs):
        assert method == 'POST'
        with zipfile.ZipFile(package) as archive:
            import json
            indices = [case['index'] for case in json.loads(archive.read('manifest.json'))['testcases']]
        calls.append(indices)
        result = record('wrong_answer' if 3 in indices else 'accepted', run_id=str(len(calls)))
        result['result']['testcase_results'] = [dict(index=i, final_status='wrong_answer' if i==3 else 'accepted', runtime_ms=1, memory_kb=512) for i in indices]
        return result
    monkeypatch.setattr(judge_bridge, 'remote', remote)
    result = judge_bridge.verify_solution(pipeline, SimpleNamespace(email='author@example.com'), 'main.cpp', threading.Event())
    assert calls == [[1, 2], [3]]
    assert result['complete'] and result['matched']
    assert [case['index'] for case in result['cases']] == [1, 2, 3]
    assert [run['testcase_count'] for run in result['judge_runs']] == [2, 1]


def test_progress_keeps_prior_large_batches(problem, tmp_path):
    from app.verification_progress import VerificationProgress
    p = fixture_data(problem, tmp_path)
    p.publish = lambda *args, **kwargs: None
    p.last_publish = 0
    p.result_lock = threading.RLock()
    progress = VerificationProgress(p, ['main.cpp'])
    progress.batch('main.cpp', 'tests', [{'testset':'tests','index':1,'verdict':'AC'}], {'id':'batch1','testset':'tests'})
    progress.batch('main.cpp', 'tests', [{'testset':'tests','index':2,'verdict':'WA'}], {'id':'batch2','testset':'tests'})
    progress.batch('main.cpp', 'tests', [{'testset':'tests','index':2,'verdict':'WA'}])
    assert [case['index'] for case in progress.rows['main.cpp']['cases']] == [1, 2]
    assert len(progress.rows['main.cpp']['judge_runs']) == 2
