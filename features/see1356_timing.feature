Feature: SEE-1356 L1 timing store budget negotiation
  Scenario: cache hit feeds the anchor; baseline is still measured
    Given a mutation target with a cached baseline sample of 108 seconds
    When the mutation run resolves its timeout budget from the timing store
    Then the budget anchor comes from the cache
    And the baseline GUT run still executes as the kill-diff control
