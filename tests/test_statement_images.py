import copy
import io
import json
import os
import zipfile
from types import SimpleNamespace

import pytest
from PIL import Image as PillowImage

from app.db import Problem, Revision, SessionLocal
from app.demo import populate
from app.packages import statement_html
from app.rendering import render_statements, statement_tex
from app.schema import SourceFile, empty_document
from app.statement_images import (editorial_only, html_section, image_tag, images,
    local_name, png_image, rewrite, tex_section)
from app.storage import put_blob
from test_package_export import run_export


MARKUP = '<img src="diagram.png" alt="그림 &amp; &quot;설명&quot;" data-zoj-align="right" data-zoj-width="75%">'


def raster(format="PNG", color=(10, 20, 30)):
    output = io.BytesIO()
    PillowImage.new("RGB", (2, 3), color).save(output, format=format)
    return output.getvalue()


def files(*names):
    return {name: {"name": name, "kind": "statement", "sha256": "a" * 64} for name in names}


def test_parser_preserves_code_examples_and_clamps_only_image_settings():
    examples = [f"```html\n{MARKUP}\n```", f"~~~\n{MARKUP}\n~~~", f"`{MARKUP}`",
                f"<pre>{MARKUP}</pre>", f"<code>{MARKUP}</code>", f"<!--{MARKUP}-->",
                "\\begin{verbatim}\n" + MARKUP + "\n\\end{verbatim}", "    " + MARKUP,
                r'\![literal](missing.png)', r'\<img src="missing.png">', r'\![figure]' + '\n\n[figure]: missing.png']
    for source in examples:
        assert list(images(source)) == []
        assert rewrite(source, {}, {}) == source
    image, = images(MARKUP)
    assert (image.src, image.alt, image.align, image.width) == ("diagram.png", '그림 & "설명"', "right", "75%")
    assert image_tag(image, 'asset://id') == MARKUP.replace('diagram.png', 'asset://id')
    for raw, expected in [("999%", "100%"), ("0px", "1px"), ("10000px", "4096px"), ("calc(2px)", None)]:
        parsed, = images(f'<img src="diagram.png" data-zoj-width="{raw}" data-zoj-align="bad">')
        assert parsed.width == expected and parsed.align == "center"


def test_target_rewrite_does_not_replace_alt_text_or_unrelated_occurrences():
    source = 'diagram.png ![diagram.png](./diagram.png "title") [download](diagram.png)\n' + MARKUP
    result = rewrite(source, files('diagram.png'), {'diagram.png': 'asset://asset-7'})
    assert result.startswith('diagram.png ![diagram.png](asset://asset-7 "title") [download](asset://asset-7)')
    assert 'src="asset://asset-7"' in result
    assert 'data-zoj-width="75%"' in result
    assert rewrite(r'\includegraphics[width=0.5\textwidth]{diagram.png}', files('diagram.png'),
                   {'diagram.png': 'asset://asset-7'}) == r'\includegraphics[width=0.5\textwidth]{asset://asset-7}'


@pytest.mark.parametrize('usage', ['![A][figure]', '![][figure]', '![figure][]', '![figure]'])
def test_reference_images_are_shared_public_references_and_rewrite_the_definition(usage, monkeypatch):
    source = usage + '\n\n[Figure]: <diagram.png> "Title"\n'
    result = rewrite(source, files('diagram.png'), {'diagram.png': 'asset://asset-7'})
    assert result == usage + '\n\n[Figure]: asset://asset-7 "Title"\n'
    doc = empty_document('Reference')
    doc['files'] = files('diagram.png')
    doc['statements']['korean'].update(legend=source, tutorial='![answer](diagram.png)')
    assert editorial_only(doc) == set()
    monkeypatch.setattr('app.storage.read_blob', lambda sha: raster())
    assert 'src="../../files/diagram.png"' in html_section(source, doc['files'])
    assert '[Figure]:' not in html_section(source, doc['files'])
    assert r'\zojimage{' in tex_section(source, doc['files'], {})


def test_reference_links_and_code_examples_keep_their_original_labels():
    source = '[download][figure]\n\n[FIGURE]: diagram.png\n\n`![x][figure]`'
    result = rewrite(source, files('diagram.png'), {'diagram.png': 'asset://asset-7'})
    assert result == '[download][figure]\n\n[FIGURE]: asset://asset-7\n\n`![x][figure]`'


def test_shared_section_reference_definitions_are_resolved_in_the_effective_statement():
    doc = empty_document('Shared definitions')
    doc['files'] = files('diagram.png')
    doc['common_statement'] = '[figure]: diagram.png'
    doc['statements']['korean'].update(legend='![figure]', tutorial='![private](diagram.png)')
    assert editorial_only(doc) == set()


