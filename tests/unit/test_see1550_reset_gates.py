"""SEE-1550 §SPEC-020 ②——R-C 门套件 J4 全向量灵敏度测试面。

J4 判据：任一污染向量 → 对应门必须 fire（门不 fire = 门无效）。V1 的
skip_reload 本轮显式实现（修复 PoC 驱动缺陷），负向向量可真实构造。
正对照（干净 reload）必须零 fire——门不许误报。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation.reset_gates import (
    GATE_CLOSURE_FRESHNESS,
    GATE_CONNECTION_FINGERPRINT,
    GATE_DOUBLE_INIT,
    GATE_RELOAD_ORDER,
    GATE_STATIC_HASH,
    GATE_STATIC_REPLAY,
    GATE_USER_TREE,
    ResetProtocolModel,
    all_gates,
    allows_gut,
    canonical_reload_order,
    closure_freshness_gate,
    connection_fingerprint_gate,
    double_init_gate,
    reload_order_gate,
    static_hash_gate,
    static_replay_gate,
    user_tree_gate,
)

CLOSURE = ("res://forms/preload_boot.gd", "res://forms/autoload_form.gd")
DISK_MUTANT = {
    "res://forms/preload_boot.gd": b"boot mutant",
    "res://forms/autoload_form.gd": b"autoload mutant",
}
BASELINE_CONNECTIONS = (("tick", "Vendor", "on_tick"),)
BASELINE_USER = frozenset({"user://baseline.cfg"})


def _clean_model() -> ResetProtocolModel:
    """正对照：会话从 post-boot 基线态出发，闭包全成员按硬序 reload 完成
    （cache == disk），副作用面 == 基线。"""
    m = ResetProtocolModel(closure=CLOSURE, disk=dict(DISK_MUTANT))
    m.connections = list(BASELINE_CONNECTIONS)
    m.user_files = set(BASELINE_USER)
    for res in canonical_reload_order(CLOSURE, {}):
        m.reload(res)
    m.static_values = {"res://forms/preload_boot.gd": "mutant-boot"}
    m.mutant_static_values = {"res://forms/preload_boot.gd": "mutant-boot"}
    m.init_count = 1
    return m


def _model_with_skip(skipped_res: str) -> ResetProtocolModel:
    """V1 负向向量构造（skip_reload 实现后的真实形态）：腿内硬序 reload
    除 skipped 成员外全部完成；skipped 成员的缓存对象仍是**上一腿的原码**
    （IGNORE 原位刷码没刷到它），盘上已是本腿 mutant——缓存/盘分叉即残留。"""
    m = _clean_model()
    # 回滚到腿首：清掉本腿 reload 效果，再按序重放（跳过 skipped）
    m.cache.pop(skipped_res, None)
    m.reload_ledger = [(r, rc) for r, rc in m.reload_ledger if r != skipped_res]
    original = {CLOSURE[0]: b"boot original", CLOSURE[1]: b"autoload original"}
    m.cache[skipped_res] = original[skipped_res]
    for res in canonical_reload_order(CLOSURE, {}):
        if res == skipped_res:
            m.skip_reload(res)
        else:
            m.reload(res)
    return m


class TestPositiveControl:
    def test_clean_leg_all_gates_silent(self):
        m = _clean_model()
        assert all_gates(m, BASELINE_CONNECTIONS, BASELINE_USER,
                         {r for r in CLOSURE}) == {}

    def test_clean_leg_allows_gut(self):
        m = _clean_model()
        assert allows_gut(m) is True

    def test_canonical_order_is_deterministic(self):
        assert canonical_reload_order(CLOSURE, {}) \
            == canonical_reload_order(CLOSURE, {})


class TestJ4Vectors:
    """J4 污染向量 → 对应门 fire（V1 为修复后的真实构造）。"""

    def test_v1_static_residue_via_skip_reload_fires_hash_gate(self):
        """V1 修复实证：skip_reload 显式实现（硬序腿内跳过）→ skipped
        成员缓存残留上一腿原码、盘上为 mutant → 缓存/盘分叉 → 静态哈希
        门 fire（负向控制可真实构造，补 PoC 缺口）。"""
        m = _model_with_skip("res://forms/preload_boot.gd")
        fired = static_hash_gate(m)
        assert GATE_STATIC_HASH in fired
        assert "res://forms/preload_boot.gd" in fired[GATE_STATIC_HASH]

    def test_v2_stale_expected_hash_fires(self):
        """V2：期望哈希被篡改（陈旧）→ 哈希门 fire。"""
        m = _clean_model()
        m.disk["res://forms/preload_boot.gd"] = b"tampered disk"
        fired = static_hash_gate(m)
        assert GATE_STATIC_HASH in fired

    def test_v3_bad_parse_blocks_gut_via_order_gate(self):
        """V3：闭包含坏 parse（rc=43）→ 顺序门 fire 且不进 Gut。"""
        m = _clean_model()
        m.parse_failures.add("res://forms/autoload_form.gd")
        for res in canonical_reload_order(CLOSURE, {}):
            m.reload(res)
        fired = reload_order_gate(m)
        assert GATE_RELOAD_ORDER in fired
        assert fired[GATE_RELOAD_ORDER]["failed"][
            "res://forms/autoload_form.gd"] == 43
        assert allows_gut(m) is False

    def test_v4_static_initializer_mutant_replay_positive(self):
        """静态初始化器 mutant：重置后重放值 == mutant 冷启动值 → 零 fire
        （正向对照；scrub 反向见 V5）。"""
        m = _clean_model()
        m.mutant_static_values = {"res://forms/preload_boot.gd": "mutant-x"}
        m.static_values = {"res://forms/preload_boot.gd": "mutant-x"}
        assert GATE_STATIC_REPLAY not in static_replay_gate(m)

    def test_v5_scrub_to_original_fires_replay_gate(self):
        """V5：scrub 错误重置（重放回原码参照值）→ 静态重放门 fire。"""
        m = _clean_model()
        m.mutant_static_values = {"res://forms/preload_boot.gd": "mutant-x"}
        m.static_values = {"res://forms/preload_boot.gd": "original-x"}
        fired = static_replay_gate(m)
        assert GATE_STATIC_REPLAY in fired
        assert fired[GATE_STATIC_REPLAY][
            "res://forms/preload_boot.gd"]["replayed"] == "original-x"

    def test_v6_folded_stale_closure_member_missing_fires(self):
        """V6 折叠陈旧：闭包成员在盘上消失（图过期）→ 新鲜门 fire。"""
        m = _clean_model()
        fired = closure_freshness_gate(m, {"res://forms/preload_boot.gd"})
        assert GATE_CLOSURE_FRESHNESS in fired
        assert fired[GATE_CLOSURE_FRESHNESS] == ["res://forms/autoload_form.gd"]

    def test_v7_connection_leak_fires_fingerprint_gate(self):
        """V7 连接泄漏：腿新增连接元组 → 指纹门 fire。"""
        m = _clean_model()
        m.connections.append(("leaked", "Vendor", "on_leak"))
        fired = connection_fingerprint_gate(m, BASELINE_CONNECTIONS)
        assert GATE_CONNECTION_FINGERPRINT in fired
        assert fired[GATE_CONNECTION_FINGERPRINT]["leaked"] == [
            ("leaked", "Vendor", "on_leak")]

    def test_v8_user_tree_residue_fires(self):
        """V8 user:// 残留：腿写 user:// 文件 → 树门 fire。"""
        m = _clean_model()
        m.user_files.add("user://leg1-save.cfg")
        fired = user_tree_gate(m, BASELINE_USER)
        assert GATE_USER_TREE in fired
        assert fired[GATE_USER_TREE]["residue"] == ["user://leg1-save.cfg"]

    def test_v9_double_init_fires(self):
        """V9 双 init：init 计数 2 → 双 init 门 fire。"""
        m = _clean_model()
        m.init_count = 2
        fired = double_init_gate(m)
        assert GATE_DOUBLE_INIT in fired
        assert fired[GATE_DOUBLE_INIT]["init_count"] == 2

    def test_v10_reload_hang_is_session_fatal(self):
        """V10 挂死：reload 挂起 → 顺序门 fire（会话致命 → respawn）。"""
        m = _clean_model()
        m.reload_hang = True
        fired = reload_order_gate(m)
        assert GATE_RELOAD_ORDER in fired
        assert fired[GATE_RELOAD_ORDER].get("hang") is True
        assert allows_gut(m) is False

    def test_residue_leg_full_batch_invalidation_surface(self):
        """会话致命聚合：污染腿 → all_gates 非空 = 整批作废信号面。"""
        m = _model_with_skip("res://forms/preload_boot.gd")  # V1 残留
        m.connections.append(("leaked", "V", "m"))    # V7 叠加
        fired = all_gates(m, BASELINE_CONNECTIONS, BASELINE_USER,
                          {r for r in CLOSURE})
        assert GATE_STATIC_HASH in fired
        assert GATE_CONNECTION_FINGERPRINT in fired
