Feature: SEE-1288 fork CLI wiring restores the 90s QUICK_TIMEOUT first-call budget
  Scenario: AC-MCP-001 Submodule checkout without dist still wires the owner fork
    Given the godot-mcp submodule launch dir exists
    And the fork CLI build products are absent from the submodule checkout
    When the launcher resolves its fork CLI path
    Then the resolved fork CLI must be executable
    And the fork wiring path must emit FORK_WIRED instead of the upstream fallback warning

  Scenario: AC-MCP-002 Upstream fallback branch is preserved for explicit overrides
    Given the fork CLI env override points at a missing path
    When the launcher fork wiring executes against that override
    Then the upstream fallback warning branch is retained in the launcher
    And the wiring block must not abort the launcher on a missing fork

  Scenario: AC-MCP-003 Proxy and launcher default-inject the 90s QUICK_TIMEOUT
    Given the fork wiring is active
    Then the proxy default-injects GODOT_MCP_QUICK_TIMEOUT_MS of 90000
    And the launcher default-injects GODOT_MCP_QUICK_TIMEOUT_MS of 90000

  Scenario: AC-MCP-004 Doctor registration must not fail on the live launch path
    Given the current repository provides the godot-mcp launch entry
    When doctor runs its registration check
    Then no registration FAIL may reference a launch path inside this repository
