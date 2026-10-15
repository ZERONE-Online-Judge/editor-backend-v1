import re
import base64
from pathlib import Path
from .config import get_settings
from .sandbox import SandboxError, safe_output
from .storage import read_blob
from .statement_samples import display


def tex_escape(text):
    replacements = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    return "".join(replacements.get(c, c) for c in text)


def statement_tex(doc, language):
    statement = doc["statements"][language]
    title = tex_escape(statement["name"] or doc["info"]["name"])
    result = [r"\documentclass[11pt]{article}", r"\usepackage[a4paper,margin=22mm]{geometry}", r"\usepackage{fontspec,xeCJK,amsmath,amssymb,graphicx,enumitem,hyperref}", r"\setCJKmainfont{Noto Sans CJK KR}", r"\setmainfont{DejaVu Sans}", r"\newcommand{\InputFile}{\section*{입력}}", r"\newcommand{\OutputFile}{\section*{출력}}", r"\newcommand{\Note}{\section*{노트}}", r"\newcommand{\Scoring}{\section*{부분 점수}}", r"\newcommand{\Interaction}{\section*{인터랙션}}", r"\begin{document}", r"\begin{center}{\Large\bfseries " + title + r"}\end{center}", f"시간 제한: {doc['info']['time_limit_ms']}ms\\quad 메모리 제한: {doc['info']['memory_limit_mb']}MiB\n\n"]
    for label, key in [("문제", "legend"), ("입력", "input"), ("출력", "output"), ("부분 점수", "scoring"), ("인터랙션", "interaction"), ("노트", "notes"), ("해설", "tutorial")]:
        text = statement[key]
        if text:
            result += [r"\section*{" + label + "}", text]
    for testset in doc["testsets"].values():
        for test in sorted(testset["tests"].values(), key=lambda t: t["index"]):
            if test["sample"] and (test.get("input_sha") or test.get("statement_input") is not None) and (test.get("answer_sha") or test.get("statement_output") is not None):
                result += [r"\subsection*{예제 입력}", r"\begin{verbatim}", display(test, "input").replace(r"\end{verbatim}", "[verbatim-end]"), r"\end{verbatim}", r"\subsection*{예제 출력}", r"\begin{verbatim}", display(test, "output").replace(r"\end{verbatim}", "[verbatim-end]"), r"\end{verbatim}"]
    result.append(r"\end{document}")
    return "\n".join(result)


def render_statements(job, sandbox):
    from .packages import statement_html
    destination = get_settings().data_dir / "rendered" / job.id
    destination.mkdir(parents=True, exist_ok=True)
    results = {}
    for language in job.document["statements"]:
        html_content = statement_html(job.document, language, "/assets")
        katex = Path(__file__).resolve().parent.parent / "vendor" / "katex"
        css = (katex / "katex.min.css").read_text()
        def inline_font(match):
            font = katex / match.group(1)
            encoded = base64.b64encode(font.read_bytes()).decode()
            kind = "woff2" if font.suffix == ".woff2" else "woff" if font.suffix == ".woff" else "ttf"
            return "url(data:font/" + kind + ";base64," + encoded + ")"
        css = re.sub(r"url\((fonts/[^)]+)\)", inline_font, css)
        html_content = html_content.replace("<link rel='stylesheet' href='/assets/katex/katex.min.css'>", "<style>" + css + "</style>")
        for script in ["katex.min.js", "auto-render.min.js"]:
            html_content = html_content.replace(f"<script src='/assets/katex/{script}'></script>", "<script>" + (katex / script).read_text().replace("</script", "<\\/script") + "</script>")
        (destination / (language + ".html")).write_text(html_content)
        folder = sandbox.new_folder()
        (folder / "statement.tex").write_text(statement_tex(job.document, language))
        for file in job.document["files"].values():
            if file["kind"] == "statement":
                (folder / file["name"]).write_bytes(read_blob(file["sha256"]))
        run = sandbox.execute(folder, ["xelatex", "-no-shell-escape", "-halt-on-error", "-interaction=nonstopmode", "statement.tex"], cpu_ms=60000, memory_mb=1024, output_bytes=16 * 1024 * 1024, wall_seconds=90, image=get_settings().render_image, export_files=["statement.pdf"])
        pdf_path = folder / "run" / "statement.pdf"
        if run["exit_code"] == 0 and run["status"] == "ok" and pdf_path.is_file():
            pdf_bytes = safe_output(pdf_path, 16 * 1024 * 1024)
            (destination / (language + ".pdf")).write_bytes(pdf_bytes)
            results[language] = {"html": "OK", "pdf": "OK", "pdf_size": len(pdf_bytes)}
        else:
            results[language] = {"html": "OK", "pdf": "FAILED", "message": (run["stderr"] + run["output"].decode("utf-8", "replace"))[-8192:]}
    return results
