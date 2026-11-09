"""SEE-1550 四级注入可达性 beacon 宿主（终案 §3.2）。

Godot 宿主在 GUT 前依次核验：
1. **磁盘读回**：影子内目标文件 sha256 == mutant sha256（不符 → run_error）；
2. **load 编译断言**：``load(target_res)`` 非 null 且可实例化——null/不可
   实例化（含 parse error）→ ``invalid_mutant``；
3. **autoload 同源断言**：目标在 ``[autoload]`` 表时，单例实例的脚本与按
   路径加载的资源**同源**（资源缓存同一对象）——不符 → run_error；
4. **GUT 运行证据**：Python 侧分类强制——tail 无 GUT 运行流不得
   killed/survived（gutrun._GUT_RUN_EVIDENCE）。

失败码契约：exit 3 = invalid_mutant、exit 4 = run_error，均带
``GQT_BEACON_FAIL`` stderr 标记；标记缺席 = 非 beacon 失败（走既有分类）。

实现硬约束（终案原文钉死）：
- main-loop 等待段保留——缺失时 rc=124 挂起（Archi 探针实证）；
- beacon ``load()`` 触发的 static init 只执行一次：L2 唯一一次 load，
  L3 复用同一资源对象做同源比较，不二次 load（与旧机制 impl 预编译同
  副作用档，差分语义中性）。
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

BEACON_FAIL_MARKER = "GQT_BEACON_FAIL"
ENV_MUTATION_CFG = "GQT_MUTATION_CFG"

# beacon 失败码 → verdict 档（Python 侧分类依据）
BEACON_INVALID_RC = 3
BEACON_RUN_ERROR_RC = 4

_BEACON_SCRIPT_NAME = ".gqt_beacon_host_{pid}.gd"

# 宿主脚本：beacon 四级核验 → 完全复用 GUT gut_cmdln 启动序列（版本转换 +
# main loop 等待 + gut_cli.main）——除 beacon 外与官方入口行为逐行同构。
BEACON_GD_TEMPLATE = """\
extends SceneTree

