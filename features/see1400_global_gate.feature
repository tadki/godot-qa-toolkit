Feature: SEE-1400 cross-process godot slot gate (§SPEC-003)
  Scenario: one slot serializes concurrent godot subprocesses
    Given the global slot capacity is pinned to 1
    When two concurrent workers run GUT through the gated exit
    Then the observed concurrent godot subprocess count never exceeded 1

  Scenario: slot capacity follows the environment
    Given the global slot capacity env is set to 3
    When the gate resolves its capacity
    Then the resolved capacity is 3

  Scenario: missing fcntl degrades with a warning and never blocks
    Given the platform has no fcntl module
    When a gated GUT run executes
    Then the run completes without holding any slot
