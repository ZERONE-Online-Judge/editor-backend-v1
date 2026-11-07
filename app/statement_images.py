"""Portable statement images. Never interpret arbitrary HTML, CSS or remote files."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import html
from html.parser import HTMLParser
import io
import math
import re
import warnings
from urllib.parse import unquote, urlsplit

MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
_IMAGE = re.compile(r'''<img\b(?:[^>"']|"[^"]*"|'[^']*')*>|!\[(?P<alt>(?:\\.|[^\]\\])*)\]\((?P<src>[^\s()]+)(?:\s+["'][^\n]*?["'])?\)|\\includegraphics(?:\[(?P<options>[^\]\n]*)\])?\{(?P<texsrc>[^{}\n]+)\}''', re.I)
_PROTECTED = re.compile(r"<!--[\s\S]*?-->|<(pre|code)\b[^>]*>[\s\S]*?</\1\s*>|\\begin\{(verbatim\*?|lstlisting)\}[\s\S]*?\\end\{\2\}", re.I)
_LINK = re.compile(r"(?<!!)\[[^\]\n]*\]\((?P<src>[^\s()]+)(?:\s+[\"'][^\n]*?[\"'])?\)")
_DEFINITION = re.compile(r"(?m)^ {0,3}\[(?P<label>(?:\\.|[^\]\\\n]){1,999})\]:[ \t]*(?:\n[ \t]*)?(?P<src><[^<>\n]+>|[^\s<>]+)[^\n]*(?:\n|$)")
_REFERENCE = re.compile(r"(?P<image>!)?\[(?P<alt>(?:\\.|[^\]\\\n]){0,999})\](?:[ \t]*\[(?P<label>(?:\\.|[^\]\\\n]){0,999})\])?")


@dataclass(frozen=True)
class Image:
    start: int
    end: int
    src: str
    alt: str = ""
    align: str = "center"
    width: str | None = None
    kind: str = "html"
    definition: tuple[int, int] | None = None
    definition_line: tuple[int, int] | None = None
    legacy_width: str | None = None
    legacy_height: str | None = None
    legacy_scale: float | None = None


class _Tag(HTMLParser):
    def handle_starttag(self, tag, attrs):
        self.attrs = dict(attrs)


def width(value):
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(%|px)", str(value or "").strip())
    if not match:
        return None
    number = min(100 if match[2] == "%" else 4096, max(1, float(match[1])))
    return f"{number:g}{match[2]}"


def _masked(text):
    """Keep offsets intact while excluding examples, fenced and inline code."""
    result = list(text)
    def mask(start, end):
        result[start:end] = ["\n" if ch == "\n" else " " for ch in text[start:end]]
    for match in _PROTECTED.finditer(text):
        mask(*match.span())
    fence = None
    offset = 0
    for line in text.splitlines(keepends=True):
        match = re.match(r" {0,3}(`{3,}|~{3,})(.*)", line)
        if fence:
            mask(offset, offset + len(line))
            if match and match[1][0] == fence[0] and len(match[1]) >= len(fence) and not match[2].strip():
                fence = None
        elif match:
            fence = match[1]
            mask(offset, offset + len(line))
        elif line.startswith(("    ", "\t")):
            mask(offset, offset + len(line))
        offset += len(line)
    source = "".join(result)
    for match in re.finditer(r"(`+)([^`]|(?!\1)`)*?\1(?!`)", source):
        mask(*match.span())
    return "".join(result)


def _escaped(text, position):
    start = position
    while start and text[start - 1] == "\\":
        start -= 1
    return (position - start) % 2 == 1


def _reference_images(text, source, occupied):
    def label(value):
        return " ".join(re.sub(r"\\(.)", r"\1", value).split()).casefold()
    definitions = {}
    for match in _DEFINITION.finditer(source):
        definitions.setdefault(label(match["label"]), match)
    for match in _REFERENCE.finditer(source):
        if _escaped(source, match.start()) or any(start <= match.start() < end for start, end in occupied) or any(
                definition.start() <= match.start() < definition.end() for definition in definitions.values()):
            continue
        definition = definitions.get(label(match["label"] or match["alt"]))
        if definition:
            yield Image(*match.span(), html.unescape(definition["src"].strip("<>")),
                        re.sub(r"\\(.)", r"\1", match["alt"]), kind="reference" if match["image"] else "link-reference",
                        definition=definition.span("src"), definition_line=definition.span())


def images(text):
    source = _masked(text)
    occupied = []
    for match in _IMAGE.finditer(source):
        if _escaped(source, match.start()):
            continue
        occupied.append(match.span())
        raw = text[match.start():match.end()]
        if raw.lower().startswith("<img"):
            parser = _Tag(convert_charrefs=True)
            parser.feed(raw)
            attrs = getattr(parser, "attrs", {})
            align = attrs.get("data-zoj-align", "center")
            try:
                raw_scale = float(attrs.get("data-tex-scale", ""))
            except (TypeError, ValueError):
                raw_scale = 0
            yield Image(*match.span(), attrs.get("src") or "", attrs.get("alt") or "",
                        align if align in {"left", "center", "right"} else "center",
                        width(attrs.get("data-zoj-width")), legacy_width=width(attrs.get("data-tex-width")),
                        legacy_height=width(attrs.get("data-tex-height")),
                        legacy_scale=min(4, max(.1, raw_scale)) if math.isfinite(raw_scale) and raw_scale > 0 else None)
        elif match["texsrc"] is not None:
            options = dict(part.strip().split("=", 1) for part in (match["options"] or "").split(",") if "=" in part)
            value = options.get("width", "").strip()
            fraction = re.fullmatch(r"(\d*(?:\.\d+)?)\\(?:textwidth|linewidth|columnwidth)", value)
            dimension = width(f"{float(fraction[1] or 1) * 100:g}%") if fraction else width(value)
            yield Image(*match.span(), match["texsrc"], "", "center", dimension, "tex")
        else:
            yield Image(*match.span(), html.unescape(match["src"].strip("<>")),
                        re.sub(r"\\(.)", r"\1", match["alt"]), kind="markdown")
    yield from (image for image in _reference_images(text, source, occupied) if image.kind == "reference")


def local_name(src, files):
    """Resolve explicit known flat paths, never guess from a nested basename."""
    value = html.unescape(src).strip()
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or parts.query or parts.fragment:
        return None
    value = unquote(parts.path)
    for prefix in ("../../files/", "files/", "./"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    if "/" in value or "\\" in value or value in {"", ".", ".."}:
        return None
    file = files.get(value)
    if file and file["kind"] == "statement":
        return value
    # TeX may omit the extension, but guessing among two images is unsafe.
    candidates = [name for name, item in files.items() if item["kind"] == "statement"
                  and name.rsplit(".", 1)[0] == value and name != value]
    if len(candidates) > 1:
        raise ValueError(f"지문 이미지 '{value}'에 해당하는 파일이 여러 개입니다. 확장자를 포함해 주세요.")
    return candidates[0] if candidates else None


def image_tag(image, src):
    scheme = urlsplit(src).scheme.lower()
    if (not src or any(ord(char) < 32 for char in src) or scheme not in {"", "http", "https", "asset", "data"}
            or (scheme == "data" and not re.match(r"^data:image/(?:png|jpeg|gif|webp|avif|svg\+xml);base64,", src))):
        raise ValueError("지문 이미지 주소가 올바르지 않습니다.")
    legacy = "".join(f' data-tex-{key}="{value:g}"' if isinstance(value, float) else f' data-tex-{key}="{value}"'
                     for key, value in (("width", image.legacy_width), ("height", image.legacy_height), ("scale", image.legacy_scale)) if value is not None)
    return (f'<img src="{html.escape(src, quote=True)}" alt="{html.escape(image.alt, quote=True)}"'
            f' data-zoj-align="{image.align}"' + (f' data-zoj-width="{image.width}"' if image.width else "") + legacy + ">")


def replace_source(text, image, target):
    if image.kind == "html":
        return image_tag(image, target)
    raw = text[image.start:image.end]
    match = (_LINK if image.kind == "link" else _IMAGE).fullmatch(raw)
    key = "texsrc" if image.kind == "tex" else "src"
    return raw[:match.start(key)] + target + raw[match.end(key):]


def references(text):
    found = list(images(text))
    for match in _LINK.finditer(_masked(text)):
        if not _escaped(text, match.start()) and not any(image.start <= match.start() < image.end for image in found):
            found.append(Image(*match.span(), html.unescape(match["src"].strip("<>")), kind="link"))
    occupied = [(image.start, image.end) for image in found]
    found.extend(image for image in _reference_images(text, _masked(text), occupied) if image.kind == "link-reference")
    return sorted(found, key=lambda item: item.start)


def apply_changes(text, changes, *, escape=False):
    result, offset = [], 0
    for (start, end), value in sorted(changes.items()):
        unchanged = text[offset:start]
        result.append(html.escape(unchanged) if escape else unchanged)
        result.append(value)
        offset = end
    return "".join(result) + (html.escape(text[offset:]) if escape else text[offset:])


def source_change(text, image, target):
    return ((image.definition, target) if image.definition else
            ((image.start, image.end), replace_source(text, image, target)))


def rewrite(text, files, targets):
    """Replace only image targets; code examples and project source stay intact."""
    changes = {}
    for image in references(text):
        name = local_name(image.src, files)
        if name is None:
            if image.kind in {"tex", "link", "link-reference"}:
                continue
            if urlsplit(image.src).scheme.lower() not in {"http", "https"}:
                raise ValueError(f"지문 이미지 '{image.src}'를 지문 자료에서 찾을 수 없습니다.")
        target = targets[name] if name else image.src
        change, value = source_change(text, image, target)
        changes[change] = value
    return apply_changes(text, changes)


def editorial_only(doc, *, sample_notes=None):
    from .statements import effective
    files = doc["files"]
    def used(texts):
        return {name for text in texts for image in references(text) if (name := local_name(image.src, files))}
    statements = [effective(doc, language) for language in doc["statements"]]
    if sample_notes is None:
        sample_notes = [test.get("description", "") for testset in doc["testsets"].values()
                        for test in testset["tests"].values() if test.get("sample")]
    public = used([*(item.get(key, "") for item in statements
                  for key in ("legend", "input", "output", "scoring", "interaction", "notes")),
                  *sample_notes])
    tutorial = used([item.get("tutorial", "") for item in statements])
    return tutorial - public


def image_mime(content, name):
    if content.startswith(b"\x89PNG\r\n\x1a\n"): return "image/png"
    if content.startswith(b"\xff\xd8\xff"): return "image/jpeg"
    if content[:6] in {b"GIF87a", b"GIF89a"}: return "image/gif"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP": return "image/webp"
    if content[4:8] == b"ftyp" and content[8:12] in {b"avif", b"avis"}: return "image/avif"
    if name.lower().endswith(".svg") and re.match(br"(?:\xef\xbb\xbf)?\s*(?:<\?xml[\s\S]*?\?>\s*)?(?:<!--[\s\S]*?-->\s*)*<svg(?:\s|>)", content[:2048], re.I):
        return "image/svg+xml"
    raise ValueError(f"지문 이미지 '{name}'의 파일 형식을 확인해 주세요.")


def html_section(text, files, *, embed=False, prefix="../../files"):
    from .storage import read_blob
    changes = {}
    for image in sorted(images(text), key=lambda item: item.start):
        name = local_name(image.src, files)
        if not name:
            raise ValueError(f"지문 이미지 '{image.src}'를 지문 자료에서 찾을 수 없습니다. 외부 이미지는 파일로 업로드해 주세요.")
        content = read_blob(files[name]["sha256"])
        mime = image_mime(content, name)
        if len(content) > MAX_IMAGE_BYTES:
            raise ValueError(f"지문 이미지 '{name}'는 16MiB 이하여야 합니다.")
        src = f"data:{mime};base64,{base64.b64encode(content).decode()}" if embed else f"{prefix}/{name}"
        style = f"max-width:100%;height:auto;display:block;margin-left:{'0' if image.align == 'left' else 'auto'};margin-right:{'0' if image.align == 'right' else 'auto'}"
        if dimension := image.width or image.legacy_width: style += f";width:{dimension}"
        if image.legacy_height and not image.width: style += f";height:{image.legacy_height}"
        if image.legacy_scale: style += f";zoom:{image.legacy_scale:g}"
        changes[(image.start, image.end)] = image_tag(image, src).replace(">", f' style="{style}">', 1)
        if image.definition_line: changes[image.definition_line] = ""
    return apply_changes(text, changes, escape=True)


def png_image(content, name):
    """Decode only bounded local raster data before passing pixels to TeX."""
    from PIL import Image as PillowImage, UnidentifiedImageError
    if len(content) > MAX_IMAGE_BYTES or image_mime(content, name) not in {"image/png", "image/jpeg", "image/webp"}:
        raise ValueError(f"PDF 이미지 '{name}'는 PNG·JPEG·WebP, 16MiB 이하만 지원합니다.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", PillowImage.DecompressionBombWarning)
            with PillowImage.open(io.BytesIO(content), formats=["PNG", "JPEG", "WEBP"]) as source:
                if source.width * source.height > MAX_IMAGE_PIXELS:
                    raise ValueError("이미지는 2,000만 화소 이하여야 합니다.")
                source.load()
                # Discard metadata and normalize to at most four bytes per pixel.
                result = io.BytesIO()
                source.convert("RGBA" if "A" in source.getbands() or "transparency" in source.info else "RGB").save(result, format="PNG")
                if result.tell() > 80 * 1024 * 1024:
                    raise ValueError("변환된 이미지가 너무 큽니다.")
                return result.getvalue()
    except (OSError, UnidentifiedImageError, PillowImage.DecompressionBombError, PillowImage.DecompressionBombWarning) as error:
        raise ValueError(f"PDF 이미지 '{name}'를 안전하게 읽을 수 없습니다.") from error


def tex_section(text, files, output):
    from .storage import read_blob
    changes = {}
    for image in sorted(images(text), key=lambda item: item.start):
        # Preserve existing TeX as-is; the renderer already supplies its files.
        if image.kind == "tex":
            continue
        name = local_name(image.src, files)
        if not name:
            raise ValueError(f"PDF 이미지 '{image.src}'를 지문 자료에서 찾을 수 없습니다. 외부 이미지는 파일로 업로드해 주세요.")
        identifier = "zoj-image-" + files[name]["sha256"] + ".png"
        if identifier not in output:
            output[identifier] = png_image(read_blob(files[name]["sha256"]), name)
        dimension = image.width or image.legacy_width
        size = (f"{float(dimension[:-1]) / 100:g}\\linewidth" if dimension.endswith("%")
                else f"{float(dimension[:-2]) * 0.75:g}pt") if dimension else None
        include = (r"\zojsizedimage{" + identifier + "}{" + size + "}" if size
                   else r"\zojimage{" + identifier + "}")
        if image.legacy_height or image.legacy_scale:
            options = ["width=" + size] if size else []
            if image.legacy_height and not image.width:
                height = image.legacy_height
                options.append("height=" + (f"{float(height[:-1]) / 100:g}\\textheight" if height.endswith('%') else f"{float(height[:-2]) * .75:g}pt"))
            include = r"\includegraphics" + ("[" + ",".join(options) + "]" if options else "") + "{" + identifier + "}"
            if image.legacy_scale: include = f"\\scalebox{{{image.legacy_scale:g}}}{{{include}}}"
            include = r"\zojfitimage{" + include + "}"
        environment = {"left": "flushleft", "center": "center", "right": "flushright"}[image.align]
        changes[(image.start, image.end)] = "\n\\begin{" + environment + "}\n" + include + "\n\\end{" + environment + "}\n"
        if image.definition_line: changes[image.definition_line] = ""
    return apply_changes(text, changes)
