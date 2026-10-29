"""ReAct-style self-correction execution pipeline module (Phase 7).

Implements the 4th prompting technique:
1. ReAct self-correction loop:
   Thought -> Action -> Observation
2. Error categorization:
   Retries only for syntax errors, unknown columns, unknown tables, or type mismatches.
   Never retries safety blocks, cancellation, or permission denials.
3. Up to 2 retries with Rich terminal panels for each step.
4. Beginner-friendly error formatting.
"""

import re
from typing import Any, Dict, List, Optional, Tuple, Union

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from devdb.schema import schema_to_text

console = Console()

# Categories of errors that are safe and eligible for ReAct self-correction
RETRYABLE_PATTERNS = [
    (r"no such column|undefinedcolumn|column .* does not exist|has no column named", "unknown_column"),
    (r"no such table|undefinedtable|relation .* does not exist|table .* does not exist", "unknown_table"),
    (r"syntax error|syntaxerror|near .* syntax error|incomplete input|parse error", "syntax_error"),
    (r"datatype mismatch|type mismatch|invalid input syntax|conversion error|cannot cast", "type_mismatch"),
    (r"ambiguous column|column reference .* is ambiguous", "ambiguous_column"),
    (r"no such function|undefinedfunction|function .* does not exist", "undefined_function"),
    (r"not null constraint failed|null value in column .* violates not-null", "not_null_violation"),
    (r"unknown collection|collection .* does not exist|ns not found", "unknown_collection"),
    (r"unknown field|field .* does not exist|bad query|bad value", "invalid_field"),
    (r"operationfailure|command failed|cannot use .* with", "operation_failure"),
    (r"duplicatekey|e11000 duplicate key", "duplicate_key"),
]

# Patterns that MUST NEVER be retried
BLOCKED_PATTERNS = [
    r"safety guardrail",
    r"strictly forbidden",
    r"safety violation",
    r"permission denied",
    r"insufficientprivilege",
    r"cancelled by user",
    r"aborted",
    r"confirmation mismatch",
    r"blocked by foreign keys",
    r"safe delete check",
]


def is_retryable_error(err: Union[Exception, str]) -> Tuple[bool, str]:
    """
    Determine if a database error is eligible for ReAct self-correction.

    Returns:
        (can_retry: bool, error_category: str)
    """
    if type(err).__name__ == "SafetyViolationError" or "safetyviolationerror" in str(type(err)).lower():
        return False, "safety_block"

    msg = str(err).lower()

    # Rule 2: Never retry safety blocks or user cancellations
    if any(k in msg for k in ["safety", "blocked", "strictly forbidden", "drop database", "truncate", "drop schema"]):
        return False, "safety_block"

    if any(k in msg for k in ["permission denied", "insufficientprivilege"]):
        return False, "permission_denied"

    if any(k in msg for k in ["connection timed out", "connection refused", "could not connect"]):
        return False, "connection_error"

    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, msg):
            return False, "safety_block"

    # Check for retryable categories
    for pattern, category in RETRYABLE_PATTERNS:
        if re.search(pattern, msg):
            return True, category

    return False, "non_retryable"