def test_existing_tex_image_metadata_survives_target_remapping_and_rendering(monkeypatch):
    source = '<img src="diagram.png" alt="Old" data-tex-width="60%" data-tex-height="20px" data-tex-scale="1.5">'
    result = rewrite(source, files('diagram.png'), {'diagram.png': 'asset://asset-7'})
    for attribute in ('data-tex-width="60%"', 'data-tex-height="20px"', 'data-tex-scale="1.5"'):
        assert attribute in result
    monkeypatch.setattr('app.storage.read_blob', lambda sha: raster())
    rendered = html_section(source, files('diagram.png'))
    assert 'width:60%' in rendered and 'height:20px' in rendered and 'zoom:1.5' in rendered
    tex = tex_section(source, files('diagram.png'), {})
    assert r'width=0.6\linewidth' in tex and 'height=15pt' in tex and r'\scalebox{1.5}' in tex


def test_sources_are_scoped_and_ambiguous_paths_never_guess():
    assert local_name('../../files/diagram.png', files('diagram.png')) == 'diagram.png'
    assert local_name('private/diagram.png', files('diagram.png')) is None
    assert local_name('diagram', files('diagram.png')) == 'diagram.png'
    with pytest.raises(ValueError, match='여러 개'):
        local_name('diagram', files('diagram.png', 'diagram.jpg'))
    with pytest.raises(ValueError, match='찾을 수 없습니다'):
        rewrite('<img src="missing.png">', files('diagram.png'), {})
    with pytest.raises(ValueError):
        rewrite('<img src="javascript:alert(1)" onerror="run()">', {}, {})
    assert rewrite('<img src="https://example.com/image.png" onerror="run()">', {}, {}) == (
        '<img src="https://example.com/image.png" alt="" data-zoj-align="center">')


def test_editorial_images_are_private_only_when_no_public_section_uses_them():
    doc = empty_document('Images')
    doc['files'] = files('diagram.png', 'answer.png', 'shared.png', 'unused.png')
    doc['statements']['korean']['legend'] = MARKUP
    doc['statements']['korean']['tutorial'] = '![answer](answer.png)\n![shared](shared.png)'
    doc['common_statement'] = '[download](shared.png)'
    doc['common_tutorial'] = '<img src="answer.png">'
    assert editorial_only(doc) == {'answer.png'}
    doc['testsets']['tests']['tests']['1'] = {'sample': True, 'description': '<img src="answer.png">'}
    assert editorial_only(doc) == set()


def test_html_images_use_only_local_blobs_and_escape_the_rest(monkeypatch):
    content = raster()
    monkeypatch.setattr('app.storage.read_blob', lambda sha: content)
    source = MARKUP.replace('data-zoj-align=', 'onerror="run()" data-zoj-align=') + '<script>bad()</script>'
    output = html_section(source, files('diagram.png'))
    assert 'src="../../files/diagram.png"' in output
    assert 'width:75%' in output and 'margin-left:auto;margin-right:0' in output
    assert 'onerror' not in output and '&lt;script&gt;' in output
    embedded = html_section(MARKUP, files('diagram.png'), embed=True)
    assert 'src="data:image/png;base64,' in embedded
    with pytest.raises(ValueError, match='외부 이미지'):
        html_section('<img src="https://example.com/remote.png">', {})


@pytest.mark.parametrize('format', ['PNG', 'JPEG', 'WEBP'])
def test_pdf_raster_conversion_is_bounded_and_discards_metadata(format, monkeypatch):
    content = raster(format)
    result = png_image(content, 'image.' + format.lower())
    with PillowImage.open(io.BytesIO(result)) as image:
        assert image.format == 'PNG' and image.size == (2, 3) and not image.info
    monkeypatch.setattr('app.statement_images.MAX_IMAGE_PIXELS', 2)
    with pytest.raises(ValueError, match='화소'):
        png_image(content, 'image.' + format.lower())


def test_pdf_rejects_unsupported_invalid_and_decompression_bomb_images(monkeypatch):
    with pytest.raises(ValueError, match='PNG·JPEG·WebP'):
        png_image(b'<svg xmlns="http://www.w3.org/2000/svg"/>', 'image.svg')
    with pytest.raises(ValueError, match='파일 형식'):
        png_image(b'not an image', 'image.png')
    monkeypatch.setattr(PillowImage, 'MAX_IMAGE_PIXELS', 1)
    with pytest.raises(ValueError, match='안전하게'):
        png_image(raster(), 'image.png')


