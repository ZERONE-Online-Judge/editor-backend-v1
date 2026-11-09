"""Reuse immutable package artifacts without requesting new verification."""
import re
from types import SimpleNamespace

from sqlalchemy import func, select
from sqlalchemy.orm import load_only

from .config import get_settings
from .db import Job, JobDiscard


def package_path(job):
    return get_settings().data_dir / "packages" / ((job if isinstance(job, str) else job.id) + ".zip")


def artifact_present_summary(job_id, artifact):
    path = package_path(job_id)
    return (isinstance(artifact, dict) and not artifact.get("invalid")
            and re.fullmatch(r"[a-f0-9]{64}", str(artifact.get("sha256", "")))
            and path.is_file() and path.stat().st_size == artifact.get("size"))


def artifact_present(job):
    return artifact_present_summary(job.id, job.result.get("package", {}))


def invalidate_artifact(db, job_id):
    job = db.scalar(select(Job).where(Job.id == job_id).options(load_only(Job.id, Job.result)).with_for_update())
    if job and isinstance(job.result.get("package"), dict):
        job.result = {**job.result, "package": {**job.result["package"], "invalid": True}}


def effective_sets(job):
    if job.options.get("export_format", "editor") != "zoj":
        return list(job.document["testsets"])
    if job.options.get("testsets") is not None:
        requested = set(job.options["testsets"])
        return [name for name in job.document["testsets"] if name in requested]
    return [job.options.get("testset", "tests")]


def package_layout(document_sets, options):
    if options.get("export_format", "editor") != "zoj" or len(document_sets) <= 1:
        return "single"
    # Legacy ZOJ exports contain all sets as separate manifest versions;
    # explicit selected sets are combined into one active version.
    return "versions" if options.get("testsets") is None else "merged"


def package_identity(problem_id, revision, source_hash, document, options):
    return {"problem_id": problem_id, "revision": revision, "source_document_hash": source_hash,
        "document_testsets": list(document["testsets"]),
        "testsets": effective_sets(SimpleNamespace(document=document, options=options))}


def compatible_package(job, item, options):
    if (job.kind != "package" or job.revision != item["revision"]
            or (job.options.get("source_document_hash") or job.document_hash) != item["source_document_hash"]
            or job.options.get("export_format", "editor") != options["export_format"]
            or effective_sets(job) != item["testsets"]
            or package_layout(list(job.document["testsets"]), job.options)
                != package_layout(item.get("document_testsets", item["testsets"]), options)):
        return False
    if options["export_format"] == "zoj" and job.options.get("statement_language") != options["statement_language"]:
        return False
    if job.status == "completed":
        artifact = job.result.get("package", {})
        if not artifact_present(job) or artifact.get("format") != options["export_format"]:
            return False
        # Historical packages may lack the top-level verification summary.
        # Trust their completed artifact metadata, but reject contradictions.
        if not isinstance(artifact.get("verified"), bool):
            return False
        if artifact["verified"] and (job.options.get("verify", True) is False or job.result.get("verified") is False):
            return False
        if options["verify"] and artifact["verified"] is not True:
            return False
        if options["export_format"] == "zoj" and (artifact.get("testsets") != item["testsets"]
                or artifact.get("statement_language") != options["statement_language"]):
            return False
        return True
    # Joining an existing preparation also needs no new agent admission, but
    # its requested verification mode must satisfy this request.
    if options["verify"] and not job.options.get("verify", True):
        return False
    if not job.options.get("verify", True) and (job.options.get("package_export_version") != 1
            or job.options.get("execution_backend") != "archive" or job.options.get("execution_transport") != "archive"):
        return False
    return job.status in {"queued", "running"}


def cached_package(db, user, item, options):
    rows = db.execute(select(Job.id, Job.revision, Job.document_hash, Job.options, Job.status,
        Job.result["package"].label("package"), Job.result["verified"].as_boolean().label("verified"))
        .where(Job.problem_id == item["problem_id"], Job.kind == "package",
        Job.revision == item["revision"], Job.status.in_(["completed", "queued", "running"]),
        func.coalesce(Job.options["source_document_hash"].as_string(), Job.document_hash) == item["source_document_hash"],
        ~select(JobDiscard.job_id).where(JobDiscard.job_id == Job.id, JobDiscard.user_id == user.id).exists()
        ).order_by((Job.status == "completed").desc(), Job.created_at.desc()).limit(100)).all()
    for row in rows:
        job = SimpleNamespace(id=row.id, kind="package", revision=row.revision, document_hash=row.document_hash,
            options=row.options, status=row.status,
            document={"testsets": dict.fromkeys(item.get("document_testsets", item["testsets"]))},
            result={"package": row.package, "verified": row.verified})
        if compatible_package(job, item, options):
            return job
    return None
