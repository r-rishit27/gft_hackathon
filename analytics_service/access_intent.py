"""Helpful access-denial classification; never grants SQL execution permission."""
import re

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from .errors import AnalyticsError

ACCESS_MESSAGE = "You do not have the required access to answer this question. Check your profile for permitted tables or contact your administrator."
TOPICS = {
    "Transaction": r"\btransactions?\b|\bpayments?\b",
    "RiskCaseEvent": r"\balerts?\b|\bsars?\b|\bsar filings?\b|\binvestigations?\b|\b(?:risk|investigation) case(?:s| events?)?\b|\b(?:many|count|distinct|open|closed) cases?\b|\bcase count\b",
    "RiskScores": r"\brisk scores?\b|\bhighest[- ]risk\b|\blowest[- ]risk\b",
    "Explainability": r"\bexplainability\b|\brisk attributions?\b",
    "InteractionEvent": r"\binteraction events?\b|\blogins?\b|\bpassword changes?\b|\bkyc changes?\b",
    "AccountPartyLink": r"\baccount (?:owners?|holders?|relationships?|links?)\b|\bprimary.holders?\b",
    "PartySupplementaryData": r"\bsupplementary (?:customer )?data\b",
    "RetailPartiesRegistration": r"\bretail registrations?\b",
    "CommercialPartiesRegistration": r"\bcommercial registrations?\b",
}


def check_question_access(question, allowed, known):
    # Negation and alternatives can mention a table without requesting its data.
    # Leave those to SQL validation rather than guessing the user's intent.
    if re.search(r"\b(?:not|without|exclude|excluding|ignore|instead|rather|or)\b|don't", question, re.I):
        return
    for table in set(known) - set(allowed):
        exact = rf"(?<!\w){re.escape(table)}(?!\w)"
        if re.search(exact, question, re.I) or (table in TOPICS and re.search(TOPICS[table], question, re.I)):
            raise AnalyticsError("resource_denied", ACCESS_MESSAGE, 403)


def denied_sql_tables(sql, allowed, known):
    """Classify actual physical sources, not CTE names or quoted text."""
    if not isinstance(sql, str) or len(sql) > 32_000:
        return False
    try:
        trees = sqlglot.parse(sql, read="bigquery")
        denied = {name.casefold() for name in known} - {name.casefold() for name in allowed}
        for tree in trees:
            if tree is None:
                continue
            for scope in traverse_scope(tree):
                for _, source in scope.selected_sources.values():
                    if isinstance(source, exp.Table) and source.name.casefold() in denied:
                        return True
    except (sqlglot.errors.SqlglotError, ValueError):
        return False
    return False
