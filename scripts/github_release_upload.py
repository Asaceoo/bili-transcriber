"""GitHub Release 上传:创建/复用 release 并上传资产(无 gh CLI 环境用)。

用法:
    python scripts/github_release_upload.py <tag> <资产1> [资产2 ...] [--notes 说明.md]

- token 从 git credential manager(https://github.com)获取,不落盘。
- release 已存在(422)时自动复用;同名资产已存在时先删再传(覆盖语义)。
- `--notes` 指定更新说明文件(Markdown),创建时作为 release 正文;
  复用已有 release 时也会 PATCH 覆盖正文,避免说明与产物版本脱节。
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

OWNER, REPO = "Asaceoo", "bili-transcriber"
API = f"https://api.github.com/repos/{OWNER}/{REPO}"
UPLOAD = f"https://uploads.github.com/repos/{OWNER}/{REPO}/releases"
ATTEMPTS = 3  # 单资产最多传几次(1.3GB 的 GPU 包在大陆网络上常被 408 打断)


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


def _api(method: str, url: str, token: str, data: dict | bytes | io.BufferedReader | None = None,
         headers: dict | None = None):
    body = None
    h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
         "User-Agent": "bili-transcriber-release"}
    if headers:
        h.update(headers)
    if data is not None:
        if isinstance(data, bytes):
            body = data
        elif isinstance(data, dict):
            body = json.dumps(data).encode()
            h.setdefault("Content-Type", "application/json")  # 缺此头 GitHub 会 422 "is not an object"
        else:
            body = data  # 二进制文件流(dict/bytes 之外的 IO 对象,urllib 原生支持流式发送)
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
    argv = sys.argv[1:]
    notes_text = ""
    if "--notes" in argv:
        i = argv.index("--notes")
        if i + 1 >= len(argv):
            print("--notes 需要跟一个文件路径")
            sys.exit(2)
        notes_path = Path(argv[i + 1])
        if not notes_path.is_file():
            print(f"说明文件不存在: {notes_path}")
            sys.exit(2)
        notes_text = notes_path.read_text(encoding="utf-8")
        del argv[i:i + 2]
    if len(argv) < 2:
        print(__doc__)
        sys.exit(2)
    tag = argv[0]
    assets = [Path(p) for p in argv[1:]]
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
        if notes_text and rel.get("body") != notes_text:
            st, _ = _api("PATCH", f"{API}/releases/{release_id}", tok,
                         data={"body": notes_text})
            print(f"[release] 更新说明已覆盖 (HTTP {st})")
    else:
        status, rel = _api("POST", f"{API}/releases", tok, data={
            "tag_name": tag,
            "target_commitish": "main",
            "name": f"bili-transcriber {tag}",
            "body": notes_text or (
                f"bili-transcriber {tag} 发布产物。\n\n"
                "- `bili-transcriber-setup-*.exe` 安装版\n"
                "- `bili-transcriber-single-*.exe` 单文件便携版\n"
                "- `bili-transcriber-portable-*.zip` 便携包\n"
                "- `bili-transcriber-gpu-*.zip` GPU 加速包(可选)\n\n"
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
        size = a.stat().st_size
        # GPU 包 1.3GB+,单次 PUT 跑到一半遇上网络抖动就会被 GitHub 的
        # "Upload body timed out due to inactivity"(HTTP 408)整单打回,
        # 而 GitHub 的资产上传不支持断点续传 —— 只能整份重来。所以给每个
        # 资产最多 ATTEMPTS 次机会,重试前先清掉可能残留的半截同名资产。
        for attempt in range(1, ATTEMPTS + 1):
            if name in existing:
                st, _ = _api("DELETE", f"{API}/releases/assets/{existing[name]}", tok)
                print(f"[asset] 删除旧资产 {name} (HTTP {st})")
                existing.pop(name, None)
            # 文件流式上传:GPU 包可达 2GB+,read_bytes() 会整份驻留内存,低配机易 OOM
            with a.open("rb") as fh:
                st, body = _api(
                    "POST", f"{UPLOAD}/{release_id}/assets?name={urllib.parse.quote(name)}", tok,
                    data=fh,
                    headers={"Content-Type": "application/octet-stream",
                             "Content-Length": str(size)},
                )
            if st in (201, 200):
                print(f"[asset] OK  {name} ({size/1e6:.1f} MB, 第 {attempt} 次)")
                break
            print(f"[asset] 第 {attempt}/{ATTEMPTS} 次失败 {name} "
                  f"({size/1e6:.1f} MB, HTTP {st}): {body}")
            if attempt == ATTEMPTS:
                print("[release] 该资产多次重试仍失败,中止")
                sys.exit(1)
            wait = 10 * attempt
            print(f"[asset] {wait}s 后重试…")
            time.sleep(wait)
    print("[release] 全部资产上传完成")


if __name__ == "__main__":
    main()
