Feature: SEE-1546 M2 — 最小 exploratory shadow（CRAP-Line 诊断排序器）
  diagnostic-only：不接 CI、不设阈值、不驱动 tidy 改代码。所有场景用确定性
  fake 测量注入，真机 core/full/二跑/acceptance 探针由 §SPEC-009 独立实跑。

  Scenario: eligible 文件全函数参与测量
    Given a set of eligible GDScript sources and one non-eligible source
    When the shadow partitions the candidates and measures the eligible files
    Then every eligible file reports all of its functions as scored records
    And only the non-eligible source routes to a gap lane

  Scenario: 三类负例进入测量缺口车道
    Given lambda inner class and var def negative sources
    When the shadow profiles them without consuming a GUT round
    Then each negative lands in its own gap lane
    And none of them is counted as an eligible measured file

  Scenario: 整文件零命中保持 unknown
    Given an eligible source whose measurement fires probes with no function hit
    When the shadow assembles its records
    Then the file state is unknown
    And no record is scored or marked true zero

  Scenario: CRAP-Line 公式数值边界
    Given the crap-line/1 formula
    When cc is 6 with coverage 0 0.5 and 1
    Then the scores are exactly 42 10.5 and 6

  Scenario: 每条记录携带强制披露字段
    Given a scored record from the shadow
    When its disclosure fields are inspected
    Then coverage semantics is statement start line
    And path coverage and assertion effectiveness are explicitly not measured
    And the evidence level is exploratory

  Scenario: vacuous 文件不出分且 coverage 为 null
    Given a source with no executable statements
    When the shadow classifies and records it
    Then the state is vacuous
    And coverage and score are null

  Scenario: 函数身份与 probe 映射一一归属
    Given a GDScript source with several functions
    When the identity layer builds the probe manifest
    Then every probe maps to exactly one function
    And the census T equals the instrumentation set I

  Scenario: inner class 文件的插桩缺口被显式披露
    Given a GDScript source with an inner class method
    When the instrumentation census is compared per function
    Then the inner class body has no probe
    And the file is not eligible
