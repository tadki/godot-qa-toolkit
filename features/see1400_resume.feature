Feature: SEE-1400 mutation resume store and --resume (§SPEC-001/§SPEC-002)
  Scenario: interrupted run keeps completed verdicts on disk
    Given a mutation target with a resumable fake GUT that dies after the second mutant
    When the mutation run is interrupted mid-loop
    Then the resume store holds exactly 2 completed verdicts

  Scenario: resume skips completed mutants and merges an isomorphic report
    Given a resume store holding 2 completed verdicts for a 4-mutant target
    When the mutation run reruns with resume enabled
    Then exactly 2 mutants are skipped
    And the merged report is isomorphic to a full run

  Scenario: reused failure details carry no store envelope fields
    Given a resume store holding every mutant verdict for a 4-mutant target
    When the mutation run reruns with resume enabled
    Then every reported failure detail holds exactly the seven verdict fields

  Scenario: fingerprint mismatch re-runs every mutant
    Given a resume store holding 2 completed verdicts for a 4-mutant target
    When the target source is touched and the run reruns with resume enabled
    Then no mutant is skipped and every mutant runs fresh

  Scenario: resume without any store is a plain run
    Given a mutation target with no prior store
    When the mutation run reruns with resume enabled
    Then no mutant is skipped and every mutant runs fresh

  Scenario: corrupted store entries degrade to no history
    Given a resume store whose entries are corrupted
    When the mutation run reruns with resume enabled
    Then no mutant is skipped and every mutant runs fresh
