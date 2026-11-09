Feature: SEE-1546 WP1a — 真实 KOL 树基线重锚（需要 KOL 检出）
  这三个场景消费真实 KOL worktree（KOL_ROOT / SEE1288_REPO / 并排布局），
  缺树时整模块 collection 级 skip（跳过非红，见 test_see1288_fork_cli 先例）。

  Scenario: 七个 var def 文件全量回收并重锚基线（真实 KOL 树）
    Given the real KOL worktree with systems scenes and autoload trees
    When the complexity gate runs over the three trees
    Then exactly 1561 functions are reported with zero unparseable files
    And the seven var def files contribute exactly 170 recovered functions
    And mini parity over the 170 functions shows no mismatch beyond the disclosed lambda gaps

  Scenario: 七个 var def 文件逐文件通过复杂度门（真实 KOL 树）
    Given the real KOL worktree with the seven var def files present
    When the complexity gate runs on each of the seven files alone
    Then every single-file gate verdict is ok=true
    And the per-file function counts are exactly 14 2 36 30 7 45 36 totalling 170

  Scenario: 全量五个预存 violations 的数量与身份钉住不变（真实 KOL 树）
    Given the real KOL worktree with systems scenes and autoload trees
    When the complexity gate runs over the three trees
    Then exactly 5 violations remain with their exact file function and complexity identity
