Feature: gqt mutation defaults to per-mutant parallel slicing (SEE-1413)

  Scenario: Single file with --jobs omitted goes parallel by default
    Given a mutation target with 12 mutation sites and a passing GUT stub
    When gqt mutation runs the single file with jobs omitted
    Then the report top-level shape is flat with jobs greater than 1
    And the flat summary keeps existing keys and adds jobs

  Scenario: Mutation count not exceeding jobs auto-falls back to serial before any GUT call
    Given a mutation target with 3 mutation sites and a passing GUT stub
    When gqt mutation runs the single file with jobs 8
    Then the report falls back to serial with jobs 1
    And zero prewarm imports were performed

  Scenario: Explicit --jobs 1 still forces serial
    Given a mutation target with 12 mutation sites and a passing GUT stub
    When gqt mutation runs the single file with jobs 1
    Then the report reports jobs 1 with a single result

  Scenario: --jobs help text documents per-file and per-mutant slicing
    When the mutation --jobs help text is rendered
    Then the text mentions per-file AND per-mutant slicing
    And the text mentions single file support
