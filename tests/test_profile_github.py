import hashlib
import io
import json
import unittest
import zipfile

from scripts.profile_github import (
    HTTPResponse,
    ProfileGitHub,
    ProfileGitHubError,
    TrustedWorkflow,
    strict_json,
)


SHA = "a" * 40
PIN = "b" * 40
RUN_ID = 17
TARGET = "https://github.com/RetireGolden/example/actions/runs/17"


def response(value, status=200, headers=None):
    body = value if isinstance(value, bytes) else json.dumps(value).encode()
    return HTTPResponse(status, headers or {}, body)


class FakeTransport:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def __call__(self, method, url, headers, timeout, cap, data):
        self.calls.append((method, url, dict(headers), cap, data))
        return self.handler(method, url, headers, cap, data)


class ProfileGitHubTests(unittest.TestCase):
    def setUp(self):
        self.config = TrustedWorkflow(
            "RetireGolden", "example", "main", reusable_sha=PIN
        )

    def client(self, handler):
        self.transport = FakeTransport(handler)
        return ProfileGitHub(self.config, "secret", self.transport)

    def valid_run(self, **overrides):
        run = {
            "id": RUN_ID,
            "run_attempt": 2,
            "workflow_id": 9,
            "head_sha": SHA,
            "head_branch": "topic",
            "event": "pull_request",
            "path": self.config.caller_path,
            "repository": {"full_name": self.config.full_name},
            "head_repository": {"full_name": self.config.full_name},
            "referenced_workflows": [
                {"path": f"{self.config.reusable_path}@{PIN}", "sha": PIN}
            ],
            "status": "completed",
            "conclusion": "success",
        }
        run.update(overrides)
        return run

    def provenance_handler(self, jobs=None, blob_sha=SHA):
        def handler(method, url, headers, cap, data):
            if "/actions/workflows/" in url:
                return response(
                    {"id": 9, "path": self.config.caller_path, "state": "active"}
                )
            if "/contents/" in url:
                return response({"type": "file", "sha": blob_sha})
            if "/attempts/2/jobs" in url:
                return response({"jobs": jobs or []})
            raise AssertionError(url)

        return handler

    def test_strict_json_rejects_duplicates_nonfinite_invalid_utf8_surrogates_and_depth(
        self,
    ):
        for raw in (
            b'{"a":1,"a":2}',
            b'{"\\u0061":1,"a":2}',
            b'{"a":NaN}',
            b'{"a":Infinity}',
            b'{"a":-Infinity}',
            b"\xff",
            b'{"key":"\\uD800"}',
            b'{"\\uD800":1}',
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(ProfileGitHubError):
                    strict_json(raw)
        with self.assertRaises(ProfileGitHubError):
            strict_json(("[" * 25 + "0" + "]" * 25).encode())
        with self.assertRaises(ProfileGitHubError):
            strict_json(("[" * 10000 + "0" + "]" * 10000).encode())

    def test_pagination_rejects_malformed_pages_overflow_and_invalid_endpoints(self):
        client = self.client(lambda *args: response({"wrong": []}))
        with self.assertRaises(ProfileGitHubError):
            client.paginated("/repos/RetireGolden/example/items", "items")
        for endpoint in (
            "/repos/x/y#frag",
            "/repos/x/y\r",
            "/repos/x/y\n",
            "https://api.github.com/x",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ProfileGitHubError):
                    client.paginated(endpoint, "items")
        many = list(range(100))
        client = self.client(lambda *args: response({"items": many}))
        with self.assertRaises(ProfileGitHubError):
            client.paginated("/repos/RetireGolden/example/items", "items", 100)

    def test_verify_run_positive_with_jobs_and_canonical_blob_sha(self):
        jobs = [{"name": "producer", "conclusion": "success", "steps": []}]
        client = self.client(self.provenance_handler(jobs, blob_sha=SHA))
        verified = client.verify_run(self.valid_run(), SHA, require_success=True)
        self.assertEqual(
            (verified.run_id, verified.attempt, verified.head_sha), (RUN_ID, 2, SHA)
        )
        self.assertEqual(len(verified.jobs), 1)
        self.assertEqual(verified.jobs[0]["name"], "producer")

    def test_verifies_pr_and_dispatch_and_rejects_pin_path_id_blob_headrepo_and_attempt_mismatch(
        self,
    ):
        client = self.client(self.provenance_handler())
        verified = client.verify_run(self.valid_run(), SHA, require_success=True)
        self.assertEqual((verified.run_id, verified.attempt), (RUN_ID, 2))
        dispatch = self.valid_run(
            event="workflow_dispatch", head_branch="main", head_sha="c" * 40
        )
        dispatch_client = self.client(self.provenance_handler())
        dispatch_client.verify_run(dispatch, SHA)
        for changed in (
            {"referenced_workflows": []},
            {"path": "other.yml"},
            {"workflow_id": 10},
            {"head_repository": {"full_name": "fork/example"}},
            {"run_attempt": 0},
            {"run_attempt": 1001},
            {"repository": None},
            {"repository": []},
            {"head_repository": None},
            {"head_branch": []},
        ):
            with self.subTest(changed=changed):
                with self.assertRaises(ProfileGitHubError):
                    client.verify_run(self.valid_run(**changed), SHA)
        blob_client = self.client(self.provenance_handler())
        with self.assertRaises(ProfileGitHubError):
            blob_client.verify_run(self.valid_run(), "d" * 40)
        for bad_blob in (None, 123, True):
            with self.subTest(bad_blob=bad_blob):
                with self.assertRaises(ProfileGitHubError):
                    client.verify_run(self.valid_run(), bad_blob)
        for bad_head in (None, 123, "not-a-sha"):
            with self.subTest(bad_head=bad_head):
                with self.assertRaises(ProfileGitHubError):
                    client.verify_run(self.valid_run(head_sha=bad_head), SHA)

    def test_successful_steps_allows_later_cancel_but_not_skipped_job(self):
        jobs = [
            {
                "name": "producer",
                "conclusion": "cancelled",
                "steps": [
                    {"name": "accept", "status": "completed", "conclusion": "success"},
                    {"name": "upload", "status": "completed", "conclusion": "success"},
                ],
            }
        ]
        verified = self.client(self.provenance_handler(jobs)).verify_run(
            self.valid_run(), SHA
        )
        self.assertTrue(
            self.client(lambda *args: response({})).successful_steps(
                verified, "producer", ["accept", "upload"]
            )
        )
        skipped = type(verified)(
            **{**verified.__dict__, "jobs": ({**jobs[0], "conclusion": "skipped"},)}
        )
        self.assertFalse(
            self.client(lambda *args: response({})).successful_steps(
                skipped, "producer", ["accept"]
            )
        )

    def test_create_status_posts_valid_payload_and_rejects_invalid_inputs(self):
        captured = {}

        def handler(method, url, headers, cap, data):
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = headers
            captured["payload"] = json.loads(data.decode())
            return response(
                {"id": 1, "state": "success", "context": "openrouter-profile"}
            )

        client = self.client(handler)
        result = client.create_status(SHA, "success", TARGET, "review ok ✓")
        self.assertEqual(result["context"], "openrouter-profile")
        self.assertEqual(captured["method"], "POST")
        self.assertTrue(captured["url"].endswith(f"/statuses/{SHA}"))
        self.assertIn("Authorization", captured["headers"])
        self.assertEqual(
            captured["payload"],
            {
                "state": "success",
                "target_url": TARGET,
                "description": "review ok ✓",
                "context": "openrouter-profile",
            },
        )
        bad_cases = (
            (SHA, True, TARGET, "ok"),
            (
                SHA,
                "success",
                "https://evil.com/RetireGolden/example/actions/runs/17",
                "ok",
            ),
            (SHA, "success", TARGET + "#frag", "ok"),
            (SHA, "success", TARGET, ""),
            (SHA, "success", TARGET, "x" * 141),
            ("short", "success", TARGET, "ok"),
        )
        for args in bad_cases:
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    client.create_status(*args)

    def test_artifact_transport_auth_redirect_digest_and_zip_safety(self):
        def zipped(name, data, **kwargs):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(zipfile.ZipInfo(name), data, **kwargs)
            return stream.getvalue()

        verified = self.client(self.provenance_handler()).verify_run(
            self.valid_run(), SHA
        )
        safe_zip = zipped("receipt.json", b"ok")
        digest = "sha256:" + hashlib.sha256(safe_zip).hexdigest()

        def artifact_handler(method, url, headers, cap, data):
            if "/artifacts?" in url:
                return response(
                    {
                        "artifacts": [
                            {
                                "id": 3,
                                "name": "proof",
                                "expired": False,
                                "digest": digest,
                                "workflow_run": {"id": RUN_ID, "head_sha": SHA},
                            }
                        ]
                    }
                )
            if url.startswith("https://api.github.com/") and url.endswith("/zip"):
                self.assertIn("Authorization", headers)
                return response(
                    b"", 302, {"Location": "https://storage.example/presigned"}
                )
            if url.startswith("https://storage.example/"):
                self.assertNotIn("Authorization", headers)
                return response(safe_zip)
            raise AssertionError(url)

        self.assertEqual(
            self.client(artifact_handler).artifact(
                verified, "proof", 100, "receipt.json"
            ),
            b"ok",
        )

        bad_digest_zip = zipped("receipt.json", b"ok")
        bad_digest = "sha256:" + ("c" * 64)

        def bad_digest_handler(method, url, headers, cap, data):
            if "/artifacts?" in url:
                return response(
                    {
                        "artifacts": [
                            {
                                "id": 3,
                                "name": "proof",
                                "expired": False,
                                "digest": bad_digest,
                            }
                        ]
                    }
                )
            if url.endswith("/zip"):
                return response(b"", 302, {"Location": "https://storage.example/p"})
            return response(bad_digest_zip)

        with self.assertRaises(ProfileGitHubError):
            self.client(bad_digest_handler).artifact(
                verified, "proof", 100, "receipt.json"
            )

        unsafe_cases = (
            (zipped("../escape", b"x"), 100, None),
            (zipped("receipt.json", b"x" * 20), 10, None),
            (b"not-a-zip", 100, None),
        )
        for bad_zip, bound, expected_name in unsafe_cases:
            with self.subTest(bound=bound, expected_name=expected_name):

                def bad_handler(method, url, headers, cap, data, bad_zip=bad_zip):
                    if "/artifacts?" in url:
                        return response(
                            {
                                "artifacts": [
                                    {"id": 3, "name": "proof", "expired": False}
                                ]
                            }
                        )
                    if url.endswith("/zip"):
                        return response(
                            b"", 302, {"Location": "https://storage.example/p"}
                        )
                    return response(bad_zip)

                with self.assertRaises(ProfileGitHubError):
                    self.client(bad_handler).artifact(
                        verified, "proof", bound, expected_name
                    )

        multi_stream = io.BytesIO()
        with zipfile.ZipFile(multi_stream, "w") as archive:
            archive.writestr("a.txt", b"a")
            archive.writestr("b.txt", b"b")
        multi_zip = multi_stream.getvalue()

        def multi_handler(method, url, headers, cap, data):
            if "/artifacts?" in url:
                return response(
                    {"artifacts": [{"id": 3, "name": "proof", "expired": False}]}
                )
            if url.endswith("/zip"):
                return response(b"", 302, {"Location": "https://storage.example/p"})
            return response(multi_zip)

        with self.assertRaises(ProfileGitHubError):
            self.client(multi_handler).artifact(verified, "proof", 100)

        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            info = zipfile.ZipInfo("receipt.json")
            info.flag_bits |= 1
            archive.writestr(info, b"secret")
        encrypted_bytes = bytearray(stream.getvalue())
        # zipfile clears encryption flags when writing an unencrypted entry.
        # Mark both headers after writing to exercise the reader's rejection.
        encrypted_bytes[6] |= 1
        encrypted_bytes[encrypted_bytes.index(b"PK\x01\x02") + 8] |= 1
        encrypted_zip = bytes(encrypted_bytes)

        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            info = zipfile.ZipInfo("link.txt")
            info.external_attr = (0o120000 << 16) | 0o100644
            archive.writestr(info, b"target")
        symlink_zip = stream.getvalue()

        stream = io.BytesIO()
        with zipfile.ZipFile(
            stream, "w", zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            archive.writestr("receipt.json", b"\x00" * 200)
        bomb_zip = stream.getvalue()

        for bad_zip in (encrypted_zip, symlink_zip, bomb_zip):
            with self.subTest(kind=bad_zip[:4]):

                def zip_handler(method, url, headers, cap, data, bad_zip=bad_zip):
                    if "/artifacts?" in url:
                        return response(
                            {
                                "artifacts": [
                                    {"id": 3, "name": "proof", "expired": False}
                                ]
                            }
                        )
                    if url.endswith("/zip"):
                        return response(
                            b"", 302, {"Location": "https://storage.example/p"}
                        )
                    return response(bad_zip)

                with self.assertRaises(ProfileGitHubError):
                    self.client(zip_handler).artifact(
                        verified, "proof", 50, "receipt.json"
                    )

        for artifacts in (
            [{"id": 3, "name": "proof", "expired": True}],
            [
                {"id": 3, "name": "proof", "expired": False},
                {"id": 4, "name": "proof", "expired": False},
            ],
        ):
            with self.subTest(artifacts=artifacts):
                client = self.client(
                    lambda *args, artifacts=artifacts: response(
                        {"artifacts": artifacts}
                    )
                )
                with self.assertRaises(ProfileGitHubError):
                    client.artifact(verified, "proof", 100)

    def test_maintainer_404_is_false_but_other_http_errors_fail_closed(self):
        client = self.client(lambda *args: response({"permission": "maintain"}))
        self.assertTrue(client.maintainer("alice-ops"))
        self.assertFalse(client.maintainer("alice/../../x"))
        self.assertFalse(
            self.client(lambda *args: response({"permission": "read"})).maintainer(
                "alice"
            )
        )
        self.assertFalse(
            self.client(lambda *args: response(b"{}", status=404)).maintainer("ghost")
        )
        for status in (401, 403, 429, 500):
            with self.subTest(status=status):
                with self.assertRaises(ProfileGitHubError):
                    self.client(
                        lambda *args, status=status: response(b"{}", status=status)
                    ).maintainer("alice")

    def test_permissions_and_unsafe_artifact_redirect(self):
        with self.assertRaises(ProfileGitHubError):
            ProfileGitHub._safe_artifact_url("https://user@127.0.0.1/zip")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
