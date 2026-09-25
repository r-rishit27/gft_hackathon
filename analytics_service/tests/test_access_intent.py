import httpx
import pytest

from analytics_service.access_intent import check_question_access, denied_sql_tables
from analytics_service.errors import AnalyticsError
from analytics_service.model_client import ModelClient

KNOWN = ["Party", "Transaction", "RiskCaseEvent", "RiskScores"]


@pytest.mark.parametrize("question", ["Count open cases", "How many alerts?", "Show risk scores", "Show RiskCaseEvent"])
def test_restricted_question_is_explicitly_denied(question):
    with pytest.raises(AnalyticsError, match="You do not have the required access") as error:
        check_question_access(question, ["Party", "Transaction"], KNOWN)
    assert error.value.code == "resource_denied"


@pytest.mark.parametrize("question", ["Count transactions", "Count customers without risk scores", "Transactions, not alerts", "Count customers"])
def test_ambiguous_mentions_are_left_to_sql_validator(question):
    check_question_access(question, ["Party", "Transaction"], KNOWN)


@pytest.mark.parametrize("sql,denied", [
    ("SELECT COUNT(*) FROM RiskCaseEvent", True),
    ("SELECT COUNT(*) FROM `project.dataset.RiskCaseEvent`", True),
    ("SELECT COUNT(*) FROM Transaction", False),
    ("WITH RiskCaseEvent AS (SELECT * FROM Transaction) SELECT * FROM RiskCaseEvent", False),
    ("SELECT 'RiskCaseEvent' FROM Party", False),
    ("WITH cases AS (SELECT * FROM RiskCaseEvent) SELECT * FROM cases", True),
    (None, False),
    ("SELECT (", False),
])
def test_only_physical_denied_tables_are_classified(sql, denied):
    assert denied_sql_tables(sql, ["Party", "Transaction"], KNOWN) is denied


@pytest.mark.parametrize("sql,code", [
    ("SELECT COUNT(*) FROM RiskCaseEvent", "resource_denied"),
    ("SELECT wrong_column FROM Transaction", "model_rejected"),
    (None, "model_rejected"),
])
def test_invalid_model_sql_access_denial_is_not_generic(sql, code):
    response = {"sql": sql, "schema_valid": False, "schema_violations": ["invalid"]}
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=response))) as transport:
        model = ModelClient("http://localhost", transport)
        with pytest.raises(AnalyticsError) as error:
            model.generate("question", allowed_columns={"Party": ["party_id"], "Transaction": ["transaction_id"]})
    assert error.value.code == code
