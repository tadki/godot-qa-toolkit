Feature: SEE-1400 §SPEC-012 launch-failure guard narrowing
  Scenario: unrelated resource warnings do not swallow real mutant kills
    Given a mutation target whose GUT rounds carry unrelated resource warnings
    When the mutation run executes
    Then every mutant is killed and none is run_error

  Scenario: a true launch failure still classifies as run_error
    Given a mutation target whose GUT rounds fail to launch the runner script
    When the mutation run executes
    Then every mutant verdict is run_error and none is killed
