"""Static contract tests for the hub installers' serve-port behavior
(issue #423). The installers are host-only scripts (a Tailscale node, a
scheduled task / systemd unit — not CI-executable), so these pin the
contract at the source level: the `-HttpsPort`/`HTTPS_PORT` parameter
exists, the serve call uses it, and an occupied :443 is refused before
re-pointing serve. A mutant that deletes the guard or re-hardcodes 443
turns these red (verified during development).

Run: python tests/test_fleet_installer_443.py
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PS1 = REPO_ROOT / "scripts" / "fleet" / "install-hub.ps1"
SH = REPO_ROOT / "scripts" / "fleet" / "install-hub.sh"
FLEET = REPO_ROOT / "docs" / "FLEET.md"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class FleetInstaller443PsTest(unittest.TestCase):
    """AC1 — the Windows installer refuses an occupied :443 and takes a
    -HttpsPort parameter."""

    def test_httpsport_parameter_exists(self):
        src = _src(PS1)
        self.assertRegex(
            src, r"\[int\]\$HttpsPort\s*=\s*443",
            "the -HttpsPort parameter (default 443) is missing — the "
            "installer cannot publish on any other port")
        self.assertRegex(src, r"\$HttpsPort -lt 1", "the parameter is unvalidated")

    def test_serve_call_uses_the_parameter(self):
        src = _src(PS1)
        self.assertRegex(
            src, r"tailscale\s+serve\s+--bg\s+--https=\$HttpsPort",
            "the serve call still hardcodes its https port")
        self.assertNotRegex(
            src, r"--https=443(?!\s*http)",
            "a hardcoded --https=443 remains somewhere in the serve path")

    def test_occupied_443_refusal_guard(self):
        src = _src(PS1)
        self.assertIn(
            "serve status", src,
            "the installer never inspects current serve state")
        self.assertRegex(
            src, r"REFUSED: tailscale serve already has a handler",
            "the occupied-:443 refusal is missing")
        self.assertRegex(
            src, r"-HttpsPort <n>",
            "the refusal must name the -HttpsPort escape")

    def test_detection_is_line_scoped_and_port_aware(self):
        """Review rounds 1-2: BEHAVIORAL oracle for the ps1 guard. The
        extracted patterns are compiled and driven against the six
        serve-status shapes; a handler on another port must NOT be an
        occupier, header-first output must not defeat detection, a
        TLS-terminated TCP forward on 443 IS an occupier, and the
        idempotent bypass must anchor the localhost proxy target."""
        src = _src(PS1)
        m = re.search(
            r"\$_ -match '\^https\?://\[\^/: \]\+\(:443\)\?\(\\s\|\$\)'", src)
        self.assertIsNotNone(
            m, "the port-strict web-handler pattern is missing "
            "(host class must exclude ':' so an explicit non-443 port "
            "breaks the match)")
        web = re.compile(r"^https?://[^/: ]+(:443)?(\s|$)")
        tcp = re.compile(r"^\|-- tcp://[^ ]+:443 ")
        ours = re.compile(r"proxy http://127\.0\.0\.1:8765(\D|$)")
        cases = [
            # (status, Port, expect_occupier, expect_ours)
            ("https://host.ts.net (tailnet only)\n"
             "|-- / proxy http://127.0.0.1:32400", 8765, True, False),
            ("https://host.ts.net (tailnet only)\n"
             "|-- / proxy http://127.0.0.1:8765", 8765, True, True),
            ("# Funnel on:\n\nhttps://host.ts.net (tailnet only)\n"
             "|-- / proxy http://127.0.0.1:32400", 8765, True, False),
            ("|-- tcp://foo.ts.net:443 (TLS-terminated TCP, tailnet only)",
             8765, True, False),
            # Round-2 case (e): an explicit NON-443 port is NOT an occupier.
            ("https://host.ts.net:8443 (tailnet only)", 8765, False, False),
            ("No serve config", 8765, False, False),
        ]
        for status, port, want_occ, want_ours in cases:
            lines = status.splitlines()
            occ = any(web.search(ln) or tcp.search(ln) for ln in lines)
            got_ours = any(
                re.search(
                    r"proxy http://127\.0\.0\.1:" + str(port) + r"(\D|$)", ln)
                for ln in lines)
            self.assertEqual(
                occ, want_occ, f"occupier misdetected for status {status!r}")
            self.assertEqual(
                got_ours, want_ours, f"bypass misdetected for {status!r}")


class FleetInstaller443ShTest(unittest.TestCase):
    """AC2 — the Linux installer applies the same refusal."""

    def test_https_port_arg_and_guard(self):
        src = _src(SH)
        self.assertIn('HTTPS_PORT="${5:-443}"', src,
                      "the HTTPS_PORT positional parameter is missing")
        self.assertRegex(
            src, r"--https=\"\$HTTPS_PORT\"",
            "the serve call still hardcodes 443")
        self.assertRegex(
            src, r"REFUSED: tailscale serve already has a handler",
            "the occupied-:443 refusal is missing")
        self.assertRegex(src, r"HTTPS_PORT=<n>",
                         "the refusal must name the HTTPS_PORT escape")

    def test_detection_is_line_scoped_and_port_aware(self):
        """Review rounds 1-2, sh parity: BEHAVIORAL oracle driving the same
        six serve-status shapes through the sh regexes (an explicit non-443
        port must not read as an occupier; the localhost bypass must be
        anchored)."""
        src = _src(SH)
        self.assertIn("while IFS= read -r line", src,
                      "the status must be walked line by line")
        self.assertIn(
            "^https?://[^/: ]+(:443)?([[:space:]]|$)", src,
            "the port-strict web-handler pattern is missing (the host class "
            "must exclude ':' so an explicit non-443 port breaks the match)")
        web = re.compile(r"^https?://[^/: ]+(:443)?(\s|$)")
        tcp = re.compile(r"^\|-- tcp://[^ ]+:443 ")
        cases = [
            ("https://host.ts.net (tailnet only)\n"
             "|-- / proxy http://127.0.0.1:32400", True),
            ("# Funnel on:\n\nhttps://host.ts.net (tailnet only)\n"
             "|-- / proxy http://127.0.0.1:32400", True),
            ("|-- tcp://foo.ts.net:443 (TLS-terminated TCP, tailnet only)",
             True),
            ("https://host.ts.net:8443 (tailnet only)", False),
            ("No serve config", False),
        ]
        for status, want in cases:
            got = any(
                web.search(ln) or tcp.search(ln)
                for ln in status.splitlines())
            self.assertEqual(
                got, want, f"occupier misdetected for status {status!r}")
        self.assertIn(
            'proxy http://127\\.0\\.0\\.1:$PORT([^0-9]|\\$)', src,
            "the idempotent bypass must anchor the localhost proxy target "
            "(a bare substring would bypass on port prefixes)")


class HttpsPortParameterTest(unittest.TestCase):
    """AC3 — the parameter is documented in FLEET.md, replacing the
    hardcoded-443 prose."""

    def test_docs_name_the_parameter(self):
        src = _src(FLEET)
        self.assertRegex(
            src, r"`-HttpsPort <n>`|`install-hub\.ps1 -HttpsPort <n>`",
            "FLEET.md does not document the -HttpsPort parameter")
        self.assertIn("HTTPS_PORT", src,
                      "FLEET.md does not document the Linux HTTPS_PORT arg")
        self.assertNotIn(
            "`tailscale serve --bg --https=443 http://127.0.0.1:<port>`",
            src,
            "the hardcoded-443 command shape is still documented as THE "
            "command (it must be --https=<https-port>)")


if __name__ == "__main__":
    _loader = unittest.TestLoader()
    _argv = [a for a in sys.argv[1:] if not a.startswith("-")]
    if _argv:
        _suite = unittest.TestSuite()
        for _name in _argv:
            _suite.addTests(_loader.loadTestsFromName(_name, sys.modules[__name__]))
    else:
        _suite = _loader.loadTestsFromModule(sys.modules[__name__])
    _result = unittest.TextTestRunner(verbosity=2).run(_suite)
    print("CHECK-PASS" if _result.wasSuccessful() else "CHECK-FAIL")
    sys.exit(0 if _result.wasSuccessful() else 1)
