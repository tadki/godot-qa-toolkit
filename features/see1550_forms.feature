Feature: SEE-1550 M0 — 真实 Godot 加载形态矩阵（§SPEC-002）
  磁盘影子注入使引擎在解析/实例化/初始化前读到的就是 mutant：
  全部加载形态可达（killed），orphan 负对照 survived 且披露疑似孤儿。
  真实引擎跑（Godot + GUT addon；CI 无引擎环境模块级 skip）。

  Scenario: 运行时 load 形态 mutant 被杀死
    Given a real 8-form fixture project with the GUT addon
    When the default mutation command runs on "res://forms/runtime_load.gd"
    Then every mutant is killed with zero survivors

  Scenario: 启动期与晚期 preload 形态 mutant 被杀死
    Given a real 8-form fixture project with the GUT addon
    When the default mutation command runs on "res://forms/preload_boot.gd"
    Then every mutant is killed with zero survivors
    When the default mutation command runs on "res://forms/preload_late.gd"
    Then every mutant is killed with zero survivors

  Scenario: autoload 单例形态 mutant 被杀死
    Given a real 8-form fixture project with the GUT addon
    When the default mutation command runs on "res://forms/autoload_form.gd"
    Then every mutant is killed with zero survivors

  Scenario: class_name 全局类形态 mutant 被杀死
    Given a real 8-form fixture project with the GUT addon
    When the default mutation command runs on "res://forms/class_name_form.gd"
    Then every mutant is killed with zero survivors

  Scenario: 内部类形态 mutant 被杀死
    Given a real 8-form fixture project with the GUT addon
    When the default mutation command runs on "res://forms/inner_class_form.gd"
    Then every mutant is killed with zero survivors

  Scenario: PackedScene 场景脚本形态 mutant 被杀死
    Given a real 8-form fixture project with the GUT addon
    When the default mutation command runs on "res://forms/scene_script.gd"
    Then every mutant is killed with zero survivors

  Scenario: 启动组合根形态 mutant 被杀死
    Given a real 8-form fixture project with the GUT addon
    When the default mutation command runs on "res://forms/composition_root.gd"
    Then every mutant is killed with zero survivors

  Scenario: orphan 负对照 survived 且披露疑似孤儿
    Given a real 8-form fixture project with the GUT addon
    When the default mutation command runs on "res://forms/orphan_form.gd"
    Then mutants survive with the suspected-orphan disclosure

  Scenario: vendored addon 显式指定目标 mutant 被杀死（§SPEC-017）
    Given a real 8-form fixture project with the GUT addon
    When the default mutation command runs on "res://addons/gqt_forms_vendor/vendor_calc.gd"
    Then every mutant is killed with zero survivors
