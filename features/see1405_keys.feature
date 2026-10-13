Feature: SEE-1405 §SPEC-001 — store key 路径成分归一
  Scenario: 同内容文件在两个检出下产出同一 resume/timing key
    Given two checkouts with an identical relative layout
    Then the resume keys of both checkouts are equal
    And the timing keys of both checkouts are equal

  Scenario: 相对路径不同则 key 不同（路径成分保持判别力）
    Given two checkouts with different relative layouts
    Then the resume keys of both checkouts differ

  Scenario: A 检出落盘的 verdict 条目搬到 B 检出 store 后可命中复用
    Given a verdict recorded at checkout A
    When the store entry is placed at checkout B
    Then checkout B resumes the recorded verdict without rerunning
