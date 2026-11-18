"""SEE-1594 BTIDY（零行为拆分）：measurement shadow 装配与命中/归属/
注册表 sink 读取。自 orchestrator.py 原行段物理迁移；`_prepare_shadow` 与
`_deref_shadow_symlink` 按阶段分解为 ≤50 行函数，逻辑逐语句等价。"""

from __future__ import annotations

import os
import re
import shutil
import stat as stat_module
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

from ..mutation.godot_gate import GlobalGodotGate
from ..mutation.inject import BinaryConfigError, godot_command, spawn_godot
from .backup import MeasurementError, file_key8


def _run_one_gut(project_root: Path, gut_args: list[str],
                 timeout_s: int,
                 env: dict | None = None,
                 gate_root: Path | None = None
                 ) -> tuple[subprocess.CompletedProcess, float]:
    t0 = time.monotonic()
    gut = project_root / "addons" / "gut" / "gut_cmdln.gd"
    if not gut.is_file():
        raise MeasurementError(f"GUT runner not found: {gut}")
    argv = [godot_command(), "--headless", "--path", str(project_root),
            "-s", "res://addons/gut/gut_cmdln.gd", *gut_args, "-gexit"]
    with GlobalGodotGate(str(gate_root or project_root)):
        try:
            r = spawn_godot(argv, capture_output=True, text=True,
                            timeout=timeout_s, cwd=str(project_root),
                            env=env)
        except subprocess.TimeoutExpired as e:
            raise MeasurementError(
                f"GUT timed out after {timeout_s}s "
                f"(args: {' '.join(gut_args)})") from e
        except BinaryConfigError as e:
            # §SPEC-079 C：静态前置漏判时的拉起点兜底（双防线）——
            # 归一为 measurement 领域错误，cli 既有 MeasurementError 出口转 JSON。
            raise MeasurementError(str(e)) from e
    return r, time.monotonic() - t0


def _project_name(root: Path) -> str:
    pg = root / "project.godot"
    if pg.is_file():
        m = re.search(r'config/name\s*=\s*"([^"]+)"',
                      pg.read_text(encoding="utf-8"))
        if m:
            return m.group(1)
    return root.name


def _shard_groups(root: Path, dirs: list[str], width: int) -> list[list[str]]:
    """按测试文件数 LPT 装箱把 config 目录分到 width 组（§SPEC-052 并行
    分片臂）；weight 用 test_*.gd 文件数近似执行成本。"""
    if not dirs:
        return [[]]
    if width <= 1:
        return [list(dirs)]
    weighted = []
    for d in dirs:
        rel = d[len("res://"):] if d.startswith("res://") else d
        p = root / rel
        w = len(list(p.rglob("test_*.gd"))) if p.is_dir() else 1
        weighted.append((max(1, w), d))
    n = min(width, len(weighted))
    groups: list[list[str]] = [[] for _ in range(n)]
    loads = [0] * n
    for w, d in sorted(weighted, key=lambda x: -x[0]):
        i = loads.index(min(loads))
        groups[i].append(d)
        loads[i] += w
    return [g for g in groups if g]


def _force_rmtree(path: Path) -> None:
    """§SPEC-074：失败路径强制清理——shadow 可能含 copystat 保留的只读
    模式副本（如 0555 只读目录），需先自底向上恢复写位再删除。"""
    for dirpath, _dirnames, _filenames in os.walk(path, topdown=False):
        try:
            os.chmod(dirpath, 0o700)
        except OSError:
            pass
    shutil.rmtree(path, ignore_errors=True)


def _shadow_copy_tree(base_root: Path, shadow: Path) -> None:
    """§SPEC-056/070 FR-2：源码/resource 树 reflink/实拷贝 + `.godot`
    实拷贝隔离；junit 断开放分片自写。"""
    import subprocess as _sp

    entries = [e for e in base_root.iterdir() if e.name != ".godot"]
    for e in entries:
        _sp.run(["cp", "-a", "--reflink=auto", str(e),
                 str(shadow / e.name)], check=True)
    godot_cache = base_root / ".godot"
    if godot_cache.is_dir():
        _sp.run(["cp", "-a", str(godot_cache),
                 str(shadow / ".godot")], check=True)
    junit = shadow / "tests" / "reports" / "junit_report.xml"
    if junit.is_file():
        junit.unlink()


