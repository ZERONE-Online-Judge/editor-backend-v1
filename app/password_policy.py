"""Validate new credential input; ZOJ remains the credential authority."""
import string


def validate_password(password):
    if not 8 <= len(password) <= 128:
        raise ValueError("비밀번호는 8~128자로 입력하세요.")
    allowed = string.ascii_letters + string.digits + string.punctuation
    if any(c not in allowed for c in password):
        raise ValueError("영문·숫자·특수문자만 사용할 수 있습니다. 한글과 공백은 제외하세요.")
    if not any(c in string.ascii_letters for c in password):
        raise ValueError("영문을 1개 이상 포함하세요. 대소문자를 구분합니다.")
    if not any(c in string.digits for c in password):
        raise ValueError("숫자를 1개 이상 포함하세요.")
    if not any(c in string.punctuation for c in password):
        raise ValueError("특수문자를 1개 이상 포함하세요.")
    return password
