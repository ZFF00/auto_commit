"""Publish verified immutable builds, then update the latest download index.

Only the release index changes in place. Published build assets and source tags
are never overwritten or deleted. A retry reconciles drafts and verifies bytes
already uploaded before continuing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import quote


FILES = {
    "auto_commit-windows-x86_64": ("auto_commit.exe", "auto_commit-windows-x86_64.exe"),
    "auto_commit-linux-x86_64": ("auto_commit", "auto_commit-linux-x86_64"),
}


class PublishError(RuntimeError):
    pass


def fingerprint(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return path.stat().st_size, digest.hexdigest()


class GitHub:
    def __init__(self, repository: str) -> None:
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
            raise PublishError("Invalid repository")
        self.base = f"repos/{repository}"

    def api(self, method: str, path: str, payload: dict | None = None, *, optional=False):
        command = ["gh", "api", "--method", method, f"{self.base}/{path}"]
        if payload is not None:
            command.extend(["--input", "-"])
        result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                                capture_output=True, text=True, encoding="utf-8", timeout=120)
        if result.returncode:
            if optional and "(HTTP 404)" in result.stderr:
                return None
            raise PublishError(f"GitHub {method} {path} failed: {result.stderr.strip()}")
        return json.loads(result.stdout) if result.stdout.strip() else None

    def upload(self, tag: str, path: Path) -> None:
        result = subprocess.run(["gh", "release", "upload", tag, str(path),
                                 "--repo", self.base.removeprefix("repos/")],
                                capture_output=True, text=True, encoding="utf-8", timeout=600)
        if result.returncode:
            raise PublishError(f"Asset upload failed: {result.stderr.strip()}")

    def asset_fingerprint(self, asset: dict) -> tuple[int, str]:
        with tempfile.TemporaryDirectory(prefix="release_verify_") as directory:
            path = Path(directory) / "asset"
            with path.open("wb") as output:
                result = subprocess.run(["gh", "api", f"{self.base}/releases/assets/{asset['id']}",
                                         "-H", "Accept: application/octet-stream"],
                                        stdout=output, stderr=subprocess.PIPE, timeout=600)
            if result.returncode:
                raise PublishError("Could not download uploaded asset for verification")
            return fingerprint(path)


def prepare_assets(artifacts: Path, destination: Path, sha: str, version: str) -> tuple[dict, str]:
    """Verify both runners built the same version/commit/attempt before publishing."""
    expected = {}
    build_ids = set()
    for artifact, (source_name, asset_name) in FILES.items():
        directory = artifacts / artifact
        manifest = json.loads((directory / "build-manifest.json").read_text(encoding="utf-8"))
        if manifest.get("sha") != sha or manifest.get("version") != version or manifest.get("file") != source_name:
            raise PublishError(f"Build identity mismatch: {artifact}; rerun all build jobs")
        build_id = manifest.get("build_id", "")
        if not re.fullmatch(r"\d+\.\d+", build_id):
            raise PublishError("Invalid build identity")
        build_ids.add(build_id)
        source = directory / source_name
        value = fingerprint(source)
        if value != (manifest.get("size"), manifest.get("sha256")):
            raise PublishError(f"Artifact checksum mismatch: {artifact}")
        shutil.copyfile(source, destination / asset_name)
        expected[asset_name] = value
    if len(build_ids) != 1:
        raise PublishError("Mixed build attempts; rerun all build jobs")
    checksums = destination / "SHA256SUMS"
    checksums.write_text("".join(f"{digest}  {name}\n" for name, (_, digest) in expected.items()), encoding="ascii")
    expected[checksums.name] = fingerprint(checksums)
    return expected, build_ids.pop()


def ensure_tag(api, tag: str, sha: str) -> None:
    ref = api.api("GET", f"git/ref/tags/{quote(tag, safe='')}", optional=True)
    if ref is None:
        api.api("POST", "git/refs", {"ref": f"refs/tags/{tag}", "sha": sha})
        ref = api.api("GET", f"git/ref/tags/{quote(tag, safe='')}")
    obj = ref["object"]
    for _ in range(10):
        if obj["type"] != "tag":
            break
        obj = api.api("GET", f"git/tags/{obj['sha']}")["object"]
    if obj["type"] != "commit" or obj["sha"] != sha:
        raise PublishError(f"Tag {tag} points to a different commit; refusing to move it")


def find_release(api, tag: str) -> dict | None:
    release = api.api("GET", f"releases/tags/{quote(tag, safe='')}", optional=True)
    if release is not None:
        return release
    # Some GitHub API versions only return published releases from /tags/.
    # Authenticated release listings also include our recoverable drafts.
    page = 1
    while True:
        releases = api.api("GET", f"releases?per_page=100&page={page}")
        matches = [item for item in releases if item["tag_name"] == tag]
        if len(matches) > 1:
            raise PublishError(f"Multiple releases for tag {tag}")
        if matches:
            return matches[0]
        if len(releases) < 100:
            return None
        page += 1


def verify_assets(api, release: dict, expected: dict, *, complete: bool) -> None:
    assets = release.get("assets", [])
    names = [asset["name"] for asset in assets]
    if len(names) != len(set(names)) or set(names) - expected.keys():
        raise PublishError("Unexpected or duplicate release assets")
    if complete and set(names) != expected.keys():
        raise PublishError("Release assets are incomplete")
    for asset in assets:
        if asset.get("state") != "uploaded" or api.asset_fingerprint(asset) != expected[asset["name"]]:
            raise PublishError(f"Uploaded asset verification failed: {asset['name']}")


def publish(api, assets: Path, expected: dict, *, sha: str, version: str,
            build_id: str, repository: str, server: str, run_url: str, mode: str, tag: str = "") -> str:
    rolling = mode == "latest"
    if rolling:
        if api.api("GET", "git/ref/heads/main")["object"]["sha"] != sha:
            return "Skipped outdated main build"
        tag = f"build-{build_id.replace('.', '-')}"
    elif tag != f"v{version}":
        raise PublishError("Release tag does not match program version")
    ensure_tag(api, tag, sha)
    root_url = f"{server.rstrip('/')}/{repository}"
    title = f"开发构建 {version}" if rolling else tag
    body = f"程序版本：{version}\n\n源码提交：{sha}\n\n构建记录：{run_url}\n"
    prerelease = rolling or "-" in version
    release = find_release(api, tag)
    if release is None:
        release = api.api("POST", "releases", {"tag_name": tag, "name": title, "body": body,
                           "draft": True, "prerelease": prerelease})
    if release["name"] != title or release["body"] != body or release["prerelease"] != prerelease:
        raise PublishError("Existing release identity differs; refusing to overwrite")
    verify_assets(api, release, expected, complete=not release["draft"])
    if release["draft"]:
        existing = {asset["name"] for asset in release.get("assets", [])}
        for name in sorted(expected.keys() - existing):
            api.upload(tag, assets / name)
        release = api.api("GET", f"releases/{release['id']}")
        verify_assets(api, release, expected, complete=True)
        api.api("PATCH", f"releases/{release['id']}", {"draft": False,
                "make_latest": "false" if prerelease else "true"})
        release = api.api("GET", f"releases/{release['id']}")
        if release["draft"] or {a["name"] for a in release["assets"]} != expected.keys():
            raise PublishError("Release publication could not be confirmed; rerun to reconcile")
    if not rolling:
        return f"Published {tag}"

    # The latest tag is a permanent anchor for a download index, never a source
    # version. One API request replaces the entire index after assets are ready.
    if api.api("GET", "git/ref/heads/main")["object"]["sha"] != sha:
        return f"Verified {tag}; newer main exists, leaving latest unchanged"
    index_body = (
        "最新开发版下载入口（预发布）。请使用下方下载链接。\n\n"
        "latest 标签固定用于此入口，不表示当前构建源码；源码以本页提交链接和独立构建标签为准。\n\n"
        f"程序版本：{version}\n\n[源码提交 {sha}]({root_url}/commit/{sha})\n\n"
        f"[完整构建 {tag}]({root_url}/releases/tag/{tag}) · [构建记录]({run_url})\n\n"
    )
    for name, (_, digest) in expected.items():
        index_body += f"- [{name}]({root_url}/releases/download/{tag}/{name})\n  SHA-256：`{digest}`\n"
    index = find_release(api, "latest")
    if index and (index.get("assets") or index.get("draft") or not index.get("prerelease")):
        raise PublishError("Existing latest is not an empty prerelease index; manual migration required")
    latest_ref = api.api("GET", "git/ref/tags/latest", optional=True)
    if latest_ref is None:
        api.api("POST", "git/refs", {"ref": "refs/tags/latest", "sha": sha})
    fields = {"name": f"latest · {version}", "body": index_body,
              "prerelease": True, "make_latest": "false"}
    if index:
        api.api("PATCH", f"releases/{index['id']}", fields)
    else:
        api.api("POST", "releases", dict(fields, tag_name="latest", draft=False))
    confirmed = api.api("GET", "releases/tags/latest")
    if any(confirmed.get(key) != fields[key] for key in ("name", "body", "prerelease")) or confirmed.get("assets"):
        raise PublishError("Latest index update could not be confirmed; rerun to reconcile")
    return f"Latest now links to verified {tag}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("latest", "tag"), required=True)
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts"))
    args = parser.parse_args()
    sha, version = os.environ["BUILD_SHA"], os.environ["BUILD_VERSION"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or not re.fullmatch(r"[0-9A-Za-z.+-]+", version):
        raise PublishError("Invalid build version or commit")
    with tempfile.TemporaryDirectory(prefix="release_assets_") as directory:
        assets = Path(directory)
        expected, build_id = prepare_assets(args.artifacts, assets, sha, version)
        print(publish(GitHub(os.environ["GH_REPO"]), assets, expected,
            sha=sha, version=version, build_id=build_id, repository=os.environ["GH_REPO"],
            server=os.environ["GITHUB_SERVER_URL"], run_url=os.environ["RUN_URL"],
            mode=args.mode, tag=os.environ.get("RELEASE_TAG", "")))


if __name__ == "__main__":
    main()
