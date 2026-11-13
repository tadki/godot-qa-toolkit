"""E1 prewarm 新鲜度证明（SEE-1571 §SPEC-507 定案设计 + 质量门修订，
经 §SPEC-508 质量门全闭后按 §SPEC-509 转正）。

「导入成功后签发新鲜度证明：输入与导入产物双内容摘要逐位一致才允许
跳过 `godot --headless --import`」——任何说不清的状态一律不发证、宁可
多导入一次（fail-closed）。

关键实现口径（设计条目的落地形态）：
- 热路径零子进程：引擎身份以「启动器 + 实际目标二进制当前内容 SHA-256」
  验证；版本串在签证期解析（--version rc 门）并写入证书，热路径由内容
  哈希蕴含版本一致性（同内容 ⟹ 同版本串）。
- 单遍共享目录遍历（os.scandir 融合）同时产出 input_digest 与 output_seal；
  流式分块哈希；entries = 排序后 (relpath, kind, sha256)，长度前缀编码后
  联合 sha256——路径集与内容集均计入，空目录不计。
- fail-closed：symlink / 非常规文件 / 逃逸路径 / 畸形 .import / IO 错误
  一律 TreeAmbiguity → 本轮不得签发、不得跳过。
- 输入排除仅三类：.git/、.qa-cache/、.godot/（另计 output seal）；
  JUnit 精确报告按当前 .gutconfig.json 的 junit_xml_file 精确解析，解析
  不确定则不排除；.uid / *.import / 未知扩展名 / .gdignore 全部纳入。
- provider 登记表 = 观测到的 Godot 4 内建离线 importer
  {texture, font_data_dynamic, font_data_bmfont, csv_translation}；
  项目 .import 出现表外 importer、或 .gd 静态扫描命中 EditorImportPlugin /
  add_import_plugin → 不自包含，永不签发持久证（始终导入）。
- keep sidecar（`importer="keep"`，Owner 裁决 1）：非导入配置输入——
  完整字节计入 input_digest、不入 provider 闭包、零 remap 声明合法；
  非 keep sidecar 零产物声明仍 fail-closed。
- baseline 执行根双绑定：复制后、任何 baseline 启动前，在执行根重算
  input+output 双摘要并与证书比较；只比 output 不够。
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCHEMA = "gqt-prewarm-proof/1"
PROOF_DIRNAME = ".qa-cache/gqt-prewarm"
PROOF_NAME = "proof.json"
LOCK_NAME = "prewarm.lock"
# keep 语义修订推进 provider/sidecar 语义版本——旧版证（实验轮签发）
# 在新分类下不视为已验证，自动失效重签证。
REGISTRY_VERSION = "see1571-e1/1"
BUILTIN_OFFLINE_PROVIDERS = frozenset({
    "texture", "font_data_dynamic", "font_data_bmfont", "csv_translation",
})
ENV_TOGGLE = "GQT_PREWARM_SENTINEL"
_IMPORT_TIMEOUT_S = 300
_CHUNK = 65536
_INPUT_EXCLUDED_SUBTREES = (".git", ".qa-cache", ".godot")
_OUTPUT_SUBTREE = ".godot"
_GUT_CONFIG = ".gutconfig.json"
_JUNIT_KEY = "junit_xml_file"

_TREE_AMBIGUITY = "tree-ambiguity"


class TreeAmbiguity(Exception):
    """扫描/解析歧义（fail-closed：本轮不得签发、不得跳过）。"""


def _env_disabled() -> bool:
    return os.environ.get(ENV_TOGGLE, "").strip().lower() in ("0", "false", "no")


def _proof_paths(project_root: str) -> tuple[Path, Path]:
    base = Path(project_root) / PROOF_DIRNAME
    return base, base / PROOF_NAME


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _joint_digest(lines: list[str]) -> str:
    """长度前缀行 → 联合摘要（禁止裸拼接边界碰撞）。"""
    payload = "\n".join(lines).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _entry_line(rel: str, kind: str, sha: str) -> str:
    rb = rel.encode("utf-8")
    return f"{len(rb)}:{rel}|{kind}|{len(sha)}:{sha}"


def _inside_root(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _walk_files(base: Path) -> "dict[str, Path]":
    """确定性递归枚举常规文件（fail-closed：symlink/非常规/逃逸即歧义）。"""
    out: dict[str, Path] = {}
    stack = [base]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name)
        except OSError as e:
            raise TreeAmbiguity(f"scandir failed: {current}: {e}") from e
        for entry in entries:
            path = Path(entry.path)
            if entry.is_symlink():
                raise TreeAmbiguity(f"symlink not supported: {path}")
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    raise TreeAmbiguity(f"non-regular file: {path}")
            except OSError as e:
                raise TreeAmbiguity(f"stat failed: {path}: {e}") from e
            rel = path.relative_to(base).as_posix()
            out[rel] = path
    return out


def _resolve_res(root: Path, res: str, src: Path) -> Path:
    s = res.strip().strip('"')
    if not s.startswith("res://"):
        raise TreeAmbiguity(f"non-res:// remap path in {src}: {res}")
    target = root / s[len("res://"):]
    if not _inside_root(root, target):
        raise TreeAmbiguity(f"remap path escapes project: {src}: {res}")
    return target


_REMAP_PATH_RE = re.compile(r'^(path[a-zA-Z0-9_.]*)="(.*)"\s*$')
_DEST_FILES_RE = re.compile(r'^dest_files=\[(.*)\]\s*$')
_FILES_RE = re.compile(r'^files=\[(.*)\]\s*$')
_DEST_ITEM_RE = re.compile(r'"([^"]+)"')
_IMPORTER_RE = re.compile(r'^importer="([^"]*)"', re.MULTILINE)
KEEP_IMPORTER = "keep"


def _importer_of(import_file: Path) -> str | None:
    """sidecar 的 importer 值；无 importer 行 = 畸形（歧义）。"""
    try:
        text = import_file.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as e:
        raise TreeAmbiguity(f"unreadable .import {import_file}: {e}") from e
    m = _IMPORTER_RE.search(text)
    if m is None:
        raise TreeAmbiguity(f".import without importer field: {import_file}")
    return m.group(1)


def _import_remap_outputs(root: Path, import_file: Path) -> list[Path]:
    """单个 .import 声明的全部输出产物路径（[remap] path*/dest_files 与
    [deps] files/dest_files——csv_translation 把产物声明在 [deps]）。

    K7 收敛（SEE-1571 质量门）：零声明输出**仅**对 `importer=="keep"` 豁免——keep
    sidecar 由调用方（_scan_state）在进入本函数前跳过；到达本函数的
    非 keep sidecar 若解析出零声明 = 畸形/未声明产物，必须 TreeAmbiguity
    （fail-closed），不得把 keep 豁免扩大到全部 sidecar。声明存在且产物
    在位 → 正常校验（D1）；声明存在产物缺失 → ambiguity（D2）。
    """
    section = None
    outs: list[Path] = []
    seen: set[str] = set()
    try:
        text = import_file.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as e:
        raise TreeAmbiguity(f"unreadable .import {import_file}: {e}") from e
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section not in ("remap", "deps") or not line:
            continue
        m = _REMAP_PATH_RE.match(line)
        if m:
            outs.append(_resolve_res(root, m.group(2), import_file))
            continue
        m = _DEST_FILES_RE.match(line) or _FILES_RE.match(line)
        if m:
            for item in _DEST_ITEM_RE.findall(m.group(1)):
                if item not in seen:
                    seen.add(item)
                    outs.append(_resolve_res(root, item, import_file))
    if not outs:
        raise TreeAmbiguity(f".import without remap outputs: {import_file}")
    return outs


def _junit_report_paths(root: Path, files: dict[str, Path]) -> set[str]:
    """当前生效 GUT 配置明确产生的 JUnit 精确报告（不确定则不排除）。"""
    config = files.get(_GUT_CONFIG)
    if config is None:
        return set()
    try:
        raw = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()  # 配置不可解析 → 不排除（保守多纳入）
    value = raw.get(_JUNIT_KEY) if isinstance(raw, dict) else None
    if not isinstance(value, str) or not value.strip():
        return set()
    s = value.strip()
    if s.startswith("res://"):
        rel = s[len("res://"):]
        target = (root / rel).resolve()
        if not _inside_root(root, target):
            return set()
        return {target.relative_to(root.resolve()).as_posix()}
    return set()  # 非 res:// 形式无法证明 → 不排除


def _scan_state(project_root: str) -> dict:
    """单遍共享遍历 → input/output 双摘要 + 产物与 remap 校验。

    输入 = 项目内除 {.git, .qa-cache, .godot} 与 JUnit 精确报告外的全部
    常规文件；输出 = .godot 整树 + 必要产物 + 全部 remap 输出可读。
    """
    root = Path(project_root).resolve()
    files = _walk_files(root)
    junit = _junit_report_paths(root, files)
    input_lines: list[str] = []
    import_files: list[Path] = []
    output_lines: list[str] = []
    for rel in sorted(files):
        path = files[rel]
        top = rel.split("/", 1)[0]
        sha = _sha256_file(path)
        if top == _OUTPUT_SUBTREE:
            output_lines.append(_entry_line(rel, "f", sha))
            continue
        if top in _INPUT_EXCLUDED_SUBTREES or rel in junit:
            continue
        input_lines.append(_entry_line(rel, "f", sha))
        if rel.endswith(".import"):
            import_files.append(path)
    for f in import_files:
        # keep sidecar（Owner 裁决 1）= 非导入配置输入——计入
        # input_digest（上方已纳入）、不进 provider 闭包、无 remap 校验。
        if _importer_of(f) == KEEP_IMPORTER:
            continue
        for out_path in _import_remap_outputs(root, f):
            if not out_path.is_file():
                raise TreeAmbiguity(f"remap output missing: {out_path}")
    required = [
        root / _OUTPUT_SUBTREE / "uid_cache.bin",
        root / _OUTPUT_SUBTREE / "global_script_class_cache.cfg",
        root / _OUTPUT_SUBTREE / "imported",
    ]
    for req in required:
        if not req.exists():
            raise TreeAmbiguity(f"required import artifact missing: {req}")
    return {
        "input_digest": _joint_digest(input_lines),
        "input_files": len(input_lines),
        "output_digest": _joint_digest(output_lines),
        "output_files": len(output_lines),
    }


def _godot_targets(launcher: Path) -> list[Path]:
    """wrapper 脚本引用的可执行目标（绝对路径 + 脚本目录相对路径）。

    二进制本体（含 NUL）直接返回空；文本脚本逐 token 提取候选路径，
    相对路径按脚本所在目录解析（G12 多级链 outer→inner→fin 的关键：
    inner 以相对名引用 fin 时仍可达）。
    """
    targets: list[Path] = []
    try:
        with open(launcher, "rb") as f:
            head = f.read(8192)
        if b"\x00" in head:
            return targets  # 二进制本体，无 wrapper 目标
        text = launcher.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return targets
    base = launcher.parent
    lines = text.splitlines(keepends=True)
    # shebang 解释器不是引擎链目标——首行 #! 排除（质量门冒烟实证：混入
    # /bin/sh 会截断 outer→inner→fin 链，fin 换 /bin/true 不可检）。
    if lines and lines[0].startswith("#!"):
        lines = lines[1:]
    body = "".join(lines)
    for token in re.findall(r"[A-Za-z0-9_./+-]+", body):
        candidates = [Path(token)]
        if not token.startswith("/"):
            candidates.append(base / token)
        for candidate in candidates:
            if candidate.is_file() and os.access(candidate, os.X_OK) \
                    and candidate != launcher:
                targets.append(candidate)
    return targets


def _resolve_chain(launcher: Path) -> list[Path]:
    """启动器 wrapper 链解析至最终二进制（G12：绑定全链内容）。

    实现口径（质量门修订）：终端实体必须是二进制（含 NUL 实体）；链路缺失、
    循环、深度超限、无法判定终端实体 → TreeAmbiguity → NO_CERT，不得
    降级为只绑定直接目标。深度 ≤4；目标已见于链中 = 循环。
    """

    def _is_binary(path: Path) -> bool:
        try:
            with open(path, "rb") as f:
                return b"\x00" in f.read(8192)
        except OSError:
            return False

    chain = [launcher]
    seen = {launcher.resolve()}
    current = launcher
    while not _is_binary(current):
        nxt = None
        for target in _godot_targets(current):
            if target.resolve() in seen:
                raise TreeAmbiguity(f"wrapper chain cycle at {target}")
            nxt = target
            break
        if nxt is None:
            raise TreeAmbiguity(
                f"wrapper chain cannot reach terminal binary from {current}")
        if len(chain) > 4:
            raise TreeAmbiguity("wrapper chain depth exceeds 4")
        chain.append(nxt)
        seen.add(nxt.resolve())
        current = nxt
    return chain


def _engine_identity(check_version: bool) -> dict:
    """引擎身份：启动器 + 实际目标二进制当前内容哈希（+签证期版本串）。"""
    from .inject import detect_binary_kind, godot_command
    command = godot_command()
    launcher = Path(command).resolve() if "/" in command else None
    identity: dict = {"command": command, "kind": detect_binary_kind()}
    if launcher is None or not launcher.is_file():
        raise TreeAmbiguity(f"godot launcher unresolvable: {command}")
    identity["launcher_path"] = str(launcher)
    identity["launcher_sha256"] = _sha256_file(launcher)
    # G12：wrapper 链解析至最终二进制，全链内容入身份（任一环节
    # 被替换即失配——B2 存活例 fin 交换在链哈希覆盖下必然失配）。
    chain = _resolve_chain(launcher)
    identity["chain_sha256"] = [_sha256_file(link) for link in chain[1:]]
    if check_version:
        try:
            r = subprocess.run([command, "--version"], capture_output=True,
                               text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise TreeAmbiguity(f"godot --version failed: {e}") from e
        if r.returncode != 0 or not r.stdout.strip():
            raise TreeAmbiguity(
                f"godot --version rc={r.returncode} unresolvable")
        identity["version"] = r.stdout.strip().splitlines()[0]
    return identity


def _identity_hot_matches(cert_identity: dict) -> tuple[bool, str]:
    """热路径身份核对：内容哈希（版本串由内容一致性蕴含）。"""
    try:
        current = _engine_identity(check_version=False)
    except TreeAmbiguity as e:
        return False, f"engine identity unresolvable: {e}"
    for key in ("launcher_path", "launcher_sha256", "chain_sha256", "kind"):
        if current.get(key) != cert_identity.get(key):
            return False, f"engine identity drifted: {key}"
    return True, "match"


def _enumerate_providers(root: Path, files: dict[str, Path]) -> tuple[set[str], str | None]:
    """实际 importer 枚举 + 自包含判定（§2.2 支持边界）。"""
    providers: set[str] = set()
    for rel, path in sorted(files.items()):
        if rel.endswith(".import"):
            importer = _importer_of(path)
            # keep（Owner 裁决 1）= 非导入配置输入，不进 provider
            # 闭包（不计入枚举集合）。
            if importer == KEEP_IMPORTER:
                continue
            providers.add(importer)
    unknown = providers - BUILTIN_OFFLINE_PROVIDERS
    if unknown:
        return set(), f"providers outside offline registry: {sorted(unknown)}"
    for rel, path in sorted(files.items()):
        if rel.endswith(".gd"):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                raise TreeAmbiguity(f"unreadable .gd {rel}: {e}") from e
            if "EditorImportPlugin" in text or "add_import_plugin" in text:
                return set(), f"project registers import plugin: {rel}"
    return providers, None


def _load_proof(project_root: str) -> tuple[dict | None, str | None]:
    path = _proof_paths(project_root)[1]
    if not path.is_file():
        return None, "no proof file"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, f"proof unreadable: {e}"
    if not isinstance(raw, dict):
        return None, "proof root not an object"
    required = ("schema", "project_realpath", "identity", "providers_version",
                "input_digest", "output_seal", "proof_id", "last_import_rc")
    missing = [k for k in required if k not in raw]
    if missing:
        return None, f"proof fields missing: {missing}"
    if raw["schema"] != SCHEMA:
        return None, f"schema mismatch: {raw['schema']}"
    if raw["project_realpath"] != str(Path(project_root).resolve()):
        return None, "project realpath mismatch"
    if raw.get("last_import_rc") != 0:
        return None, "last import rc != 0"
    if raw.get("providers_version") != REGISTRY_VERSION:
        return None, "provider registry version mismatch"
    ok, reason = _identity_hot_matches(raw["identity"] or {})
    if not ok:
        return None, reason
    return raw, None


def _publish_proof(project_root: str, state: dict, identity: dict,
                   proof_id: str) -> None:
    base, path = _proof_paths(project_root)
    base.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": SCHEMA,
        "proof_id": proof_id,
        "project_realpath": str(Path(project_root).resolve()),
        "identity": identity,
        "providers_version": REGISTRY_VERSION,
        "input_digest": state["input_digest"],
        "input_files": state["input_files"],
        "output_seal": state["output_digest"],
        "output_files": state["output_files"],
        "last_import_rc": 0,
        "issued_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    tmp = base / (PROOF_NAME + f".tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _invalidate_proof(project_root: str) -> str:
    """失效先行：启动 Godot 前原子移除旧证（SIGKILL 安全）。"""
    _, path = _proof_paths(project_root)
    if path.exists():
        try:
            os.replace(path, path.with_suffix(".json.stale"))
            return "stale-renamed"
        except OSError:
            try:
                path.unlink()
                return "unlinked"
            except OSError:
                return "remove-failed"
    return "absent"


def _run_import(project_root: str) -> tuple[bool, str]:
    """同源 canonical 导入（godot_command 同源）；rc/输出/OSError 三重门。"""
    from .inject import godot_command
    command = godot_command()
    try:
        r = subprocess.run(
            [command, "--headless", "--import", "--path", project_root],
            capture_output=True, text=True, timeout=_IMPORT_TIMEOUT_S,
            cwd=project_root)
    except subprocess.TimeoutExpired:
        return False, f"import timed out after {_IMPORT_TIMEOUT_S}s"
    except OSError as e:
        return False, f"import launch failed: {e}"
    if r.returncode != 0:
        return False, f"import rc={r.returncode}"
    blob = (r.stdout or "") + "\n" + (r.stderr or "")
    error_lines = [ln for ln in blob.splitlines()
                   if ln.strip().startswith("ERROR:")]
    if error_lines:
        return False, f"import error output: {error_lines[0][:120]}"
    return True, "rc=0 clean"


class _PrewarmLock:
    """项目级 prewarm flock；不可靠锁 → 放弃跨进程跳过（fail-closed）。"""

    def __init__(self, project_root: str) -> None:
        self._path = Path(project_root) / PROOF_DIRNAME / LOCK_NAME
        self._fd: int | None = None

    def acquire(self) -> bool:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


def prewarm_with_proof(project_root: str) -> dict:
    """§3.1 签发协议全流程入口（executor.prewarm_import 的新实现）。

    返回 {decision, probe_s, proof_id, import_calls, reason?}；
    任何意外异常 → canonical 导入兜底（LOW-5 契约），绝不 raise。
    """
    t0 = time.perf_counter()
    out: dict = {"decision": "imported_uncertified", "import_calls": 0,
                 "probe_s": 0.0}
    if not os.path.isfile(os.path.join(project_root, "project.godot")):
        out["decision"] = "skipped_non_project"
        return out
    try:
        if _env_disabled():
            ok, reason = _run_import(project_root)
            out.update(import_calls=1, decision="disabled",
                       reason=reason if not ok else "env escape hatch")
            print(f"prewarm: import (escape hatch {ENV_TOGGLE} set)",
                  file=sys.stderr)
            return out
        lock = _PrewarmLock(project_root)
        if not lock.acquire():
            ok, reason = _run_import(project_root)
            out.update(import_calls=1, decision="imported_no_lock",
                       reason="flock unavailable")
            print("prewarm: import (no reliable lock — cross-process "
                  "fresh-skip disabled)", file=sys.stderr)
            return out
        try:
            return _locked_flow(project_root, t0, out)
        finally:
            lock.release()
    except Exception as e:  # noqa: BLE001 — LOW-5：预热失败不打破统一 JSON
        ok, reason = _run_import(project_root)
        out.update(import_calls=out.get("import_calls", 0) + 1,
                   decision="imported_uncertified",
                   reason=f"prewarm flow error: {e}; import fallback rc_ok={ok}")
        print(f"prewarm: import (flow error fallback: {e})", file=sys.stderr)
        return out


def _locked_flow(project_root: str, t0: float, out: dict) -> dict:
    _, proof_path = _proof_paths(project_root)
    cert, load_reason = _load_proof(project_root)
    state = None
    if cert is not None:
        try:
            state = _scan_state(project_root)
        except TreeAmbiguity as e:
            print(f"prewarm: re-import (tree ambiguity: {e})", file=sys.stderr)
    if cert is not None and state is not None:
        if state["input_digest"] == cert["input_digest"] \
                and state["output_digest"] == cert["output_seal"]:
            out.update(decision="fresh_skip", import_calls=0,
                       proof_id=cert["proof_id"],
                       probe_s=round(time.perf_counter() - t0, 3))
            print(f"prewarm: fresh_skip proof={cert['proof_id']} "
                  f"input={cert['input_digest'][:8]} "
                  f"output={cert['output_seal'][:8]} "
                  f"probe_s={out['probe_s']}", file=sys.stderr)
            return out
        reason = load_reason or "digest mismatch"
        print(f"prewarm: re-import (stale: {reason})", file=sys.stderr)
    else:
        print(f"prewarm: re-import (no usable proof: {load_reason})",
              file=sys.stderr)
    # 失效先行 → 双导入定点 → 原子发布
    invalidate = _invalidate_proof(project_root)
    ok, reason = _run_import(project_root)
    out["import_calls"] = 1
    if not ok:
        out["reason"] = f"first import failed: {reason}"
        print(f"prewarm: no cert ({out['reason']})", file=sys.stderr)
        return out
    try:
        s1 = _scan_state(project_root)
    except TreeAmbiguity as e:
        out["reason"] = f"post-import scan ambiguous: {e}"
        print(f"prewarm: no cert ({out['reason']})", file=sys.stderr)
        return out
    ok2, reason2 = _run_import(project_root)
    out["import_calls"] = 2
    if not ok2:
        out["reason"] = f"verification import failed: {reason2}"
        print(f"prewarm: no cert ({out['reason']})", file=sys.stderr)
        return out
    try:
        s2 = _scan_state(project_root)
    except TreeAmbiguity as e:
        out["reason"] = f"post-verify scan ambiguous: {e}"
        print(f"prewarm: no cert ({out['reason']})", file=sys.stderr)
        return out
    if (s2["input_digest"], s2["output_digest"]) \
            != (s1["input_digest"], s1["output_digest"]):
        out["reason"] = "double import did not converge"
        print("prewarm: no cert (S2/O2 != S1/O1 — outputs still changing)",
              file=sys.stderr)
        return out
    try:
        identity = _engine_identity(check_version=True)
        providers, unsupported = _enumerate_providers(
            Path(project_root).resolve(),
            _walk_files(Path(project_root).resolve()))
    except TreeAmbiguity as e:
        out["reason"] = f"identity/provider calibration failed: {e}"
        print(f"prewarm: no cert ({out['reason']})", file=sys.stderr)
        return out
    if unsupported is not None:
        out["reason"] = f"project not self-contained: {unsupported}"
        print(f"prewarm: no cert ({out['reason']})", file=sys.stderr)
        return out
    proof_id = hashlib.sha256(
        f"{s2['input_digest']}|{s2['output_digest']}|"
        f"{identity.get('launcher_sha256', '')}".encode("utf-8")).hexdigest()[:16]
    _publish_proof(project_root, s2, identity, proof_id)
    out.update(decision="imported_certified", proof_id=proof_id,
               reason=f"invalidate={invalidate}",
               probe_s=round(time.perf_counter() - t0, 3))
    return out


def reissue_after_drift(project_root: str) -> dict:
    """MUST_REBUILD 通道：废弃旧证 → 重导入重签证（baseline 前调用）。"""
    _invalidate_proof(project_root)
    return prewarm_with_proof(project_root)


def guard_baseline_root(exec_root: str, project_root: str) -> tuple[bool, str]:
    """执行根双绑定（§4.3）：baseline 启动前重算 input+output 双摘要。

    返回 (ok, reason)；ok=False 时调用方必须保持 baseline 计数 0 并走
    MUST_REBUILD（reissue_after_drift + 重建槽）。无证（disabled/非自包含
    项目）时守卫不激活并如实披露——证书链未启用即无可绑定对象。
    """
    cert, load_reason = _load_proof(project_root)
    if cert is None:
        return True, f"guard inactive (no usable proof: {load_reason})"
    try:
        state = _scan_state(exec_root)
    except TreeAmbiguity as e:
        return False, f"execution root scan ambiguous: {e}"
    if state["input_digest"] != cert["input_digest"]:
        return False, "input digest mismatch at execution root"
    if state["output_digest"] != cert["output_seal"]:
        return False, "output seal mismatch at execution root"
    return True, "bound"