def _shadow_deref_links(shadow: Path, disclosure: dict) -> None:
    """§SPEC-071/072 H1：复制完成后对 shadow 做 `find -type l` 检测（原生
    速度；覆盖 `.godot` 与复制窗口内新增的任何链接），命中链接一律解引用
    为常规文件/实拷贝目录，悬空链接移除；OS 级失败统一转领域错误。"""
    import subprocess as _sp

    found = _sp.run(["find", str(shadow), "-type", "l", "-print0"],
                    check=True, capture_output=True)
    for raw in found.stdout.split(b"\0"):
        if not raw:
            continue
        p = Path(os.fsdecode(raw))
        try:
            _deref_shadow_symlink(p, disclosure)
        except MeasurementError:
            raise
        except OSError as e:
            # §SPEC-074：解引用阶段全部 OS 级失败（含只读目录内悬空
            # 链接的 unlink 拒绝）转领域错误——顶层命中与递归内层两侧
            # 统一由本包裹覆盖
            raise MeasurementError(
                f"shadow symlink handling failed (OS): {p}: {e}"
            ) from e


def _prepare_shadow(base_root: Path, run_uuid: str, idx: int
                    ) -> tuple[Path, dict]:
    """§SPEC-056：分片独立 project shadow——源码/resource 状态与基准树
    完全一致，`.godot` 引擎缓存实拷贝隔离，杜绝多 Godot 进程争用同一
    项目目录缓存。junit 断开放分片自写。

    §SPEC-070 FR-2：shadow 与 base 不得共享可写 inode——hardlink farm
    （cp -al）下运行期任何 res:// 写都会写穿 base，最终恢复只覆盖被插桩
    文件，非插桩文件可被永久污染；改为 reflink（FS 支持）否则实拷贝。

    §SPEC-071/072 H1：cp -a 会原样保留 symlink——绝对/相对链接可指向
    base inode，运行期写经链接写穿。消解**不依赖复制前的预扫快照**：
    复制完成后对 shadow 本身做 `find -type l` 检测，命中链接一律解引用；
    disclosure 计数即实际处理数。

    §SPEC-074：任何失败路径不泄漏已 mkdtemp 的 shadow 目录（整体
    try/except 后 _force_rmtree——shadow 可能含 copystat 保留的只读
    模式副本，需先自底向上恢复写位），OS 级失败（含只读目录内悬空
    链接的 unlink 拒绝）与 cp/find 失败统一转 MeasurementError——真实
    CLI 恒输出可解析 tool JSON，无裸 traceback。"""
    import subprocess as _sp

    shadow = Path(tempfile.mkdtemp(
        prefix=f"gqt-shadow-{run_uuid[:8]}-{idx}-"))
    try:
        _shadow_copy_tree(base_root, shadow)
        disclosure = {"symlinks_dereferenced": 0, "dangling_links_removed": 0}
        _shadow_deref_links(shadow, disclosure)
        return shadow, disclosure
    except MeasurementError:
        _force_rmtree(shadow)
        raise
    except (OSError, subprocess.CalledProcessError) as e:
        _force_rmtree(shadow)
        raise MeasurementError(f"shadow preparation failed: {e}") from e
    except BaseException:
        _force_rmtree(shadow)
        raise


