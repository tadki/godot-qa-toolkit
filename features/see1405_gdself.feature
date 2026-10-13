Feature: SEE-1405 §SPEC-003 — gdself 正向索引 glob 收敛
  Scenario: 四源命中收敛到单文件 glob
    Given a toy project with class autoload scene literal and dynamic consumers
    When the indexed glob of the factory target is derived
    Then the glob converges to the single direct test file

  Scenario: 场景绑定闭包命中（S3 链，反查假 0 命中）
    Given a toy project with a scene-bound script consumed only via its scene
    When the indexed glob of the script target is derived
    Then the glob converges to the scene consuming test file

  Scenario: hub 扇出截断保守方向（终端 test 命中保留）
    Given a toy project with a hub class consumed by many production files
    When the indexed consumers of the hub target are queried with fanout 20
    Then the terminal test consumer is present and hub truncation is disclosed

  Scenario: 零消费者诚实分类 no_tests
    Given a toy project with an unreferenced script and no uid literals
    When the indexed consumers of the orphan target are queried
    Then no consumer is returned and the reason is no_tests
