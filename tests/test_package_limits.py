import io
import zipfile

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app import packages
from app.config import get_settings
from app.db import Problem, SessionLocal
from app.storage import read_blob
from tests.test_polygon_import import package


GIB = 1024 ** 3


def test_import_limit_endpoint_reports_the_effective_settings(client, monkeypatch):
    settings = get_settings()
    assert settings.max_archive_bytes == 2 * GIB
    monkeypatch.setattr(settings, 'max_archive_bytes', GIB)
    response = client.get('/api/imports/limits')
    assert response.status_code == 200
    assert response.json() == {'archive_bytes': GIB, 'entry_bytes': settings.max_upload_bytes,
                               'uncompressed_bytes': settings.max_uncompressed_archive_bytes}


def test_two_gib_file_boundary_without_allocating_two_gib(tmp_path):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, 'w') as archive:
        archive.writestr('problem.xml', '<problem/>')
    data = payload.getvalue()
    path = tmp_path / 'boundary.zip'
    # ZIP permits a leading prefix. A sparse prefix exercises the exact size
    # boundary without reserving gigabytes of RAM or physical disk space.
    with path.open('w+b') as stream:
        stream.seek(2 * GIB - len(data))
        stream.write(data)
        with packages.checked_archive(stream) as archive:
            assert archive.read('problem.xml') == b'<problem/>'
        stream.seek(0, io.SEEK_END)
        stream.write(b'!')
        with pytest.raises(HTTPException) as error:
            packages.checked_archive(stream)
        assert error.value.status_code == 413
        assert '2GiB' in error.value.detail['message']


def test_large_real_zip_import_uses_disk_and_preserves_the_files(owner, tmp_path, monkeypatch):
    data, _ = package()
    path = tmp_path / 'large.zip'
    path.write_bytes(data)
    with zipfile.ZipFile(path, 'a', compression=zipfile.ZIP_STORED) as archive:
        for index in range(9):
            with archive.open(f'large-data/{index:02d}.bin', 'w') as entry:
                for _ in range(15):
                    entry.write(b'\0' * (1024 ** 2))
    assert path.stat().st_size > 128 * 1024 ** 2
    original = packages.checked_archive
    uploads = []
    def inspect(content):
        assert not isinstance(content, (bytes, bytearray, io.BytesIO))
        content.fileno()
        uploads.append(content)
        return original(content)
    monkeypatch.setattr(packages, 'checked_archive', inspect)
    with path.open('rb') as stream:
        response = owner.post('/api/imports?name=Large&slug=large-package',
                              content=iter(lambda: stream.read(1024 ** 2), b''))
    assert response.status_code == 201, response.text
    result = response.json()
    assert result['import_report']['tests'] == 2
    originals = result['import_report']['preserved_files']
    large_files = [entry for entry in originals if entry['path'].startswith('large-data/')]
    assert len(large_files) == 9
    document = owner.get('/api/problems/' + result['id']).json()['document']
    assert len(read_blob(document['files'][large_files[0]['name']]['sha256'])) == 15 * 1024 ** 2
    assert uploads and all(upload.closed for upload in uploads)
    assert not list((get_settings().data_dir / 'imports').iterdir())


@pytest.mark.parametrize('declared_length', [True, False])
def test_oversized_uploads_are_rejected_and_temporary_files_are_removed(owner, monkeypatch, declared_length):
    monkeypatch.setattr(get_settings(), 'max_archive_bytes', 1024)
    chunks = [b'x' * 700, b'y' * 700]
    headers = {'Content-Length': '1400'} if declared_length else {}
    response = owner.post('/api/imports?name=TooLarge&slug=too-large', content=iter(chunks), headers=headers)
    assert response.status_code == 413
    assert response.json()['error']['code'] == 'too_large'
    with SessionLocal() as db:
        assert db.scalar(select(Problem).where(Problem.slug == 'too-large')) is None
    directory = get_settings().data_dir / 'imports'
    assert not directory.exists() or not list(directory.iterdir())


def test_invalid_archives_leave_no_import_temporary_files(owner):
    response = owner.post('/api/imports?name=Broken&slug=broken-zip', content=b'not a ZIP')
    assert response.status_code == 400
    assert not list((get_settings().data_dir / 'imports').iterdir())