def format_beginner_friendly_error(err: Union[Exception, str], db_type: str = "sqlite") -> str:
    """
    Transform raw database engine errors into clear, educational explanations
    tailored for beginner developers.
    """
    raw_str = str(err)
    msg = raw_str.lower()

    tip = ""
    header = ""

    if type(err).__name__ == "SafetyViolationError" or "safety" in msg:
        header = "[bold red]Safety Guardrail Blocked[/bold red]"
        explanation = "This SQL statement was blocked by DevDB safety checks because it could cause irreversible data loss."
        tip = "💡 DevDB automatically protects against catastrophic operations (e.g. DROP DATABASE, TRUNCATE, DELETE without WHERE)."

    elif re.search(r"no such column|undefinedcolumn|column .* does not exist|has no column named", msg):
        header = "[bold red]Unknown Column (Not Found)[/bold red]"
        # Extract column name if available
        match = re.search(r"column\s+[`\"']?([\w\.\-]+)[`\"']?", msg) or re.search(r"no such column:\s*([\w\.\-]+)", msg)
        col = match.group(1) if match else "specified column"
        explanation = f"The database table does not have a column named [bold cyan]{col}[/bold cyan]."
        tip = "💡 Check the schema above or run 'schema' to see the exact column names available for this table."

    elif re.search(r"no such table|undefinedtable|relation .* does not exist|table .* does not exist", msg):
        header = "[bold red]Unknown Table (Not Found)[/bold red]"
        match = re.search(r"table\s+[`\"']?([\w\.\-]+)[`\"']?", msg) or re.search(r"relation\s+[`\"']?([\w\.\-]+)[`\"']?", msg)
        tbl = match.group(1) if match else "specified table"
        explanation = f"The table [bold cyan]{tbl}[/bold cyan] does not exist in this database."
        tip = "💡 Type 'schema' to inspect all tables currently defined in your database."

    elif re.search(r"syntax error|syntaxerror|near .* syntax error|incomplete input|parse error", msg):
        header = "[bold red]SQL Syntax Error[/bold red]"
        match = re.search(r"near\s+[\"']([^\"']+)[\"']", msg)
        near = f" near [bold yellow]{match.group(1)}[/bold yellow]" if match else ""
        explanation = f"The database could not parse the SQL statement{near}. There may be a typo or misplaced keyword."
        tip = "💡 Check for unclosed quotation marks, missing commas between columns, or misspelled SQL keywords."

    elif re.search(r"datatype mismatch|type mismatch|invalid input syntax|cannot cast", msg):
        header = "[bold red]Data Type Mismatch[/bold red]"
        explanation = "A value provided does not match the column's expected data type."
        tip = "💡 Ensure you aren't trying to insert text into an integer/numeric column, or verify date string formats (YYYY-MM-DD)."

    elif re.search(r"foreign key constraint failed|violates foreign key constraint", msg):
        header = "[bold red]Foreign Key Constraint Violation[/bold red]"
        explanation = "This operation references a parent record that does not exist, or attempts to delete a parent row still referenced by child rows."
        tip = "💡 Run 'validate_relationships' or 'safe_delete_check' to analyze referential integrity."

    elif re.search(r"unique constraint failed|duplicate key value", msg):
        header = "[bold red]Unique Constraint Violation[/bold red]"
        explanation = "A record with this identifier or unique field value already exists."
        tip = "💡 Pick a different unique value or check existing records using a SELECT query."

    elif re.search(r"permission denied|insufficientprivilege", msg):
        header = "[bold red]Permission Denied[/bold red]"
        explanation = "Your active database user lacks privileges to perform this operation on the target table."
        tip = "💡 A database administrator must GRANT the appropriate privilege (e.g. SELECT, INSERT) to your user."

    else:
        header = "[bold red]Database Execution Error[/bold red]"
        explanation = raw_str.split("\n")[0]
        tip = "💡 Review the SQL statement and active schema to verify table and column names."

    lines = [
        f"❌ {header}",
        f"{explanation}\n",
        f"{tip}\n",
        f"[dim italic]Raw Database Message: {raw_str.strip()}[/dim italic]"
    ]
    return "\n".join(lines)


