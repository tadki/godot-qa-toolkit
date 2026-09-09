Feature: Leaderboard refresh
  Background:
    Given the leaderboard is loaded

  Scenario: Player taps refresh
    Given 3 hearts in inventory
    When the player taps refresh
    Then ranks update within 200ms
    And the streak icon is visible

  Scenario: Refresh failure keeps old data
    Given 0 hearts in inventory
    When refresh fails
    Then the retry button appears
