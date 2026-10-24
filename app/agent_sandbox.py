"""Online jobs use ZOJ agents exclusively, including compile and preparation."""
from .sandbox import Sandbox, SandboxError
from .agent_transport import AgentTransport


class AgentSandbox(Sandbox):
    def __init__(self, check_cancelled=lambda: None, remote_context=None):
        if not remote_context:
            raise SandboxError("ZOJ 에이전트 실행 요청 정보가 필요합니다.")
        super().__init__(check_cancelled, remote_context)
        # A development/legacy setting must never select the replay executor.
        if self.agent_transport is None:
            self.agent_transport = AgentTransport(self, remote_context)

    def resolve_image(self, label):
        if self.agent_transport is None:
            raise SandboxError("ZOJ 에이전트 연결을 확인해 주세요. 자체 실행으로 전환하지 않습니다.")
        return self.agent_transport.context["runtime_revision"]

    def _execute(self, *args, **kwargs):
        if self.agent_transport is None:
            raise SandboxError("ZOJ 에이전트 연결을 확인해 주세요. 자체 실행으로 전환하지 않습니다.")
        return super()._execute(*args, **kwargs)


def require_agent_job(job):
    if job.options.get("execution_transport") != "agent" or job.options.get("execution_backend") != "zoj":
        from .domain import fail
        fail("agent_execution_required", "이전 자체 실행 설정의 작업입니다. ZOJ 에이전트로 작업을 다시 요청해 주세요.")
    needs_reference = job.kind in {"verify", "stress", "package"} or (job.kind == "prepare" and job.options.get("prepare_mode", "full") in {"full", "answers"})
    if needs_reference and job.options.get("reference_output_protocol") != "editor-reference-output-v1":
        from .domain import fail
        fail("agent_reference_required", "이전 정답 생성 환경의 작업입니다. ZOJ 채점기로 다시 요청해 주세요.")