def build_react_correction_prompt(
    original_request: str,
    failed_sql: str,
    db_error: str,
    schema: Any,
    db_type: str = "sqlite",
    attempt: int = 1,
    max_attempts: int = 2
) -> Tuple[str, str]:
    """
    Construct system and user prompts for the ReAct self-correction step.
    Returns: (system_prompt, user_prompt)
    """
    schema_str = schema_to_text(schema) if isinstance(schema, dict) else str(schema)

    if db_type.lower() == "mongodb":
        system_prompt = f"""You are a senior database architect executing a ReAct (Reason + Act) self-correction loop in a MongoDB DEVELOPMENT environment.

Target Database Dialect: MongoDB (PyMongo / JSON)

Database Schema:
{schema_str}

ReAct Self-Correction Protocol:
Your previous MongoDB JSON operation attempt failed execution with a database error.
You must carefully diagnose the failure, inspect the exact Database Schema above, and produce a corrected JSON operation.

Step 1 (Thought):
Analyze the database error. Why did the previous MongoDB operation fail?
- Did you use a non-existent collection or field name?
- Was an aggregation stage or operator malformed?
- Did an update or filter syntax violate PyMongo constraints?
Cross-reference the Database Schema above to verify exact collection and field names.

Step 2 (Action):
Generate the corrected structured JSON operation matching PyMongo specifications.

Step 3 (Reasoning & Fix Plan):
Populate the reasoning object with 'error_cause' and 'fix_applied'.

Rules:
1. MongoDB has NO SQL. Output pure JSON operation object.
2. Permitted operations: find, count, aggregate, update, delete, create_index, insert_fake, create_user, add_reference.
3. Return ONLY a single valid JSON object. No markdown fences.

Required JSON Structure:
{{
  "thought": "<detailed root cause diagnosis and how this correction resolves it>",
  "reasoning": {{
    "collections_involved": ["<collection>"],
    "keys": ["<keys>"],
    "risks": "<evaluation>",
    "error_cause": "<exact explanation of why previous operation failed>",
    "fix_applied": "<exact description of changes made to fix it>"
  }},
  "operation": "find" | "count" | "aggregate" | "update" | "delete" | "create_index" | "insert_fake" | "create_user",
  "collection": "<collection_name>",
  "filter": {{}},
  "projection": {{}},
  "limit": 50,
  "explanation": "<beginner-friendly explanation of why the fix works>"
}}"""
        user_prompt = f"""Original User Request: {original_request}
Failed Operation: {failed_sql}
Database Error: {db_error}
Correction Attempt: {attempt} of {max_attempts}

Please diagnose the error using Thought -> Action and return the corrected MongoDB JSON operation."""
        return system_prompt, user_prompt

    system_prompt = f"""You are a senior database architect executing a ReAct (Reason + Act) self-correction loop in a DEVELOPMENT environment.

Target Database Dialect: {db_type}

Database Schema:
{schema_str}

ReAct Self-Correction Protocol:
Your previous SQL attempt failed execution with a database error.
You must carefully diagnose the failure, inspect the exact Database Schema above, and produce a corrected SQL statement.

Step 1 (Thought):
Analyze the database error. Why did the previous SQL fail?
- Did you use a non-existent column or table name?
- Was there a syntax typo?
- Was a JOIN or table prefix missing?
- Did you violate a data type or constraint?
Cross-reference the Database Schema above to find the exact column/table names.

Step 2 (Action):
Generate the corrected SQL statement that strictly matches the schema.

Step 3 (Reasoning & Fix Plan):
Populate the reasoning object with 'error_cause' and 'fix_applied'.

Rules:
1. Use ONLY tables and columns explicitly present in the Database Schema.
2. Dialect must be valid for {db_type}.
3. Return ONLY a single valid JSON object. No markdown fences.

Required JSON Structure:
{{
  "thought": "<detailed root cause diagnosis and how this correction resolves it>",
  "reasoning": {{
    "tables_involved": ["<table1>", "<table2>"],
    "keys": ["<keys>"],
    "dependency_order": "<order>",
    "risks": "<evaluation>",
    "error_cause": "<exact explanation of why previous SQL failed>",
    "fix_applied": "<exact description of changes made to fix it>"
  }},
  "action": "query" | "write" | "fake_data" | "create_user" | "relationship",
  "sql": "<corrected_sql>",
  "explanation": "<beginner-friendly explanation of why the fix works>"
}}"""

    user_prompt = f"""Original User Request: {original_request}
Failed SQL: {failed_sql}
Database Error: {db_error}
Correction Attempt: {attempt} of {max_attempts}

Please diagnose the error using Thought -> Action and return the corrected JSON."""

    return system_prompt, user_prompt


def render_observation_panel(
    failed_sql: str,
    error: Union[Exception, str],
    attempt: int,
    db_type: str = "sqlite"
) -> None:
    """Render Rich panel for the initial Observation step (Database Error)."""
    friendly_msg = format_beginner_friendly_error(error, db_type=db_type)
    content = [
        f"[bold red]Execution Failed on Attempt {attempt}[/bold red]\n",
        f"[bold cyan]Failed SQL:[/bold cyan]",
    ]
    console.print(Panel(
        "\n".join(content) + "\n" + failed_sql + "\n\n" + friendly_msg,
        title=f"Observation: Database Error (Attempt {attempt})",
        border_style="red"
    ))
    console.print()


def render_react_step(
    attempt: int,
    max_attempts: int,
    thought: str,
    action: str,
    sql: str,
    explanation: str,
    reasoning: Optional[Dict[str, Any]] = None,
    verbose: bool = False
) -> None:
    """Render Rich panels for the Thought -> Action self-correction step."""
    # 1. Thought Panel
    thought_lines = [f"[bold white]{thought}[/bold white]"]
    if verbose and reasoning:
        thought_lines.append("\n[dim cyan]Deep Reasoning Breakdown:[/dim cyan]")
        for k, v in reasoning.items():
            thought_lines.append(f"  • [dim bold]{k}:[/dim bold] [dim]{v}[/dim]")

    console.print(Panel(
        "\n".join(thought_lines),
        title=f"Thought (ReAct Self-Correction - Attempt {attempt}/{max_attempts})",
        border_style="cyan"
    ))

    # 2. Action Panel
    action_lines = [
        f"[bold cyan]Proposed Action:[/bold cyan] {action.upper()}",
        f"[bold cyan]Explanation:[/bold cyan] {explanation}\n",
        f"[bold cyan]Corrected SQL:[/bold cyan]"
    ]
    console.print(Panel(
        "\n".join(action_lines),
        title=f"Action (Corrected SQL - Attempt {attempt}/{max_attempts})",
        border_style="yellow"
    ))
    if sql:
        syntax = Syntax(sql, "sql", theme="monokai", line_numbers=False)
        console.print(syntax)
        console.print()
