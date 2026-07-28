import json
import subprocess
import unittest
from unittest import mock

from data_foundation import release_source
from data_foundation.release_source import (
    MAX_RELEASE_RESPONSE_BYTES,
    ReleaseResolutionError,
    parse_latest_release_payload,
    resolve_latest_stable_commit,
    resolve_release_tag_rows,
)


class _Response:
    def __init__(self, payload: bytes, *, status: int = 200):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int) -> bytes:
        return self.payload[:limit]


def _release_payload(**updates) -> bytes:
    payload = {
        "tag_name": "v1.6.0",
        "name": "Actanara v1.6.0",
        "draft": False,
        "prerelease": False,
        "immutable": True,
    }
    payload.update(updates)
    return json.dumps(payload).encode("utf-8")


class ReleaseSourceTests(unittest.TestCase):
    @mock.patch.object(release_source.request, "urlopen")
    @mock.patch.object(release_source.ssl, "create_default_context")
    def test_default_opener_uses_a_verified_tls_context(
        self,
        create_default_context,
        urlopen,
    ):
        expected_response = object()
        expected_context = object()
        create_default_context.return_value = expected_context
        urlopen.return_value = expected_response
        api_request = release_source.request.Request("https://example.test")

        actual = release_source._verified_urlopen(api_request, timeout=17)

        self.assertIs(actual, expected_response)
        create_default_context.assert_called_once()
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 17)
        self.assertIs(urlopen.call_args.kwargs["context"], expected_context)

    def test_latest_release_payload_requires_stable_immutable_safe_metadata(self):
        self.assertEqual(parse_latest_release_payload(_release_payload()), "v1.6.0")
        rejected = (
            _release_payload(draft=True),
            _release_payload(prerelease=True),
            _release_payload(immutable=False),
            _release_payload(immutable=None),
            _release_payload(name="WITHDRAWN"),
            _release_payload(name="[withdrawn] Actanara v1.6.0"),
            _release_payload(name="Actanara v1.6.0 (WiThDrAwN)"),
            _release_payload(name="Actanara v1.6.0 — WITHDRAWN release"),
            _release_payload(tag_name="../main"),
            _release_payload(tag_name="v1..6"),
            b'{"tag_name":"v1","tag_name":"v2","draft":false,'
            b'"prerelease":false,"immutable":true}',
            b'{"tag_name":"v1.6.0"} trailing',
            b"[]",
            b"",
            b"x" * (MAX_RELEASE_RESPONSE_BYTES + 1),
        )
        for payload in rejected:
            with self.subTest(payload=payload[:80]), self.assertRaises(
                ReleaseResolutionError
            ):
                parse_latest_release_payload(payload)

    def test_release_tag_rows_prefer_annotated_peeled_commit(self):
        direct = "a" * 40
        peeled = "b" * 40
        output = (
            f"{direct}\trefs/tags/v1.6.0\n"
            f"{peeled}\trefs/tags/v1.6.0^{{}}\n"
        )
        self.assertEqual(resolve_release_tag_rows("v1.6.0", output), peeled)
        self.assertEqual(
            resolve_release_tag_rows(
                "v1.6.0",
                f"{direct}\trefs/tags/v1.6.0\n",
            ),
            direct,
        )

    def test_release_tag_rows_fail_closed_for_missing_duplicate_or_unexpected_refs(self):
        commit = "a" * 40
        invalid = (
            "",
            f"{commit}\trefs/heads/main\n",
            f"{commit}\trefs/tags/v1.6.0\n{commit}\trefs/tags/v1.6.0\n",
            f"short\trefs/tags/v1.6.0\n",
            f"{commit} refs/tags/v1.6.0\n",
        )
        for output in invalid:
            with self.subTest(output=output), self.assertRaises(
                ReleaseResolutionError
            ):
                resolve_release_tag_rows("v1.6.0", output)

    def test_resolver_uses_exact_tag_refs_and_returns_peeled_commit(self):
        direct = "a" * 40
        peeled = "b" * 40
        calls = []

        def opener(api_request, *, timeout):
            self.assertEqual(timeout, 30)
            self.assertTrue(api_request.full_url.endswith("/releases/latest"))
            return _Response(_release_payload())

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    f"{direct}\trefs/tags/v1.6.0\n"
                    f"{peeled}\trefs/tags/v1.6.0^{{}}\n"
                ),
                stderr="",
            )

        resolved = resolve_latest_stable_commit(opener=opener, runner=runner)

        self.assertEqual(resolved, peeled)
        command, kwargs = calls[0]
        self.assertEqual(command[-2:], ["refs/tags/v1.6.0", "refs/tags/v1.6.0^{}"])
        self.assertNotIn("refs/heads/main", command)
        self.assertIn("protocol.https.allow=always", command)
        self.assertIn("protocol.ext.allow=never", command)
        self.assertFalse(kwargs["check"])
        self.assertTrue(kwargs["cwd"])
        self.assertEqual(
            kwargs["env"]["GIT_CEILING_DIRECTORIES"],
            kwargs["cwd"],
        )
        self.assertEqual(kwargs["env"]["GIT_ALLOW_PROTOCOL"], "https")
        self.assertEqual(kwargs["env"]["GIT_NO_REPLACE_OBJECTS"], "1")
        self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")

    def test_resolver_rejects_non_https_or_credentialed_source_before_network(self):
        def fail(*_args, **_kwargs):
            raise AssertionError("network must not run")

        for source_url in (
            "ext::sh -c touch /tmp/pwned",
            "file:///tmp/repository",
            "http://github.com/Neo-Isshin/actanara.git",
            "https://user@example.test/repository.git",
            "https://example.test/repository.git?ref=main",
        ):
            with self.subTest(source_url=source_url), self.assertRaises(
                ReleaseResolutionError
            ):
                resolve_latest_stable_commit(
                    source_url=source_url,
                    opener=fail,
                    runner=fail,
                )

    def test_resolver_fails_before_git_for_http_or_payload_failure(self):
        def runner(*_args, **_kwargs):
            raise AssertionError("Git must not run")

        for response in (
            _Response(_release_payload(), status=503),
            _Response(_release_payload(immutable=False)),
            _Response(b"x" * (MAX_RELEASE_RESPONSE_BYTES + 1)),
        ):
            with self.subTest(status=response.status), self.assertRaises(
                ReleaseResolutionError
            ):
                resolve_latest_stable_commit(
                    opener=lambda *_args, **_kwargs: response,
                    runner=runner,
                )


if __name__ == "__main__":
    unittest.main()
