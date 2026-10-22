"""GitHub Release 上传:创建/复用 release 并上传资产(无 gh CLI 环境用)。

用法:
    python scripts/github_release_upload.py <tag> <资产1> [资产2 ...]

- token 从 git credential manager(https://github.com)获取,不落盘。
- release 已存在(422)时自动复用;同名资产已存在时先删再传(覆盖语义)。
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

OWNER, REPO = "Asaceoo", "bili-transcriber"
API = f"https://api.github.com/repos/{OWNER}/{REPO}"
UPLOAD = f"https://uploads.github.com/repos/{OWNER}/{REPO}/releases"


def _token() -> str:
    out = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, check=True,
    ).stdout
    for line in out.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("git credential manager 中未找到 github.com 的 token")


def _api(method: str, url: str, token: str, data: dict | bytes | None = None,
         headers: dict | None = None):
    body = None
    h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
         "User-Agent": "bili-transcriber-release"}
    if headers:
        h.update(headers)
    if data is not None:
        if isinstance(data, bytes):
            body = data
        else:
            body = json.dumps(data).encode()
            h.setdefault("Content-Type", "application/json")  # 缺此头 GitHub 会 422 "is not an object"
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw and resp.headers.get_content_type() == "application/json" else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, None


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    tag = sys.argv[1]
    assets = [Path(p) for p in sys.argv[2:]]
    for a in assets:
        if not a.is_file():
            print(f"资产不存在: {a}")
            sys.exit(2)

    tok = _token()
    # 已有该 tag 的 release?
    status, rel = _api("GET", f"{API}/releases/tags/{tag}", tok)
    if status == 200 and rel:
        release_id, upload_url = rel["id"], rel["upload_url"].split("{")[0]
        print(f"[release] 复用已有 release {tag} (id={release_id})")
    else:
        status, rel = _api("POST", f"{API}/releases", tok, data={
            "tag_name": tag,
            "target_commitish": "main",
            "name": f"bili-transcriber {tag}",
            "body": (f"bili-transcriber {tag} 发布产物。\n\n"
                     "- `bili-transcriber-setup-*.exe` 安装版\n"
                     "- `bili-transcriber-single-*.exe` 单文件便携版\n"
                     "- `bili-transcriber-portable-*.zip` 便携包\n"
                     "- `bili-transcriber-gpu-*.zip` GPU 加速包(可选)\n"
                     "- `*-skill-*.zip` 配套 AI 智能体技能包\n\n"
                     "详见仓库 docs/ 用户手册与技术手册。"),
            "draft": False,
            "prerelease": False,
        })
        if status not in (201, 200) or not rel:
            print(f"[release] 创建失败 HTTP {status}: {rel}")
            sys.exit(1)
        release_id, upload_url = rel["id"], rel["upload_url"].split("{")[0]
        print(f"[release] 已创建 release {tag} (id={release_id})")

    existing = {a["name"]: a["id"] for a in (rel.get("assets") or [])} if rel else {}
    for a in assets:
        name = a.name
        if name in existing:
            st, _ = _api("DELETE", f"{API}/releases/assets/{existing[name]}", tok)
            print(f"[asset] 删除旧资产 {name} (HTTP {st})")
        size = a.stat().st_size
        # 文件流式上传:GPU 包可达 2GB+,read_bytes() 会整份驻留内存,低配机易 OOM
        with a.open("rb") as fh:
            st, body = _api(
                "POST", f"{UPLOAD}/{release_id}/assets?name={urllib.parse.quote(name)}", tok,
                data=fh,
                headers={"Content-Type": "application/octet-stream",
                         "Content-Length": str(size)},
            )
        ok = st in (201, 200)
        print(f"[asset] {'OK ' if ok else 'FAIL'} {name} ({size/1e6:.1f} MB, HTTP {st})")
        if not ok:
            print(f"        {body}")
            sys.exit(1)
    print("[release] 全部资产上传完成")


if __name__ == "__main__":
    main()