def test_pdf_markup_uses_safe_paths_alignment_width_and_preserves_existing_tex(monkeypatch):
    monkeypatch.setattr('app.storage.read_blob', lambda sha: raster())
    output = {}
    value = tex_section(MARKUP + '\n![duplicate](diagram.png)', files('diagram.png'), output)
    assert len(output) == 1
    assert r'\begin{flushright}' in value and r'{0.75\linewidth}' in value
    assert r'\begin{center}' in value and r'\zojimage{zoj-image-' in value
    assert '<img' not in value and 'diagram.png' not in value
    original = r'Before $N$ \includegraphics[width=0.5\textwidth]{diagram.png}'
    assert tex_section(original, files('diagram.png'), {}) == original
    with pytest.raises(ValueError, match='외부 이미지'):
        tex_section('<img src="https://example.com/image.png">', {}, {})


def setup_document(owner, problem):
    with SessionLocal() as db:
        row = db.get(Problem, problem)
        doc = populate(db, row)
        row.document = copy.deepcopy(doc)
        db.get(Revision, (problem, 0)).document = copy.deepcopy(doc)
        for index, name in enumerate(('diagram.png', 'answer.png', 'shared.png', 'unused.png')):
            blob = put_blob(db, problem, raster(color=(index, 20, 30)))
            doc['files'][name] = SourceFile(name=name, kind='statement', language='text', **blob).model_dump()
        doc['statements']['korean'].update(legend=MARKUP, tutorial='![answer](answer.png)\n![shared](shared.png)')
        doc['statements']['english'] = {**doc['statements']['korean'], 'name': 'Images', 'legend': 'English ' + MARKUP}
        doc['common_statement'] = '![shared](shared.png)'
        doc['common_tutorial'] = '<img src="answer.png" alt="Answer" data-zoj-align="left">'
        doc['statements']['korean']['scoring'] = 'Scoring text\n' + MARKUP
        doc['statements']['korean']['interaction'] = 'Interaction text\n' + MARKUP
        doc['testsets']['tests']['tests']['1']['description'] = '<img src="shared.png" alt="Sample note">'
        db.commit()
    state = owner.get(f'/api/problems/{problem}').json()
    saved = owner.put(f'/api/problems/{problem}/working-copy', json={'version': state['version'], 'document': doc})
    assert saved.status_code == 200, saved.text
    committed = owner.post(f'/api/problems/{problem}/commit', json={'version': saved.json()['version'], 'message': 'Insert images'})
    assert committed.status_code == 200, committed.text
    assert owner.get(f'/api/problems/{problem}/revisions/1').json()['statements']['korean']['legend'] == MARKUP
    return doc


@pytest.mark.parametrize('format', ['editor', 'linux', 'zoj'])
def test_images_survive_revisions_and_package_roundtrip(owner, problem, monkeypatch, format):
    doc = setup_document(owner, problem)
    archive, content = run_export(owner, problem, monkeypatch, format)
    with archive:
        if format == 'zoj':
            manifest = json.loads(archive.read('manifest.json'))
            assets = {item['original_filename']: item for item in manifest['assets']}
            assert assets['answer.png']['category'] == 'editorial-assets'
            assert all(assets[name]['category'] == 'assets' for name in ('diagram.png', 'shared.png', 'unused.png'))
            assert 'src="asset://' + assets['diagram.png']['id'] + '"' in manifest['problem']['statement']
            assert 'data-zoj-align="right"' in manifest['problem']['statement']
            assert 'data-zoj-width="75%"' in manifest['problem']['statement']
            assert '### 부분 점수' in manifest['problem']['statement'] and 'Scoring text' in manifest['problem']['statement']
            assert '### 인터랙션' in manifest['problem']['statement'] and 'Interaction text' in manifest['problem']['statement']
            meta = json.loads(manifest['problem']['statement'].split('<!--ZOJ_META:', 1)[1].split('-->', 1)[0])
            assert 'src="asset://' + assets['shared.png']['id'] + '"' in meta['examples'][0]['note']
            assert assets['editor-project.json']['category'] == 'package-files/package-resource'
        else:
            assert json.loads(archive.read('editor.json'))['document']['statements'] == doc['statements']
            assert 'src="../../files/diagram.png"' in archive.read('statements/korean/statement.html').decode()
            tex = archive.read('statements/korean/statement.tex').decode()
            assert r'\zojsizedimage{' in tex
            assert any(name.startswith('statements/korean/zoj-image-') for name in archive.namelist())
    imported = owner.post('/api/imports?name=Images&slug=images-' + format, content=content)
    assert imported.status_code == 201, imported.text
    restored = owner.get('/api/problems/' + imported.json()['id']).json()['document']
    assert restored['statements'] == doc['statements']
    assert restored['files'] == doc['files']


def test_statement_html_and_tex_share_image_settings(owner, problem):
    doc = setup_document(owner, problem)
    output = {}
    assert 'data:image/png;base64,' in statement_html(doc, 'korean', embed_images=True)
    tex = statement_tex(doc, 'korean', image_files=output)
    assert r'\begin{flushright}' in tex and r'{0.75\linewidth}' in tex
    assert output


