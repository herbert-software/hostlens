"""One-shot fixture recorder for the wave-1 network / DNS / NTP inspectors.

Group 3 of `add-os-shell-inspectors-wave1`: `net.connections`,
`net.listening_ports`, `net.dns.resolve`, `net.ntp.drift`. These OS/Linux shell
probes are Linux-only (iproute2 `ss`, `chronyc`, `dig`) so we do NOT need a real
host. We reuse the pilot's `_CaptureTarget` pattern (lifted from
`_record_os_compute_memory.py`): drive the **real** `InspectorRunner` against a
target that

  * answers `command -v X` binary probes with a synthetic path,
  * answers `[ -r P ]` file probes empty, and
  * returns a hand-crafted `main_stdout` for the rendered collect command,

while recording every exact rendered command into a sink. Because the command
strings are captured verbatim from the real renderer (never hand-written), the
fixture can never drift from what `ReplayTarget` looks up at snapshot time
(byte-level match, Authoring Contract / design D-7).

Parameterised inspectors (`net.dns.resolve`, `net.listening_ports`) are run with
the SAME `parameters` the snapshot test passes, so the captured command (which
embeds the rendered, `| map('sh')`-quoted parameter words) matches replay.

Each scenario asserts (generation sanity) that the crafted stdout drives the
inspector to `status=ok`; abnormal scenarios further assert at least one finding
fired so we never commit a no-op fixture (ok scenarios assert none).

Run it to (re)write the fixtures:

    .venv-impl/bin/python tests/inspectors/_record_os_net.py

NOT collected by pytest (no `test_` prefix).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.runner import InspectorRunner
from hostlens.targets.base import Capability, ExecResult
from hostlens.targets.registry import TargetRegistry

_BUILTIN_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "hostlens" / "inspectors" / "builtin" / "net"
)
_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "os_net"

_PROBE_PREFIX = "command -v "
_FILE_PROBE_PREFIX = "[ -r "


# --------------------------------------------------------------------------- #
# net.tls.chain_validity — crafted `openssl s_client` stdout samples
# --------------------------------------------------------------------------- #
#
# These are the verbatim text `openssl s_client -connect host:port` prints,
# authored (NOT captured from a real handshake — `_CaptureTarget` never runs
# the collector shell, design D-7). The PEM bodies are short placeholders; the
# parser only needs the `-----BEGIN CERTIFICATE-----` marker to precede the
# "Verify return code:" footer. OpenSSL 3.x and LibreSSL footers differ in
# wording/spacing, so both are sampled to lock the regex's portability (R1).

_PEM = (
    "-----BEGIN CERTIFICATE-----\n"
    "MIIDdummyBase64CertificateBodyTruncatedForFixturePurposesOnly0000\n"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
    "-----END CERTIFICATE-----"
)

# OpenSSL 3.x valid-chain footer: depth lines + "Verify return code: 0 (ok)".
_TLS_VALID_OPENSSL = (
    "CONNECTED(00000003)\n"
    "depth=2 C = US, O = Example Root CA, CN = Example Root CA X1\n"
    "verify return:1\n"
    "depth=1 C = US, O = Example, CN = Example Intermediate CA\n"
    "verify return:1\n"
    "depth=0 CN = example.com\n"
    "verify return:1\n"
    "---\n"
    "Certificate chain\n"
    " 0 s:CN = example.com\n"
    "   i:C = US, O = Example, CN = Example Intermediate CA\n"
    "---\n"
    "Server certificate\n"
    f"{_PEM}\n"
    "subject=CN = example.com\n"
    "issuer=C = US, O = Example, CN = Example Intermediate CA\n"
    "---\n"
    "SSL handshake has read 4096 bytes and written 412 bytes\n"
    "Verification: OK\n"
    "---\n"
    "Verify return code: 0 (ok)\n"
)

# LibreSSL valid-chain footer: same "Verify return code: 0 (ok)" line, slightly
# different surrounding wording (no "Verification: OK" line on older LibreSSL).
_TLS_VALID_LIBRESSL = (
    "CONNECTED(00000005)\n"
    "depth=1 /C=US/O=Example/CN=Example Intermediate CA\n"
    "verify return:1\n"
    "depth=0 /CN=example.org\n"
    "verify return:1\n"
    "---\n"
    "Certificate chain\n"
    " 0 s:/CN=example.org\n"
    "   i:/C=US/O=Example/CN=Example Intermediate CA\n"
    "---\n"
    "Server certificate\n"
    f"{_PEM}\n"
    "subject=/CN=example.org\n"
    "issuer=/C=US/O=Example/CN=Example Intermediate CA\n"
    "---\n"
    "SSL-Session:\n"
    "    Protocol  : TLSv1.3\n"
    "---\n"
    "Verify return code: 0 (ok)\n"
)

# OpenSSL 3.x broken chain — code 20, missing intermediate CA.
_TLS_BROKEN_OPENSSL = (
    "CONNECTED(00000003)\n"
    "depth=0 CN = incomplete-chain.example\n"
    "verify error:num=20:unable to get local issuer certificate\n"
    "verify return:1\n"
    "---\n"
    "Certificate chain\n"
    " 0 s:CN = incomplete-chain.example\n"
    "   i:C = US, O = Example, CN = Example Intermediate CA\n"
    "---\n"
    "Server certificate\n"
    f"{_PEM}\n"
    "subject=CN = incomplete-chain.example\n"
    "issuer=C = US, O = Example, CN = Example Intermediate CA\n"
    "---\n"
    "Verify return code: 20 (unable to get local issuer certificate)\n"
)

# LibreSSL broken chain — code 19, self-signed cert in chain (hyphenated reason
# variant: "self-signed certificate in certificate chain").
_TLS_BROKEN_LIBRESSL = (
    "CONNECTED(00000006)\n"
    "depth=1 /CN=self-signed.example Root\n"
    "verify error:num=19:self-signed certificate in certificate chain\n"
    "verify return:1\n"
    "depth=0 /CN=self-signed.example\n"
    "verify return:1\n"
    "---\n"
    "Certificate chain\n"
    " 0 s:/CN=self-signed.example\n"
    "   i:/CN=self-signed.example Root\n"
    "---\n"
    "Server certificate\n"
    f"{_PEM}\n"
    "subject=/CN=self-signed.example\n"
    "issuer=/CN=self-signed.example Root\n"
    "---\n"
    "Verify return code: 19 (self-signed certificate in certificate chain)\n"
)

# B3 guard: non-TLS port / no peer cert. openssl STILL prints code 0, but there
# is NO PEM marker — the regex requires the marker before the Verify line, so it
# misses → null → output_schema rejects → status=exception.
_TLS_NO_CERT = (
    "CONNECTED(00000003)\n"
    "no peer certificate available\n"
    "---\n"
    "No client certificate CA names sent\n"
    "---\n"
    "SSL handshake has read 0 bytes and written 0 bytes\n"
    "---\n"
    "Verify return code: 0 (ok)\n"
)


class _CaptureTarget:
    """Generation-only target: returns canned stdout and records every command.

    Binary probes (``command -v X``) succeed with a synthetic path; file
    probes (``[ -r P ]``) succeed empty; everything else is the inspector's
    main command and returns ``main_stdout``. Each call is appended to ``sink``
    so the fixture captures the exact rendered command strings.
    """

    type = "local"

    def __init__(
        self,
        name: str,
        *,
        capabilities: set[Capability],
        main_stdout: str,
        sink: list[dict[str, Any]],
    ) -> None:
        self.name = name
        self.capabilities = capabilities
        self._main_stdout = main_stdout
        self._sink = sink

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        del timeout, env
        if cmd.startswith(_PROBE_PREFIX):
            binary = cmd[len(_PROBE_PREFIX) :].strip().strip("'\"")
            stdout = f"/usr/bin/{binary}\n"
        elif cmd.startswith(_FILE_PROBE_PREFIX):
            stdout = ""
        else:
            stdout = self._main_stdout
        self._sink.append(
            {"cmd": cmd, "stdout": stdout, "stderr": "", "exit_code": 0, "duration_seconds": 0.0}
        )
        return ExecResult(
            exit_code=0, stdout=stdout, stderr="", duration_seconds=0.0, timed_out=False
        )

    async def read_file(self, path: str) -> bytes:  # pragma: no cover - unused here
        raise AssertionError(f"_CaptureTarget.read_file unexpectedly called: {path!r}")


@dataclass(frozen=True)
class _Scenario:
    inspector: str  # manifest file stem under builtin/net/
    out_name: str  # fixture basename
    main_stdout: str  # the JSON object (or raw text) the collector pipeline would emit
    expect_findings: bool  # abnormal scenarios must produce >=1 finding
    parameters: dict[str, Any] = field(default_factory=dict)
    # net.tls.chain_validity's B3 fail-loud path: a no-cert / non-TLS stdout
    # carries "Verify return code: 0" but no PEM marker, so `raw_extract_regex`
    # misses → {verify_code: null} → output_schema rejects null → exception.
    # Those scenarios MUST record an `exception` status, so `_record` asserts
    # against this field instead of hard-coding "ok".
    expect_status: str = "ok"


# The crafted JSON objects below are exactly what each inspector's awk/dig
# pipeline emits on a host in the given state. They are the scenario data we
# author.
_SCENARIOS: tuple[_Scenario, ...] = (
    # ---- net.connections ------------------------------------------------ #
    _Scenario(
        inspector="connections",
        out_name="connections_close_wait_leak.json",
        main_stdout=(
            '{"total":1234,"established":420,"time_wait":150,"close_wait":512,'
            '"syn_sent":2,"syn_recv":1,"fin_wait":3,"listen":45}'
        ),
        expect_findings=True,
    ),
    _Scenario(
        inspector="connections",
        out_name="connections_ok.json",
        main_stdout=(
            '{"total":200,"established":120,"time_wait":30,"close_wait":2,'
            '"syn_sent":0,"syn_recv":0,"fin_wait":1,"listen":47}'
        ),
        expect_findings=False,
    ),
    # ---- net.listening_ports -------------------------------------------- #
    _Scenario(
        inspector="listening_ports",
        out_name="listening_ports_unexpected.json",
        main_stdout=(
            '{"results":['
            '{"address":"0.0.0.0","port":22,"wildcard":true,"process":"sshd"},'
            '{"address":"0.0.0.0","port":6379,"wildcard":true,"process":"redis-server"},'
            '{"address":"127.0.0.1","port":5432,"wildcard":false,"process":"postgres"}'
            "]}"
        ),
        expect_findings=True,
        parameters={"allowed_ports": [22, 443]},
    ),
    _Scenario(
        inspector="listening_ports",
        out_name="listening_ports_ok.json",
        main_stdout=(
            '{"results":['
            '{"address":"0.0.0.0","port":22,"wildcard":true,"process":"sshd"},'
            '{"address":"0.0.0.0","port":443,"wildcard":true,"process":"nginx"},'
            '{"address":"127.0.0.1","port":5432,"wildcard":false,"process":"postgres"}'
            "]}"
        ),
        expect_findings=False,
        parameters={"allowed_ports": [22, 443]},
    ),
    _Scenario(
        # add-schedule-inspector-parameters group C: a wildcard listener whose
        # owning process came back empty (non-privileged probe cannot read
        # another user's socket identity). With the default empty
        # `allowed_processes`, the conservative boundary (`"" not in []` is
        # true) still flags it. Used by the empty-process / non-empty-allowlist
        # finding test (the collector command is parameter-independent, so this
        # one recorded fixture serves every `allowed_processes` variant of the
        # empty-process row).
        inspector="listening_ports",
        out_name="listening_ports_empty_process.json",
        main_stdout=(
            '{"results":['
            '{"address":"0.0.0.0","port":9100,"wildcard":true,"process":""},'
            '{"address":"127.0.0.1","port":5432,"wildcard":false,"process":"postgres"}'
            "]}"
        ),
        expect_findings=True,
    ),
    # ---- net.dns.resolve ------------------------------------------------ #
    _Scenario(
        inspector="dns_resolve",
        out_name="dns_resolve_failure.json",
        main_stdout=(
            '{"results":['
            '{"name":"example.com","resolved":true,"address":"93.184.216.34"},'
            '{"name":"nonexistent.invalid","resolved":false,"address":""}'
            "]}"
        ),
        expect_findings=True,
        parameters={"names": ["example.com", "nonexistent.invalid"]},
    ),
    _Scenario(
        inspector="dns_resolve",
        out_name="dns_resolve_ok.json",
        main_stdout=(
            '{"results":[{"name":"example.com","resolved":true,"address":"93.184.216.34"}]}'
        ),
        expect_findings=False,
        parameters={"names": ["example.com"]},
    ),
    # ---- net.dns.resolve — injection-safety scenario -------------------- #
    # The malicious-looking payload is rejected by the parameter `pattern`
    # (`^[a-zA-Z0-9.-]+$`) at jsonschema.validate BEFORE the command renders,
    # so this scenario uses a BENIGN name and asserts (in the snapshot test)
    # that the rendered command quotes the name via shlex.quote — the payload
    # rejection is asserted separately in the test. Recording a benign name
    # here keeps the fixture loadable; the test additionally drives a payload
    # through the runner and asserts it never reaches the shell.
    _Scenario(
        inspector="dns_resolve",
        out_name="dns_resolve_injection_safe.json",
        main_stdout=(
            '{"results":[{"name":"safe-host.example","resolved":true,"address":"10.0.0.1"}]}'
        ),
        expect_findings=False,
        parameters={"names": ["safe-host.example"]},
    ),
    # ---- net.ntp.drift -------------------------------------------------- #
    _Scenario(
        inspector="ntp_drift",
        out_name="ntp_drift_high.json",
        main_stdout=(
            '{"offset_seconds":2.345678901,"abs_offset_seconds":2.345678901,'
            '"leap_status":"Normal","synced":true}'
        ),
        expect_findings=True,
    ),
    _Scenario(
        inspector="ntp_drift",
        out_name="ntp_drift_ok.json",
        main_stdout=(
            '{"offset_seconds":0.000012345,"abs_offset_seconds":0.000012345,'
            '"leap_status":"Normal","synced":true}'
        ),
        expect_findings=False,
    ),
    # ---- net.tls.chain_validity ----------------------------------------- #
    # The crafted stdout below is the verbatim text `openssl s_client` prints
    # for each scenario (cert PEM + "Verify return code: N (reason)" footer),
    # NOT JSON — this inspector parses with `format: raw` + `raw_extract_regex`.
    # The PEM bodies are truncated placeholders: the parser only needs the
    # `-----BEGIN CERTIFICATE-----` marker to appear BEFORE the Verify line
    # (B3 gate). Two openssl implementations are covered (OpenSSL 3.x footer
    # wording vs LibreSSL) for both valid-chain and broken-chain to lock the
    # regex's cross-implementation stability (design R1).
    #
    # Valid chain — OpenSSL 3.x (hostname endpoint → SNI sent). verify_code "0".
    _Scenario(
        inspector="tls_chain_validity",
        out_name="tls_chain_validity_valid_openssl.json",
        main_stdout=_TLS_VALID_OPENSSL,
        expect_findings=False,
        parameters={"endpoint": "example.com:443"},
    ),
    # Valid chain — LibreSSL footer wording (macOS local target). verify_code "0".
    _Scenario(
        inspector="tls_chain_validity",
        out_name="tls_chain_validity_valid_libressl.json",
        main_stdout=_TLS_VALID_LIBRESSL,
        expect_findings=False,
        parameters={"endpoint": "example.org:443"},
    ),
    # Broken chain — OpenSSL 3.x, code 20 (missing intermediate CA). → critical.
    _Scenario(
        inspector="tls_chain_validity",
        out_name="tls_chain_validity_broken_openssl.json",
        main_stdout=_TLS_BROKEN_OPENSSL,
        expect_findings=True,
        parameters={"endpoint": "incomplete-chain.example:443"},
    ),
    # Broken chain — LibreSSL, code 19 (self-signed cert in chain, hyphenated
    # reason variant). → critical. Locks the regex against LibreSSL wording.
    _Scenario(
        inspector="tls_chain_validity",
        out_name="tls_chain_validity_broken_libressl.json",
        main_stdout=_TLS_BROKEN_LIBRESSL,
        expect_findings=True,
        parameters={"endpoint": "self-signed.example:443"},
    ),
    # B3 false-negative guard: no peer certificate (non-TLS port / half
    # handshake) but openssl STILL prints "Verify return code: 0 (ok)" — yet
    # there is NO `-----BEGIN CERTIFICATE-----` marker, so the regex misses →
    # null → output_schema rejects → status=exception. offline-provable.
    _Scenario(
        inspector="tls_chain_validity",
        out_name="tls_chain_validity_no_cert.json",
        main_stdout=_TLS_NO_CERT,
        expect_findings=False,
        expect_status="exception",
        parameters={"endpoint": "127.0.0.1:22"},
    ),
    # Empty stdout (endpoint unreachable) → no PEM, no Verify line → null →
    # status=exception (we never call an unreachable endpoint "chain valid").
    _Scenario(
        inspector="tls_chain_validity",
        out_name="tls_chain_validity_empty.json",
        main_stdout="",
        expect_findings=False,
        expect_status="exception",
        parameters={"endpoint": "10.255.255.1:443"},
    ),
    # IPv4 endpoint, valid chain — drives the SNI case-branch command-string
    # assertion (test asserts the recorded command does NOT carry -servername
    # for a bare IPv4 endpoint). Behaviourally an ok valid-chain fixture.
    _Scenario(
        inspector="tls_chain_validity",
        out_name="tls_chain_validity_valid_ipv4.json",
        main_stdout=_TLS_VALID_OPENSSL,
        expect_findings=False,
        parameters={"endpoint": "1.1.1.1:443"},
    ),
)


async def _record(scenario: _Scenario) -> None:
    settings = Settings()
    logger = structlog.get_logger("os-net-record")
    manifest = load_manifest(_BUILTIN_DIR / f"{scenario.inspector}.yaml")

    cap_values: set[str] = {"shell"} | set(manifest.requires_capabilities)
    capabilities = {Capability(value) for value in cap_values}

    recorded: list[dict[str, Any]] = []
    runner = InspectorRunner(TargetRegistry(), settings=settings, logger=logger)
    target = _CaptureTarget(
        "capture-host",
        capabilities=capabilities,
        main_stdout=scenario.main_stdout,
        sink=recorded,
    )
    result = await runner.run(manifest, target, parameters=scenario.parameters or None)

    # Generation sanity (mirrors the pilot recorder): the crafted stdout MUST
    # drive the inspector to the declared status, and abnormal scenarios MUST
    # produce a finding so we never commit a no-op fixture. Exception scenarios
    # (net.tls.chain_validity B3: no cert / empty stdout) carry their own
    # `expect_status` and produce no findings.
    assert result.status == scenario.expect_status, (
        f"{scenario.out_name}: status={result.status} (expected "
        f"{scenario.expect_status}) error={result.error}"
    )
    if scenario.expect_findings:
        assert result.findings, (
            f"{scenario.out_name}: expected a finding but got none — check main_stdout"
        )
    else:
        assert not result.findings, (
            f"{scenario.out_name}: expected no finding but got {result.findings}"
        )

    # Dedup by command (ReplayTarget rejects duplicate command keys on load).
    commands: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in recorded:
        if entry["cmd"] in seen:
            continue
        seen.add(entry["cmd"])
        commands.append(entry)

    fixture = {
        "impersonate": "local",
        "capabilities": sorted(cap_values),
        "commands": commands,
        "files": {},
    }
    _FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = _FIXTURE_DIR / scenario.out_name
    path.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path}")


async def _main() -> None:
    for scenario in _SCENARIOS:
        await _record(scenario)


if __name__ == "__main__":
    asyncio.run(_main())
