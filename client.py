"""GitHub Repository Inspector: URL parsing, branch detection, tree scan, file metadata."""
import re
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import quote

import requests

from config import GITHUB_API, GITHUB_TIMEOUT, GITHUB_TOKEN

URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?"
    r"(?:/(?:tree|blob)/(.+?))?/?$"
)


class GitHubError(Exception):
    """Raised for any user-presentable GitHub problem."""


@dataclass
class FileEntry:
    path: str
    size: int
    sha: str


@dataclass
class RepoInspection:
    owner: str
    repo: str
    branch: str
    default_branch: str
    private: bool
    description: str
    commit_sha: str
    start_path: str
    truncated: bool
    files: List[FileEntry] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)


def _headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "User-Agent": "CopyPasteBot"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def _get(path: str, allow_404: bool = False) -> Optional[dict]:
    try:
        r = requests.get(f"{GITHUB_API}{path}", headers=_headers(), timeout=GITHUB_TIMEOUT)
    except requests.RequestException as exc:
        raise GitHubError(f"Network error talking to GitHub: {exc}") from exc
    if r.status_code in (404, 422) and allow_404:
        return None
    if r.status_code == 404:
        raise GitHubError("Repository or ref not found (or it is private and GITHUB_TOKEN is missing).")
    if r.status_code in (401, 403):
        if r.headers.get("X-RateLimit-Remaining") == "0":
            raise GitHubError("GitHub rate limit reached. Set GITHUB_TOKEN and try again later.")
        raise GitHubError("GitHub denied access. Check GITHUB_TOKEN permissions.")
    if not r.ok:
        raise GitHubError(f"GitHub API error {r.status_code}.")
    return r.json()


def parse_github_url(url: str):
    """Return (owner, repo, remainder_after_tree_or_blob_or_None)."""
    m = URL_RE.match(url.strip())
    if not m:
        raise GitHubError("That does not look like a GitHub repository URL.")
    return m.group(1), m.group(2), m.group(3)


def _resolve_ref(owner: str, repo: str, remainder: str):
    """Branch names may contain '/', so try the longest prefix first.
    Returns (ref, commit_json, subpath)."""
    parts = remainder.split("/")
    for i in range(len(parts), 0, -1):
        ref = "/".join(parts[:i])
        data = _get(f"/repos/{owner}/{repo}/commits/{quote(ref, safe='')}", allow_404=True)
        if data:
            return ref, data, "/".join(parts[i:])
    raise GitHubError("Could not resolve the branch/ref in that URL.")


def inspect_repository(url: str) -> RepoInspection:
    """Full inspection: owner/repo detect, branch detect, tree scan, file metadata."""
    owner, repo, remainder = parse_github_url(url)

    info = _get(f"/repos/{owner}/{repo}")
    default_branch = info["default_branch"]

    if remainder:
        branch, commit, subpath = _resolve_ref(owner, repo, remainder)
    else:
        branch, subpath = default_branch, ""
        commit = _get(f"/repos/{owner}/{repo}/commits/{quote(branch, safe='')}")

    tree_sha = commit["commit"]["tree"]["sha"]
    tree = _get(f"/repos/{owner}/{repo}/git/trees/{tree_sha}?recursive=1")

    files = [
        FileEntry(path=t["path"], size=t.get("size", 0), sha=t["sha"])
        for t in tree.get("tree", [])
        if t.get("type") == "blob"
    ]
    files.sort(key=lambda f: f.path.lower())

    return RepoInspection(
        owner=owner,
        repo=info["name"],
        branch=branch,
        default_branch=default_branch,
        private=bool(info.get("private")),
        description=info.get("description") or "",
        commit_sha=commit["sha"],
        start_path=subpath if any(f.path.startswith(subpath + "/") for f in files) else "",
        truncated=bool(tree.get("truncated")),
        files=files,
    )


def download_file(owner: str, repo: str, path: str, ref: str) -> bytes:
    """Download raw file bytes (used by later workflow steps)."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{quote(path)}"
    h = _headers()
    h["Accept"] = "application/vnd.github.raw+json"
    try:
        r = requests.get(url, headers=h, params={"ref": ref}, timeout=GITHUB_TIMEOUT)
    except requests.RequestException as exc:
        raise GitHubError(f"Network error downloading {path}: {exc}") from exc
    if not r.ok:
        raise GitHubError(f"Could not download {path} (HTTP {r.status_code}).")
    return r.content