def _deref_unreachable_link(p: Path, disclosure: dict) -> bool:
    """§SPEC-073：不可解析/不可读（悬空/循环）链接 → 移除并计入
    dangling_links_removed（确定性：移除即计数、计数即移除）。返回 True
    表示该链接已按悬空处理，调用方无需继续解引用。"""
    try:
        os.stat(p)  # 跟随链接；悬空/循环 → OSError
    except OSError:
        try:
            p.unlink(missing_ok=True)
        except OSError as e:
            # §SPEC-074：只读父目录内悬空链接无法移除 → 领域错误
            # fail-loud（顶层 find 命中与递归内层两侧统一由调用方包裹）
            raise MeasurementError(
                f"shadow dangling symlink cannot be removed: {p}: {e}"
            ) from e
        disclosure["dangling_links_removed"] += 1
        return True
    return False


def _deref_copy_target(p: Path, tmp: Path,
                       target_stat: os.stat_result) -> None:
    """读 base 目标内容落到 shadow 自有 inode（只读 base，零写穿）：
    目录链接 copytree(symlinks=True) 实拷贝（内层链接形态留给逐链接
    解引用消解——symlinks=False 会在内层悬空时整体报错并丢失已拷贝
    内容）；文件链接 → 解引用为常规文件。拷贝失败 → fail-loud
    （MeasurementError），不得静默删除有效链接或虚报计数（R28 反例
    根因：Linux rename 拒绝目录替换 symlink → NotADirectoryError 被
    宽 except 吞掉）。"""
    try:
        if stat_module.S_ISDIR(target_stat.st_mode):
            shutil.copytree(p, tmp, symlinks=True)
        else:
            shutil.copyfile(p, tmp)
    except OSError as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise MeasurementError(
            f"shadow symlink dereference failed (link kept): {p}: {e}"
        ) from e


def _deref_walk_inner(tmp: Path, disclosure: dict, depth: int) -> None:
    """对实拷贝目录的内层链接递归解引用（深度有界，超限 fail-loud；
    内层悬空/循环移除并计入 dangling——确定性披露）。"""
    for dirpath, dirnames, filenames in os.walk(tmp, topdown=True):
        # 目录链接不入 walk 递归（防环）——由下方递归解引用处理
        for name in list(dirnames):
            inner = Path(dirpath) / name
            if inner.is_symlink():
                _deref_shadow_symlink(inner, disclosure, depth + 1)
        dirnames[:] = [d for d in dirnames
                       if not (Path(dirpath) / d).is_symlink()]
        for name in list(filenames):
            inner = Path(dirpath) / name
            if inner.is_symlink():
                _deref_shadow_symlink(inner, disclosure, depth + 1)


def _deref_shadow_symlink(p: Path, disclosure: dict, depth: int = 0) -> None:
    """§SPEC-073：解引用 shadow 内单个 symlink——读 base 目标内容落到
    shadow 自有 inode（只读 base，零写穿）。

    - 真正不可解析/不可读（悬空/循环）→ 移除链接并计入
      dangling_links_removed。
    - 文件链接 → 解引用为常规文件；目录链接 → 实拷贝目录后对内层链接
      **递归**解引用（深度有界，超限 fail-loud）。
    - 内容拷贝开始后的任何失败 → fail-loud（MeasurementError）。"""
    if depth > 8:
        raise MeasurementError(
            f"shadow symlink dereference depth exceeded: {p}")
    if _deref_unreachable_link(p, disclosure):
        return
    tmp = p.with_name(p.name + ".gqt-deref")
    try:
        target_stat = os.stat(p)  # 跟随链接；悬空/循环 → OSError
    except OSError:
        # _deref_unreachable_link 刚确认可解析，此处仅防御 TOCTOU——
        # 语义与原实现一致（交给下方 copy 的 OSError 分支 fail-loud）
        target_stat = os.stat(p)
    _deref_copy_target(p, tmp, target_stat)
    if stat_module.S_ISDIR(target_stat.st_mode):
        _deref_walk_inner(tmp, disclosure, depth)
    # 用 tmp 换掉链接位置：Linux rename 拒绝 dir→非 dir（NotADirectory），
    # 必须先摘除链接再 rename（shadow 为进程私有，无并发竞争）
    try:
        p.unlink(missing_ok=True)
        os.rename(tmp, p)
    except OSError as e:
        # §SPEC-074：只读父目录内的最终替换 unlink 同样转领域错误
        raise MeasurementError(
            f"shadow symlink dereference replace failed: {p}: {e}") from e
    disclosure["symlinks_dereferenced"] += 1


