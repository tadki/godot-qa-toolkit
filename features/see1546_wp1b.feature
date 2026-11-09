Feature: SEE-1546 WP1b — 中立函数 registry 与全量逐函数 CC parity
  registry 只描述源码里有什么 callable（fid 四元组、static 单计、lambda 与
  inner class 独立登记、Class 聚合永不入）；probe manifest 构建期物化 fid；
  parity 出现序配对、七车道闭合；diff 与 scope escape 不计治理净收益。

  Scenario: registry 登记全部 callable 形态
    Given a GDScript source with static inner class lambda and plain functions
    When the file registry is built
    Then static inner class lambda and plain entries all exist
    And the class aggregate never becomes an entry

  Scenario: static 双计 lambda 缺失与 Class 聚合三反例
    Given the same GDScript source
    When the registry invariants are checked
    Then static functions are counted exactly once
    And the branching lambda has its own entry with a host backref
    And no entry carries the kind class_def

  Scenario: probe manifest 构建期物化 fid
    Given the registry for the GDScript source
    When the probe manifest v2 is built
    Then every probe carries a non-null fid and owner kind
    And the probe order matches the instrumentation manifest

  Scenario: inner class 缺口隔离不污染同文件其余条目
    Given an inner class method in the registry
    When the probe manifest census is assembled
    Then the inner class method lands in census gaps with partial instrumentation
    And the sibling entries keep their own probe plan and t equals i

  Scenario: 宿主 T census 剪除 lambda 子树
    Given a host function containing a branching lambda
    When the per function decomposition is assembled
    Then the host and lambda T line sets are disjoint

  Scenario: parity 出现序配对跨作用域同名
    Given two same-named functions in different lexical scopes
    When the parity rows are produced
    Then both pair by occurrence and land in the match lane

  Scenario: 七车道与闭合不变量
    Given parity rows over fixtures with match lambda_gap lambda_disclosed and missing lanes
    When the lane totals are summed
    Then the closure invariant equals func entries plus lambda entries

  Scenario: diff 标记 scope escape 且不计治理净收益
    Given added entries covering all three escape rules
    When the registry diff is computed
    Then every escape row is marked with its rule
    And countable added equals added minus scope escape