def test_polygon_declared_resource_paths_are_restored_without_guessing(owner):
    from test_polygon_import import package
    _, data = package()
    data['problem.xml'] = data['problem.xml'].replace(b'<files>', b'<files><statement-resources><file path="files/figures/diagram.png"/></statement-resources>')
    data['files/figures/diagram.png'] = raster()
    properties = json.loads(data['statements/korean/problem-properties.json'])
    properties['legend'] = MARKUP.replace('diagram.png', '../../files/figures/diagram.png')
    data['statements/korean/problem-properties.json'] = json.dumps(properties).encode()
    content = io.BytesIO()
    with zipfile.ZipFile(content, 'w') as archive:
        for name, value in data.items(): archive.writestr(name, value)
    response = owner.post('/api/imports?name=Images&slug=polygon-images', content=content.getvalue())
    assert response.status_code == 201, response.text
    doc = owner.get('/api/problems/' + response.json()['id']).json()['document']
    assert doc['statements']['korean']['legend'] == MARKUP
    assert doc['files']['diagram.png']['kind'] == 'statement'


def test_render_html_embeds_assets_and_pdf_receives_only_converted_local_pixels(owner, problem, tmp_path):
    doc = setup_document(owner, problem)
    protected = 'zoj-image-' + doc['files']['diagram.png']['sha256'] + '.png'
    with SessionLocal() as db:
        untrusted = put_blob(db, problem, b'attachment must not override generated output')
        db.commit()
    for name in (protected, 'statement.tex'):
        doc['files'][name] = SourceFile(name=name, kind='statement', language='text', **untrusted).model_dump()
    class Renderer:
        calls = 0
        def new_folder(self):
            folder = tmp_path / str(self.calls)
            folder.mkdir()
            return folder
        def execute(self, folder, argv, **kwargs):
            self.calls += 1
            assert argv[1] == '-no-shell-escape'
            assert r'\begin{flushright}' in (folder / 'statement.tex').read_text()
            assert list(folder.glob('zoj-image-*.png'))
            with PillowImage.open(folder / protected) as decoded:
                assert decoded.size == (2, 3)
            (folder / 'run').mkdir()
            (folder / 'run/statement.pdf').write_bytes(b'%PDF-test')
            return {'exit_code': 0, 'status': 'ok'}
    renderer = Renderer()
    result = render_statements(SimpleNamespace(id='image-render-test', document=doc), renderer)
    assert result['korean']['pdf'] == 'OK' and renderer.calls == 2
    from app.config import get_settings
    rendered = get_settings().data_dir / 'rendered/image-render-test/korean.html'
    assert 'src="data:image/png;base64,' in rendered.read_text()


def test_render_unsupported_pdf_image_reports_failure_without_hiding_the_picture(owner, problem, tmp_path):
    with SessionLocal() as db:
        stored = put_blob(db, problem, b'<svg xmlns="http://www.w3.org/2000/svg"/>')
        db.commit()
    doc = empty_document('Vector')
    doc['files']['diagram.svg'] = SourceFile(name='diagram.svg', kind='statement', language='text', **stored).model_dump()
    doc['statements']['korean']['legend'] = '<img src="diagram.svg" alt="Vector">'
    class Renderer:
        def new_folder(self): return tmp_path
        def execute(self, *args, **kwargs): pytest.fail('Unsupported images must not be passed to TeX')
    result = render_statements(SimpleNamespace(id='vector-render-test', document=doc), Renderer())
    assert result['korean']['html'] == 'OK' and result['korean']['pdf'] == 'FAILED'
    assert 'PNG·JPEG·WebP' in result['korean']['message']


@pytest.mark.sandbox
@pytest.mark.skipif(os.environ.get('RUN_SANDBOX_TESTS') != '1', reason='Real Editor renderer image required')
def test_real_offline_pdf_renders_inline_raster_image_layout(owner, problem):
    from app.sandbox import Sandbox
    with SessionLocal() as db:
        stored = put_blob(db, problem, raster('WEBP'))
        db.commit()
    doc = empty_document('이미지 PDF')
    doc['files']['diagram.webp'] = SourceFile(name='diagram.webp', kind='statement', language='text', **stored).model_dump()
    doc['statements']['korean']['legend'] = '\n\n'.join(
        f'앞 문장 $N$\n<img src="diagram.webp" alt="설명" data-zoj-align="{align}"{dimension}>\n뒤 문장'
        for align, dimension in [('left', ''), ('center', ' data-zoj-width="50%"'), ('right', ' data-zoj-width="4096px"')])
    sandbox = Sandbox()
    try:
        result = render_statements(SimpleNamespace(id='real-inline-image-render', document=doc), sandbox)
        assert result['korean']['pdf'] == 'OK', result
        assert result['korean']['pdf_size'] > 1000
    finally:
        sandbox.close()
