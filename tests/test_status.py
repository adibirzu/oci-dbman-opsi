from dbman_opsi.status import (
    data_safe_status,
    dbm_status,
    is_enabled,
    opsi_insight_status,
)


def test_opsi_insight_status_matches_by_database_id() -> None:
    insights = [{"database-id": "db-1", "lifecycle-state": "ACTIVE"}]
    assert opsi_insight_status(insights, "db-1") == "ENABLED"
    assert opsi_insight_status(insights, "db-2") == "NOT_ENABLED"


def test_opsi_insight_status_creating_counts_as_enabled() -> None:
    insights = [{"database-id": "db-1", "lifecycle-state": "CREATING"}]
    assert opsi_insight_status(insights, "db-1") == "ENABLED"


def test_opsi_insight_status_failed_surfaces_lifecycle() -> None:
    insights = [{"database-id": "db-1", "lifecycle-state": "FAILED"}]
    assert opsi_insight_status(insights, "db-1") == "FAILED"


def test_data_safe_status_matches_by_database_id() -> None:
    targets = [{"lifecycle-state": "ACTIVE", "database-details": {"database-id": "db-1"}}]
    assert data_safe_status(targets, "db-1") == "ENABLED"
    assert data_safe_status(targets, "other") == "NOT_ENABLED"


def test_data_safe_status_matches_by_db_system_for_base_db() -> None:
    targets = [{"lifecycle-state": "ACTIVE", "database-details": {"db-system-id": "sys-1"}}]
    # Base DB target registers against the DB system; match the DB via its system.
    assert data_safe_status(targets, "db-1", db_system_id="sys-1") == "ENABLED"
    assert data_safe_status(targets, "db-1", db_system_id="sys-2") == "NOT_ENABLED"


def test_data_safe_status_matches_autonomous() -> None:
    targets = [{"lifecycle-state": "ACTIVE", "database-details": {"autonomous-database-id": "adb-1"}}]
    assert data_safe_status(targets, "adb-1") == "ENABLED"


def test_data_safe_status_empty_is_not_enabled() -> None:
    assert data_safe_status([], "db-1") == "NOT_ENABLED"


def test_is_enabled_and_dbm_status_round_out_coverage() -> None:
    assert is_enabled("ENABLED") is True
    assert is_enabled(None) is False
    adb = {"database-management-status": "ENABLED"}
    assert dbm_status(adb, "autonomous") == "ENABLED"
