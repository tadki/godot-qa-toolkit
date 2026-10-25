"""Shared pytest fixtures and the BDD migration hooks."""

from __future__ import annotations

import pytest

from tests.bdd.plugin import *


@pytest.fixture
def bdd_state() -> dict:
    """Fresh mutable scenario context, discarded after each scenario."""

    return {}