def _cleanup_shadow(shadow: Path | None) -> None:
    if shadow is not None:
        shutil.rmtree(shadow, ignore_errors=True)


def _shard_user_dir(xdg_home: str, project_name: str) -> Path:
    return Path(xdg_home) / "godot" / "app_userdata" / project_name


def _read_hits_merged(root: Path, run_uuid: str, rel: str,
                      manifest: list[int],
                      xdg_homes: list[str],
                      retain_dir: Path | None = None,
                      consume: bool = True
                      ) -> tuple[set[int], int]:
    """并行分片臂的合并读回：sink 名含进程 PID（`...-<uuid>-<pid>-<key8>`
    .txt），按 run_uuid+key8 模式跨全部分片 user:// 目录取并集。
    consume=False 仅读不删（§SPEC-062C 收敛预读复用；正式读回仍消费）。"""
    key8 = file_key8(rel)
    name = f"qa-coverage-hits-{run_uuid}-*-{key8}.txt"
    proj = _project_name(root)
    ids: list[int] = []
    files: list[Path] = []
    for xdg in xdg_homes:
        d = _shard_user_dir(xdg, proj)
        if d.is_dir():
            files.extend(sorted(d.glob(name)))
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
            if retain_dir is not None:
                retain_dir.mkdir(parents=True, exist_ok=True)
                (retain_dir / f.name).write_text(text, encoding="utf-8")
            for ln in text.splitlines():
                ln = ln.strip()
                if ln.isdigit():
                    ids.append(int(ln))
        finally:
            if consume:
                f.unlink(missing_ok=True)
    valid = set(range(len(manifest)))
    hits = {manifest[pid] for pid in ids if pid in valid}
    return hits, sum(1 for pid in ids if pid in valid)


def _attr_pid(sink_file: str, run_uuid: str) -> int | None:
    """attr/registry sink 文件名 → 进程 PID（`...-<uuid>-<pid>-<key8>.txt`
    与 `...-<uuid>-<pid>.txt` 两形态）。解析失败返回 None（不猜）。"""
    stem = sink_file.rsplit(".", 1)[0]
    idx = stem.find(run_uuid)
    if idx < 0:
        return None
    tail = stem[idx + len(run_uuid):].strip("-")
    head = tail.split("-", 1)[0]
    try:
        return int(head)
    except ValueError:
        return None


def _iter_attr_sinks(root: Path, run_uuid: str, pattern: str,
                     xdg_homes: list[str],
                     consume: bool = True) -> Iterator[tuple[int, str]]:
    """§SPEC-083：按文件名序遍历各 XDG 下的 attr/registry sink，逐条给出
    (pid, 文本)；读后即消费（consume=False 仅用于诊断复盘）。

    文件名解析不出 pid 的条目不猜、跳过；read 失败时仍按 consume 口径
    删除文件（与调用方 finally 语义一致），异常向上抛给调用方。"""
    proj = _project_name(root)
    for xdg in xdg_homes:
        d = _shard_user_dir(xdg, proj)
        if not d.is_dir():
            continue
        for f in sorted(d.glob(pattern)):
            pid = _attr_pid(f.name, run_uuid)
            if pid is None:
                continue
            try:
                text = f.read_text(encoding="utf-8")
            finally:
                if consume:
                    f.unlink(missing_ok=True)
            yield pid, text


def _new_attr_rec() -> dict:
    # fn 级布局：seen/cnt 按上下文槽位，fn 记 (probe, slot) 函数级归属，
    # unattr_counts 记无主 probe 热点，phases = [bootstrap, gap, shutdown,
    # other]（方向③）
    return {"seen": set(), "cnt": {}, "fn": {}, "unattr_counts": {},
            "phases": [0, 0, 0, 0]}


