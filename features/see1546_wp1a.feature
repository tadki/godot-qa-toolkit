Feature: SEE-1546 WP1a — complexity 门对 var def 文件的失明修复与 Class 伪函数过滤
  WP1a 窄修复：token 级 def 消毒 + Class 聚合块过滤 + 原始 AST 决策点 mini parity
  + 契约副作用基线重定。GDScript 产品源零改动；消毒仅为内存测量投影。

  Scenario: var def 文件端到端回收全部函数
    Given a GDScript file using def as a local variable name
    When the complexity collector processes the file
    Then every named function is recovered with no error
    And a second file relying on def in parameters attributes and dict keys is also recovered

  Scenario: 字符串与注释中的 def 保持原样（双向差分）
    Given a GDScript file with def only inside strings and comments
    When the complexity collector processes the file
    Then the file parses without error
    And the sanitized projection leaves every string and comment verbatim

  Scenario: 控制流与关键字位在消毒投影中不变
    Given a GDScript file whose def tokens sit next to else and func keyword positions
    When the sanitizer builds the in-memory projection
    Then control flow tokens are untouched
    And the function named def is renamed only at its name position

  Scenario: 消毒关闭时同样的文件必然失败（独立反例）
    Given the raw gd2py pipeline without sanitization
    When a file with var def is converted
    Then conversion fails with a syntax error proving the sanitizer is the fix

  Scenario: Class 聚合块不再冒充 Function 且真实函数不丢失
    Given a GDScript file with an inner class and outer functions
    When the complexity collector processes the file
    Then the inner class name is not reported as a function
    And the inner class methods and outer functions are all reported

  Scenario: Class 过滤有独立反例（未过滤时聚合块确实冒充）
    Given the raw radon cc_visit output for a file with an inner class
    When the unfiltered block list is inspected
    Then the aggregate class block is present proving the filter is load-bearing

  Scenario: 带决策点 lambda 的宿主在 parity 中显式披露缺口
    Given a GDScript file whose host function contains a branching lambda
    When the parity oracle diffs raw AST against radon records
    Then the host is reported in the lambda_gap lane with the exact gap size
    And lambda-free functions stay in the strict match lane

  Scenario: 第二层转换失败按文件隔离且不强行通过
    Given a GDScript file with a non-def Python keyword collision
    When the complexity collector processes the file
    Then the file stays unparseable with an explicit reason
    And sibling files in the same run still report their functions

