"""Individual stored files and generated testcase data share one upper bound."""
MAX_FILE_BYTES = 128 * 1024**2


def file_limit_message(limit):
    return f"파일은 {limit / 1024**2:g}MiB 이하로 올려 주세요."