def _accumulate_attr_lines(rec: dict, text: str) -> None:
    """attr sink 行累加进 rec（方向② fn 级布局）：`test <slot>` 记文件级
    关系；`cnt <slot> <n>` 按槽位累计命中事件；`fn <probe> <slot>` 记
    函数级归属；`unattr <probe> <n>` 记无主热点；`ph <4 整数>` 累计四阶段；
    格式或数值不合法的行跳过。"""
    for raw in text.splitlines():
        line = raw.strip()
        kind, _, body = line.partition(" ")
        if not body:
            continue
        try:
            if kind == "test":
                rec["seen"].add(int(body))
            elif kind == "cnt":
                slot_s, n_s = body.split()
                slot, n = int(slot_s), int(n_s)
                rec["cnt"][slot] = rec["cnt"].get(slot, 0) + n
            elif kind == "fn":
                probe_s, slot_s = body.split()
                probe, slot = int(probe_s), int(slot_s)
                slot_set = rec["fn"].setdefault(probe, set())
                slot_set.add(slot)
            elif kind == "unattr":
                probe_s, n_s = body.split()
                probe, n = int(probe_s), int(n_s)
                rec["unattr_counts"][probe] = (
                    rec["unattr_counts"].get(probe, 0) + n)
            elif kind == "ph":
                vals = [int(x) for x in body.split()]
                if len(vals) == 4:
                    for i, v in enumerate(vals):
                        rec["phases"][i] += v
        except (ValueError, IndexError):
            continue


def _read_attr_merged(root: Path, run_uuid: str, rel: str,
                      xdg_homes: list[str], consume: bool = True
                      ) -> dict[int, dict]:
    """§SPEC-083：读回 per-file 归属 sink（`qa-coverage-attr-<uuid>-<pid>-
    <key8>.txt`）。返回 {pid: {"seen", "seen_script", "counts"}}——pid 用于
    与注册表 join（每进程独立注册表）。"""
    key8 = file_key8(rel)
    name = f"qa-coverage-attr-{run_uuid}-*-{key8}.txt"
    out: dict[int, dict] = {}
    for pid, text in _iter_attr_sinks(root, run_uuid, name, xdg_homes, consume):
        _accumulate_attr_lines(out.setdefault(pid, _new_attr_rec()), text)
    return out


def _accumulate_registry_lines(tables: tuple[dict, dict], text: str) -> None:
    """registry sink 行写入 (test_table, script_table)：组合注册表行
    `slot <idx>\\t<kind>\\t<identity>`，按 kind 分流入 test/script 两表；
    格式不符行跳过。"""
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("slot "):
            continue
        parts = line[5:].split("\t")
        if len(parts) != 3:
            continue
        idx_s, kind, identity = parts
        try:
            idx = int(idx_s)
        except ValueError:
            continue
        if kind == "test":
            tables[0][idx] = identity
        elif kind == "script":
            tables[1][idx] = identity


def _read_registries(root: Path, run_uuid: str, xdg_homes: list[str],
                     consume: bool = True
                     ) -> tuple[dict[int, dict[int, str]], dict[int, dict[int, str]]]:
    """§SPEC-083：读回每进程组合注册表 `qa-cov-attr-tests-<uuid>-<pid>.txt`
    → ({pid: {slot: identity}}, {pid: {slot: identity}})（test/script 分表）。"""
    name = f"qa-cov-attr-tests-{run_uuid}-*.txt"
    tests: dict[int, dict[int, str]] = {}
    scripts: dict[int, dict[int, str]] = {}
    for pid, text in _iter_attr_sinks(root, run_uuid, name, xdg_homes, consume):
        _accumulate_registry_lines(
            (tests.setdefault(pid, {}), scripts.setdefault(pid, {})), text)
    return tests, scripts
