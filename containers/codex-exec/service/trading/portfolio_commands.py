from dataclasses import dataclass
from pathlib import Path


class PortfolioCommandError(RuntimeError):
    def __init__(self, log_message: str, html_message: str) -> None:
        super().__init__(log_message)
        self.html_message = html_message


@dataclass(frozen=True)
class PortfolioCommandResult:
    html_message: str


def parse_portfolio_symbols(path: Path) -> list[str]:
    if not path.exists():
        return []
    text = path.read_text()
    symbols: list[str] = []
    seen: set[str] = set()
    for token in text.replace(",", " ").split():
        symbol = token.strip()
        if not symbol or symbol in seen:
            continue
        symbols.append(symbol)
        seen.add(symbol)
    return symbols


def write_portfolio_symbols(path: Path, symbols: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for index in range(0, len(symbols), 10):
        lines.append(", ".join(symbols[index : index + 10]))
    path.write_text(("\n".join(lines) + "\n") if lines else "")


def parse_portfolio_ticker_arg(args: str, command: str) -> str:
    parts = args.split()
    if len(parts) != 1:
        raise PortfolioCommandError(
            "invalid portfolio ticker arguments",
            f"사용법: <code>/{command} 005930</code>",
        )
    return parts[0].strip()


def add_portfolio_ticker(path: Path, args: str) -> PortfolioCommandResult:
    ticker = parse_portfolio_ticker_arg(args, "add_portfolio_ticker")
    symbols = parse_portfolio_symbols(path)
    if ticker in symbols:
        return PortfolioCommandResult(
            (
                "<b>포트폴리오 종목 추가</b>\n"
                f"<code>{ticker}</code>는 이미 포함되어 있습니다.\n"
                f"총 <code>{len(symbols)}</code>개"
            )
        )
    symbols.append(ticker)
    write_portfolio_symbols(path, symbols)
    return PortfolioCommandResult(
        (
            "<b>포트폴리오 종목 추가</b>\n"
            f"<code>{ticker}</code> 추가 완료\n"
            f"총 <code>{len(symbols)}</code>개"
        )
    )


def remove_portfolio_ticker(path: Path, args: str) -> PortfolioCommandResult:
    ticker = parse_portfolio_ticker_arg(args, "remove_portfolio_ticker")
    symbols = parse_portfolio_symbols(path)
    if ticker not in symbols:
        return PortfolioCommandResult(
            (
                "<b>포트폴리오 종목 삭제</b>\n"
                f"<code>{ticker}</code>는 포트폴리오에 없습니다.\n"
                f"총 <code>{len(symbols)}</code>개"
            )
        )
    symbols = [symbol for symbol in symbols if symbol != ticker]
    write_portfolio_symbols(path, symbols)
    return PortfolioCommandResult(
        (
            "<b>포트폴리오 종목 삭제</b>\n"
            f"<code>{ticker}</code> 삭제 완료\n"
            f"총 <code>{len(symbols)}</code>개"
        )
    )
