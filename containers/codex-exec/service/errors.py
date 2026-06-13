import html


ERROR_LOG_LIMIT = 2000


class UserFacingError(RuntimeError):
    def __init__(self, log_message: str, html_message: str) -> None:
        super().__init__(log_message)
        self.html_message = html_message


class CodexAuthError(UserFacingError):
    def __init__(self) -> None:
        super().__init__(
            "codex authentication failed",
            "Codex 로그인이 되어있지 않거나 API 키가 설정되지 않았습니다.\n"
            "컨테이너에서 <code>codex login</code>을 먼저 실행하거나 "
            "<code>OPENAI_API_KEY</code> 설정을 확인해주세요.",
        )


class CodexUsageLimitError(UserFacingError):
    def __init__(self, log_excerpt: str) -> None:
        log_block = f"\n<pre>{html.escape(log_excerpt)}</pre>" if log_excerpt else ""
        super().__init__(
            "codex usage limit reached",
            "<b>Codex 사용 한도에 도달했습니다.</b>\n"
            "사용 가능 시간이 지나면 다시 시도하거나 Codex 사용량/크레딧 설정을 확인해주세요."
            f"{log_block}",
        )


class UnknownCodexError(UserFacingError):
    def __init__(self, returncode: int, log_excerpt: str) -> None:
        log_block = html.escape(log_excerpt or "no stderr/stdout captured")
        super().__init__(
            f"codex exited with {returncode}",
            "<b>알 수 없는 에러가 발생했습니다.</b>\n"
            f"<code>exit_code={returncode}</code>\n"
            f"<pre>{log_block}</pre>",
        )


def classify_codex_error(returncode: int, stdout: str, stderr: str) -> UserFacingError:
    log_excerpt = codex_error_log(stdout, stderr)
    if is_codex_usage_limit_error(log_excerpt):
        return CodexUsageLimitError(log_excerpt)
    if is_codex_auth_error(log_excerpt):
        return CodexAuthError()
    return UnknownCodexError(returncode, log_excerpt)


def codex_error_log(stdout: str, stderr: str) -> str:
    parts = []
    if stderr.strip():
        parts.append(stderr.strip())
    if stdout.strip():
        parts.append(stdout.strip())
    combined = "\n".join(parts).strip()
    return combined[-ERROR_LOG_LIMIT:] if combined else ""


def is_codex_usage_limit_error(text: str) -> bool:
    lowered = text.lower()
    return (
        "you've hit your usage limit" in lowered
        or "you have hit your usage limit" in lowered
        or ("usage limit" in lowered and "try again" in lowered)
        or ("purchase more credits" in lowered and "try again" in lowered)
    )


def is_codex_auth_error(text: str) -> bool:
    text = text.lower()
    return (
        "401 unauthorized" in text
        or "missing bearer or basic authentication" in text
        or ("unauthorized" in text and "api.openai.com/v1/responses" in text)
    )