func _init() -> void:
\tvar cfg_path := OS.get_environment("GQT_MUTATION_CFG")
\tif cfg_path == "":
\t\tprinterr("GQT_BEACON_FAIL: GQT_MUTATION_CFG env missing")
\t\tquit(4)
\t\treturn
\tvar f := FileAccess.open(cfg_path, FileAccess.READ)
\tif f == null:
\t\tprinterr("GQT_BEACON_FAIL: cannot open cfg " + cfg_path)
\t\tquit(4)
\t\treturn
\tvar cfg = JSON.parse_string(f.get_as_text())
\tif typeof(cfg) != TYPE_DICTIONARY:
\t\tprinterr("GQT_BEACON_FAIL: bad cfg json")
\t\tquit(4)
\t\treturn
\tvar target_res := String(cfg["target_res"])
\tvar expect_hex := String(cfg["mutant_sha256"])
\t# main-loop 等待段保留（autoload 别名与 class_name 全局类在引擎完成
\t# 启动装配后才可解析；缺失本段时 rc=124 挂起——Archi 探针实证）。
\tvar max_iter := 20
\tvar iter := 0
\tvar Loader = load("res://addons/gut/gut_loader.gd")
\twhile(Engine.get_main_loop() == null and iter < max_iter):
\t\tawait create_timer(.01).timeout
\t\titer += 1
\tif(Engine.get_main_loop() == null):
\t\tpush_error('Main loop did not start in time.')
\t\tquit(0)
\t\treturn
\t# L1 磁盘读回：影子内目标文件哈希必须 == mutant 哈希（不符 = 注入
\t# 通道故障，跑的可能是原代码 → run_error，绝不判 killed/survived）。
\tvar bytes := FileAccess.get_file_as_bytes(target_res)
\tif bytes.is_empty():
\t\tprinterr("GQT_BEACON_FAIL: target unreadable on disk: " + target_res)
\t\tquit(4)
\t\treturn
\tvar ctx := HashingContext.new()
\tctx.start(HashingContext.HASH_SHA256)
\tctx.update(bytes)
\tif ctx.finish().hex_encode() != expect_hex:
\t\tprinterr("GQT_BEACON_FAIL: disk hash mismatch for " + target_res)
\t\tquit(4)
\t\treturn
\t# L2 load 编译断言：null / 不可实例化（含 parse error）→ invalid_mutant。
\t# 全项目上下文正常编译（mutant 就位于自身路径——class_name 全局类注册
\t# 与真实项目完全同构，无旧机制 impl 剥名特判）。static init 只在此触发
\t# 一次；L3 复用本资源对象，不二次 load。
\tvar script = load(target_res)
\tif script == null or not script.can_instantiate():
\t\tprinterr("GQT_BEACON_FAIL: mutant failed to compile/load: " + target_res)
\t\tquit(3)
\t\treturn
\t# L3 autoload 同源断言：目标在 [autoload] 表时，单例实例脚本必须与按
\t# 路径加载的资源同源（引擎资源缓存同一 Script 对象）——引擎启动期读到
\t# 的若不是本 mutant，判据全部失真 → run_error。
\tvar auto_name := String(cfg.get("autoload", ""))
\tif auto_name != "":
\t\tvar inst := get_root().get_node_or_null(NodePath(auto_name))
\t\tif inst == null or inst.get_script() == null or inst.get_script() != script:
\t\t\tprinterr("GQT_BEACON_FAIL: autoload instance not mutant-sourced: "
\t\t\t\t\t+ auto_name)
\t\t\tquit(4)
\t\t\treturn
\tvar cli : Node = load('res://addons/gut/cli/gut_cli.gd').new()
\tget_root().add_child(cli)
\tcli.main()
"""


def beacon_script_name() -> str:
    # per-PID 文件名：同 warm 槽多 worker 并发复用时互不覆写（旧机制同款
    # 隔离理由：先结束的 worker unlink 会拆掉其余 worker 的宿主）
    return _BEACON_SCRIPT_NAME.format(pid=os.getpid())


def seed_beacon_script(slot_root: str) -> Path:
    """写入（覆盖式）beacon 宿主 .gd 到影子根；返回 fs 路径。"""
    path = Path(slot_root) / beacon_script_name()
    path.write_text(BEACON_GD_TEMPLATE, encoding="utf-8")
    return path


def beacon_script_res(slot_root: str) -> str:
    return "res://" + beacon_script_name()


def write_beacon_cfg(slot_root: str, target_res: str, mutant_sha256: str,
                     autoload_name: str) -> Path:
    """单 mutant beacon cfg（JSON），落影子根；调用方 finally 删除。"""
    path = Path(slot_root) / f".gqt_beacon_cfg_{uuid.uuid4().hex[:12]}.json"
    payload = {"target_res": target_res, "mutant_sha256": mutant_sha256,
               "autoload": autoload_name}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def classify_beacon_failure(rc: int, tail: str) -> str | None:
    """beacon 失败 → verdict 档；标记缺席 → None（非 beacon 失败）。

    rc==3 → invalid_mutant（load-null/parse）；其余带标记非零 → run_error
    （哈希不符/实例不同源/基础设施）。
    """
    if BEACON_FAIL_MARKER not in tail:
        return None
    if rc == BEACON_INVALID_RC:
        return "invalid_mutant"
    return "run_error"


# ---------------------------------------------------------------------------
# project.godot [autoload] 接线解析
# ---------------------------------------------------------------------------

_AUTOLOAD_LINE = re.compile(r'^(\w+)\s*=\s*"(\*?)(res://[^"]+)"')


def autoload_map(project_root: str) -> dict[str, str]:
    """{res:// 脚本路径 → autoload 单例名}（project.godot [autoload] 段）。"""
    pg = Path(project_root) / "project.godot"
    out: dict[str, str] = {}
    try:
        text = pg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    in_section = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            in_section = line == "[autoload]"
            continue
        if not in_section or "=" not in line:
            continue
        m = _AUTOLOAD_LINE.match(line)
        if m:
            out[m.group(3)] = m.group(1)
    return out


def autoload_name_for(project_root: str, target_res: str) -> str:
    """目标脚本的单例名；不在表内返回空串（L3 跳过）。"""
    return autoload_map(project_root).get(target_res.strip(), "")
