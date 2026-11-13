"""Default-disabled owner HTTPS diagnostic. No authentication, enrollment, browser or Epic/LIVE routes.

Preparing this launcher does not authorize execution. A separately approved owner procedure must
establish certificate custody/trust and hostname handling before any explicit diagnostic activation.
No certificate generation, trust installation, hostname edits or automatic browser launch is provided.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, NoReturn


class DiagnosticRejected(Exception):
    """Content-free failure; command arguments and credential paths must never reach error output."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise DiagnosticRejected


def _lock_environment() -> None:
    # Refuse trust/keylog overrides, rather than quietly accepting a substituted trust source.
    if any(name in os.environ for name in ("SSL_CERT_FILE", "SSL_CERT_DIR", "SSLKEYLOGFILE")):
        raise DiagnosticRejected
    for name in tuple(os.environ):
        if name.startswith(
            (
                "CLINICAL_TRACE_",
                "OTEL_",
                "LANGCHAIN_",
                "LANGSMITH_",
                "MLFLOW_",
                "EPIC_",
                "AWS_",
                "AZURE_",
                "OPENAI_",
                "ANTHROPIC_",
            )
        ):
            os.environ.pop(name, None)  # diagnostic child process only; never read or print values
    os.environ["CLINICAL_TRACE_SKIP_DOTENV"] = "1"
    os.environ["CLINICAL_TRACE_LLM_PROVIDER"] = "none"
    os.environ["OTEL_SDK_DISABLED"] = "true"


def _prepare(certificate: Path, private_key: Path) -> Any:
    # No application dependency import or path access until the caller explicitly enables the diagnostic.
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    from api.beta_owner_https import prepare_bootstrap
    from api.beta_webauthn import OWNER_ORIGIN, LocalOwnerPolicy

    # Only a diagnostic policy placeholder: no owner identity, credential store or enrollment authority.
    policy = LocalOwnerPolicy(OWNER_ORIGIN, b"diagnostic-only-no-owner-identity")
    return prepare_bootstrap(policy, certificate, private_key, enabled=True)


def _serve(certificate: Path, private_key: Path) -> None:
    import uvicorn

    config = _prepare(certificate, private_key)
    uvicorn.Server(config).run()  # the sole activation point; no reload, subprocess, browser or app injection


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--serve-diagnostic", action="store_true", help="explicit diagnostic activation after separate approval"
    )
    parser.add_argument("--certificate", type=Path, help="absolute owner-managed leaf/chain PEM path")
    parser.add_argument(
        "--private-key", type=Path, help="absolute owner-managed private key path; no passphrase argument"
    )
    try:
        args = parser.parse_args(argv)
        if not args.serve_diagnostic:
            print("Owner HTTPS diagnostic is disabled. Separate listener/trust approval is required.")
            return 2
        if (
            args.certificate is None
            or args.private_key is None
            or not args.certificate.is_absolute()
            or not args.private_key.is_absolute()
        ):
            raise DiagnosticRejected
        _lock_environment()
        _serve(args.certificate, args.private_key)
        return 0
    except KeyboardInterrupt:
        return 0
    except SystemExit as error:
        # argparse help is harmless; Uvicorn bind/startup failure must remain a failure without traceback.
        return 0 if error.code == 0 else 1
    except Exception:
        print("Owner HTTPS diagnostic refused or failed; no fallback. Review is required.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
