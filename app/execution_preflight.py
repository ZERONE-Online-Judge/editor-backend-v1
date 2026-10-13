"""Stop deployment before draining live jobs if no authorized executor is ready."""
from types import SimpleNamespace
from .zoj import remote


def main():
    try:
        result = remote('GET', '/api/editor/judge/execution-preflight', SimpleNamespace(email='editor-runtime-preflight@example.com'), timeout=15)
        if result.get('protocol') == 'editor-execute-v1' and result.get('online_nodes', 0) > 0:
            print('Authorized Editor execution agents are ready.')
            return
    except Exception:
        pass
    raise SystemExit('Editor execution preflight failed; keep the current release. Deploy the ZOJ queue, prepare agent runtimes, and enable Editor on at least one online node first.')


if __name__ == '__main__':
    main()
