"""Tool schemas, derived from the source registry.

A new source needs no edit here: the enums come from the registry, so adding a
module and a registry line is the whole change.
"""

from typing import Any

from cadence_backend.sources import document_sources, sql_sources


def build_tools() -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []

    sql_ids = [s.id for s in sql_sources()]
    if sql_ids:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "run_sql",
                    "description": (
                        "Run one read-only SQL query against a source. SELECT and WITH only; "
                        "a single statement. Returns columns, rows and the row count. Each "
                        "source is a separate database — a query cannot join across sources."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "source": {
                                "type": "string",
                                "enum": sql_ids,
                                "description": "Which source to query.",
                            },
                            "purpose": {
                                "type": "string",
                                "description": (
                                    "One short sentence, written for the user, describing "
                                    "what this query is for. Shown in the trace. E.g. "
                                    "'Pulling cancellations around each price rise'."
                                ),
                            },
                            "sql": {"type": "string", "description": "The SQL to execute."},
                        },
                        "required": ["source", "purpose", "sql"],
                    },
                },
            }
        )

    doc_ids = [s.id for s in document_sources()]
    if doc_ids:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "search_documents",
                    "description": (
                        "Search a document source. Use when a result looks surprising, or "
                        "when the question touches measurement coverage, methodology or "
                        "documented gaps."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "source": {
                                "type": "string",
                                "enum": doc_ids,
                                "description": "Which document source to search.",
                            },
                            "query": {
                                "type": "string",
                                "description": "What to look for.",
                            },
                        },
                        "required": ["source", "query"],
                    },
                },
            }
        )

    return tools
