from __future__ import annotations

import argparse
import logging
import sys

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def _configure_logging(level: int) -> None:
    """Log to stderr at `level`, cooperating with OpenTelemetry's logging setup.

    Under `opentelemetry-instrument` with `OTEL_PYTHON_LOG_CORRELATION=true`,
    the logging instrumentation has already called `logging.basicConfig` with a
    format carrying `trace_id`/`span_id`, so a second `basicConfig` would be a
    no-op and drop `--log-level`. The root logger also holds OpenTelemetry's
    export handler, which must stay. So add a stderr handler only when no
    stream handler is there, never replace what is, and always apply `level`.
    """

    root = logging.getLogger()
    if not any(type(h) is logging.StreamHandler for h in root.handlers):
        handler = logging.StreamHandler()  # stderr: on stdio, stdout is the protocol
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        root.addHandler(handler)
    root.setLevel(level)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="adas-mcp")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http", "streamable-http", "sse"],
        default="stdio",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    _configure_logging(getattr(logging, args.log_level.upper(), logging.INFO))

    from .server import run

    run(args.transport)
    return 0


if __name__ == "__main__":
    sys.exit(main())
