"""Failure injection for release publication; no network or real releases."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import publish_release as publisher


SHA = "a" * 40


class FakeGitHub:
    def __init__(self):
        self.refs = {"heads/main": {"object": {"type": "commit", "sha": SHA}}}
        self.releases = {}
        self.contents = {}
        self.next_id = 1
        self.fail_after = None
        self.writes = 0

    def changed(self):
        self.writes += 1
        if self.writes == self.fail_after:
            raise publisher.PublishError("Lost response after server applied change")

    def api(self, method, path, payload=None, *, optional=False):
        if method == "GET":
            if path.startswith("git/ref/"):
                result = self.refs.get(path.removeprefix("git/ref/"))
            elif path.startswith("releases?"):
                result = list(self.releases.values())
            elif path.startswith("releases/tags/"):
                tag = path.removeprefix("releases/tags/")
                result = next((r for r in self.releases.values() if r["tag_name"] == tag), None)
            else:
                result = self.releases.get(int(path.split("/")[-1]))
            if result is None and not optional:
                raise publisher.PublishError("404")
            return copy.deepcopy(result)
        if method == "POST" and path == "git/refs":
            key = payload["ref"].removeprefix("refs/")
            if key in self.refs:
                raise publisher.PublishError("Duplicate tag")
            self.refs[key] = {"object": {"type": "commit", "sha": payload["sha"]}}
            result = self.refs[key]
        elif method == "POST" and path == "releases":
            result = dict(payload, id=self.next_id, assets=[])
            self.releases[self.next_id] = result
            self.next_id += 1
        elif method == "PATCH" and path.startswith("releases/"):
            result = self.releases[int(path.split("/")[-1])]
            result.update(payload)
        else:
            raise AssertionError(f"Forbidden operation: {method} {path}")
        self.changed()
        return copy.deepcopy(result)

    def upload(self, tag, path):
        release = next(r for r in self.releases.values() if r["tag_name"] == tag)
        assert release["draft"], "Uploads must only touch drafts"
        assert path.name not in {a["name"] for a in release["assets"]}
        asset_id = len(self.contents) + 1
        self.contents[asset_id] = path.read_bytes()
        release["assets"].append({"name": path.name, "id": asset_id, "state": "uploaded"})
        self.changed()

    def asset_fingerprint(self, asset):
        content = self.contents[asset["id"]]
        return len(content), hashlib.sha256(content).hexdigest()


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.assets = Path(self.temp.name)
        for name in ("auto_commit-windows-x86_64.exe", "auto_commit-linux-x86_64", "SHA256SUMS"):
            (self.assets / name).write_bytes(name.encode())
        self.expected = {p.name: publisher.fingerprint(p) for p in self.assets.iterdir()}

    def publish(self, api, build="100.1", **kwargs):
        return publisher.publish(api, self.assets, self.expected, sha=SHA,
            version=kwargs.pop("version", "1.0.0+build.42.1"), build_id=build,
            repository="owner/repo", server="https://github.com", run_url="https://github.com/owner/repo/actions/runs/100",
            mode=kwargs.pop("mode", "latest"), **kwargs)

    def assert_consistent_index(self, api):
        index = api.api("GET", "releases/tags/latest", optional=True)
        if index is None:
            return
        self.assertEqual(index["assets"], [])
        tags = [tag for tag in ("build-100-1", "build-101-1") if f"/releases/tag/{tag}" in index["body"]]
        self.assertEqual(len(tags), 1)
        build = api.api("GET", "releases/tags/" + tags[0])
        self.assertFalse(build["draft"])
        publisher.verify_assets(api, build, self.expected, complete=True)
        for name in self.expected:
            self.assertIn(f"/releases/download/{tags[0]}/{name}", index["body"])

    def test_every_interrupted_write_is_consistent_and_retryable(self):
        for existing in (False, True):
            baseline = FakeGitHub()
            if existing:
                self.publish(baseline)
            complete = copy.deepcopy(baseline)
            self.publish(complete, "101.1" if existing else "100.1")
            for failure_at in range(baseline.writes + 1, complete.writes + 1):
                with self.subTest(existing=existing, failure_at=failure_at):
                    api = copy.deepcopy(baseline)
                    before = copy.deepcopy(api.refs.get("tags/latest"))
                    api.fail_after = failure_at
                    with self.assertRaises(publisher.PublishError):
                        self.publish(api, "101.1" if existing else "100.1")
                    self.assert_consistent_index(api)
                    api.fail_after = None
                    self.publish(api, "101.1" if existing else "100.1")
                    self.assert_consistent_index(api)
                    if existing:
                        self.assertEqual(api.refs["tags/latest"], before)

    def test_corrupt_upload_keeps_draft_and_old_index(self):
        api = FakeGitHub()
        self.publish(api)
        original = copy.deepcopy(api.api("GET", "releases/tags/latest"))
        upload = api.upload
        def corrupt(tag, path):
            upload(tag, path)
            api.contents[max(api.contents)] = b"corrupted"
        api.upload = corrupt
        with self.assertRaisesRegex(publisher.PublishError, "verification failed"):
            self.publish(api, "101.1")
        self.assertEqual(api.api("GET", "releases/tags/latest"), original)
        self.assertTrue(api.api("GET", "releases/tags/build-101-1")["draft"])

    def test_draft_recovered_when_tag_endpoint_only_shows_published_releases(self):
        api = FakeGitHub()
        api.fail_after = 3
        with self.assertRaises(publisher.PublishError):
            self.publish(api)
        api.fail_after = None
        original = api.api
        def hide_draft(method, path, *args, **kwargs):
            result = original(method, path, *args, **kwargs)
            if path.startswith("releases/tags/") and result and result["draft"]:
                return None
            return result
        api.api = hide_draft
        self.publish(api)
        self.assert_consistent_index(api)
        self.assertEqual(len(api.releases), 2)

    def test_read_failure_does_not_mean_release_absent(self):
        api = FakeGitHub()
        original = api.api
        def unavailable(method, path, *args, **kwargs):
            if path == "releases/tags/build-100-1":
                raise publisher.PublishError("HTTP 403")
            return original(method, path, *args, **kwargs)
        api.api = unavailable
        with self.assertRaisesRegex(publisher.PublishError, "403"):
            self.publish(api)
        self.assertFalse(api.releases)

    def test_stale_build_does_not_touch_latest(self):
        api = FakeGitHub()
        api.refs["heads/main"]["object"]["sha"] = "b" * 40
        self.assertIn("Skipped", self.publish(api))
        self.assertEqual(api.writes, 0)

    def test_published_build_retry_is_no_overwrite(self):
        api = FakeGitHub()
        self.publish(api, mode="tag", tag="v1.0.0", version="1.0.0")
        writes = api.writes
        self.publish(api, mode="tag", tag="v1.0.0", version="1.0.0")
        self.assertEqual(api.writes, writes)
        self.assertNotIn("tags/latest", api.refs)

    def test_mismatched_tag_ref_fails_closed(self):
        api = FakeGitHub()
        api.refs["tags/build-100-1"] = {"object": {"type": "commit", "sha": "b" * 40}}
        with self.assertRaisesRegex(publisher.PublishError, "different commit"):
            self.publish(api)
        self.assertEqual(api.writes, 0)

    def test_provenance_rejects_mixed_attempts_and_corrupt_artifacts(self):
        artifacts = self.assets / "artifacts"
        for number, (artifact, (source_name, _)) in enumerate(publisher.FILES.items()):
            folder = artifacts / artifact
            folder.mkdir(parents=True)
            path = folder / source_name
            path.write_bytes(b"program")
            size, digest = publisher.fingerprint(path)
            manifest = dict(sha=SHA, version="1.0.0", build_id=f"100.{number+1}", file=source_name, size=size, sha256=digest)
            (folder / "build-manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(publisher.PublishError, "Mixed build attempts"):
            publisher.prepare_assets(artifacts, self.assets, SHA, "1.0.0")
        path.write_bytes(b"different")
        with self.assertRaisesRegex(publisher.PublishError, "checksum mismatch"):
            publisher.prepare_assets(artifacts, self.assets, SHA, "1.0.0")


if __name__ == "__main__":
    unittest.main()
