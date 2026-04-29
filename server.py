#!/usr/bin/env python3
"""
CW Live MCP Server
Consolidated live ConnectWise Manage API access — Service, Operations, and Finance domains.

Merges the functionality of three former servers:
  cw_service_mcp_server.py    (port 8085) — tickets, notes, boards, KB articles
  cw_operations_mcp_server.py (port 8086) — companies, contacts, configs, projects, time
  cw_finance_mcp_server.py    (port 8087) — invoices, agreements, MRR, opportunities

Finance tools are only registered when CW_TIER=leadership (set in .env).
Tech-tier installations set CW_TIER=tech to omit finance tools from the manifest entirely.

Runs as HTTP server on port 8085.
"""

import os
import base64
import json
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse

for env_path in (
    Path(__file__).resolve().parent / ".env",
    Path(__file__).resolve().parent.parent / ".env",
):
    if env_path.exists():
        load_dotenv(env_path)
        break

# ── CW API client ─────────────────────────────────────────────────────────────

CW_BASE = os.environ["CW_BASE_URL"]
CW_AUTH = base64.b64encode(
    f"{os.environ['CW_COMPANY_ID']}+{os.environ['CW_PUBLIC_KEY']}:{os.environ['CW_PRIVATE_KEY']}".encode()
).decode()
CW_HEADERS = {
    "Authorization": f"Basic {CW_AUTH}",
    "clientId": os.environ["CW_CLIENT_ID"],
    "Content-Type": "application/json",
}
PAGE_SIZE = 1000

# Finance tools are only exposed when CW_TIER=leadership
CW_TIER = os.environ.get("CW_TIER", "leadership").lower()
FINANCE_ENABLED = CW_TIER == "leadership"

# Product identifiers excluded from MRR calculations (one-time / non-recurring).
# Add your own setup fees, shipping, discount, and labor T&M product identifiers here.
# Values are matched case-insensitively against the product identifier field.
_JUNK_PRODUCTS: set[str] = {
    # "setup fee",
    # "shipping",
    # "labor time & material",
    # "discount",
}


def cw_get(path: str, params: Optional[dict] = None) -> list | dict:
    url = CW_BASE + path
    if params:
        url += "?" + urlencode(params)
    req = Request(url, headers=CW_HEADERS)
    for attempt in range(3):
        try:
            resp = urlopen(req, timeout=30)
            return json.loads(resp.read())
        except HTTPError as e:
            if e.code == 429:
                time.sleep(2 ** attempt)
            else:
                return []
    return []


def cw_post(path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = Request(CW_BASE + path, data=data, headers=CW_HEADERS, method="POST")
    resp = urlopen(req, timeout=30)
    return json.loads(resp.read())


def cw_patch(path: str, operations: list) -> dict:
    data = json.dumps(operations).encode()
    req = Request(CW_BASE + path, data=data, headers=CW_HEADERS, method="PATCH")
    resp = urlopen(req, timeout=30)
    return json.loads(resp.read())


def cw_paginate(
    path: str,
    conditions: Optional[str] = None,
    child_conditions: Optional[str] = None,
    fields: Optional[str] = None,
    order_by: Optional[str] = None,
) -> list:
    results = []
    page = 1
    while True:
        params: dict = {"pageSize": PAGE_SIZE, "page": page}
        if conditions:
            params["conditions"] = conditions
        if child_conditions:
            params["childConditions"] = child_conditions
        if fields:
            params["fields"] = fields
        if order_by:
            params["orderBy"] = order_by
        data = cw_get(path, params)
        if not data:
            break
        results.extend(data)
        if len(data) < PAGE_SIZE:
            break
        page += 1
    return results


# ── Helpers ───────────────────────────────────────────────────────────────────

def _trunc(text: str, max_chars: int = 200) -> str:
    text = (text or "").strip()
    return text[:max_chars] + "..." if len(text) > max_chars else text


def _date(val) -> str:
    if not val:
        return "—"
    return str(val)[:10]


def _n(obj, *keys, default="—"):
    for k in keys:
        if not isinstance(obj, dict):
            return default
        obj = obj.get(k)
    return obj if obj is not None else default


def _dollar(val) -> str:
    try:
        return f"${float(val):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _build_conditions(parts: list[Optional[str]]) -> str:
    return " and ".join(p for p in parts if p)


def _safe_str(value: str, max_len: int = 100) -> str:
    """Sanitize a user-supplied string for use in CW API query conditions.

    Strips characters used in CW query injection: quotes, parens, brackets.
    Truncates to max_len to prevent excessively long conditions.
    """
    if not value:
        return ""
    sanitized = "".join(c for c in value if c not in ('"', "'", "(", ")", "[", "]"))
    return sanitized[:max_len].strip()


def _pagination_footer(total: int, limit: int, offset: int = 0) -> str:
    shown_end = offset + limit
    if total <= shown_end:
        return ""
    return f"\n\n_Showing {offset + 1}–{min(shown_end, total)} of {total}. Pass offset={shown_end} for next page._"


def _is_mrr(addition: dict) -> bool:
    """Return True if this addition counts toward MRR (not a junk line item)."""
    pid = (addition.get("product", {}) or {})
    if isinstance(pid, dict):
        pid = (pid.get("identifier") or "").strip().lower()
    else:
        pid = str(pid).strip().lower()
    return pid not in _JUNK_PRODUCTS


class _StaticTokenVerifier(TokenVerifier):
    """Static bearer-token verifier for internal MCP servers."""
    def __init__(self, token: str):
        super().__init__()
        self._token = token

    async def verify_token(self, token: str) -> "AccessToken | None":
        if token == self._token:
            return AccessToken(token=token, client_id="mcp-client", scopes=[])
        return None


_mcp_auth_token = os.environ.get("MCP_AUTH_TOKEN")
_mcp_auth = _StaticTokenVerifier(_mcp_auth_token) if _mcp_auth_token else None

mcp = FastMCP("cw-live", auth=_mcp_auth)


# ══════════════════════════════════════════════════════════════════════════════
# SERVICE DOMAIN — Tickets, notes, boards, KB articles
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_ticket(ticket_id: int) -> str:
    """
    Get a single ConnectWise ticket by ID, including all notes.

    Use when: you have a specific ticket number and need full details.
    Don't use when: searching by keyword — use search_tickets instead.

    Returns full ticket detail: summary, company, board, status, priority,
    assigned tech, contact, dates, SLA status, and all ticket notes.
    """
    ticket = cw_get(f"/service/tickets/{ticket_id}")
    if not ticket or isinstance(ticket, list):
        return f"Ticket #{ticket_id} not found."

    lines = [
        f"# Ticket #{ticket.get('id')} — {ticket.get('summary', '—')}",
        "",
        f"**Company:** {_n(ticket, 'company', 'name')}",
        f"**Board:** {_n(ticket, 'board', 'name')}",
        f"**Status:** {_n(ticket, 'status', 'name')}",
        f"**Priority:** {_n(ticket, 'priority', 'name')}",
        f"**Type:** {_n(ticket, 'type', 'name')}",
        f"**Source:** {_n(ticket, 'serviceLocation', 'name')}",
        f"**Assigned To:** {_n(ticket, 'assignedTo', 'identifier')}",
        f"**Contact:** {_n(ticket, 'contact', 'name')}",
        f"**Date Entered:** {_date(ticket.get('dateEntered'))}",
        f"**Date Resolved:** {_date(ticket.get('dateResolved'))}",
        f"**SLA Status:** {_n(ticket, 'slaStatus')}",
    ]

    notes = cw_get(f"/service/tickets/{ticket_id}/notes", {"pageSize": 100, "orderBy": "dateCreated asc"})
    if notes and isinstance(notes, list):
        lines.append("")
        lines.append(f"## Notes ({len(notes)})")
        for note in notes:
            note_type = "Internal" if note.get("internalAnalysisFlag") else "Public"
            lines.append(f"\n**[{_date(note.get('dateCreated'))}] {note.get('createdBy', '—')} ({note_type}):**")
            lines.append(_trunc(note.get("text", ""), 500))

    return "\n".join(lines)


@mcp.tool()
def search_tickets(
    board: str = "",
    status: str = "",
    company_name: str = "",
    assigned_tech: str = "",
    priority: str = "",
    ticket_type: str = "",
    date_from: str = "",
    date_to: str = "",
    summary_contains: str = "",
    limit: int = 25,
    offset: int = 0,
) -> str:
    """
    Search live ConnectWise service tickets with filters.

    Use when: you need current/live ticket data from CW.
    Don't use when: doing semantic/keyword search — use a vector search tool instead.

    All filters are optional — combine any subset.
    Dates use format YYYY-MM-DD (e.g. date_from="2025-01-01").
    Returns ticket ID, summary, company, board, status, priority, tech, date.
    """
    board = _safe_str(board)
    status = _safe_str(status)
    company_name = _safe_str(company_name)
    assigned_tech = _safe_str(assigned_tech)
    priority = _safe_str(priority)
    ticket_type = _safe_str(ticket_type)
    summary_contains = _safe_str(summary_contains)
    date_from = _safe_str(date_from, max_len=10)
    date_to = _safe_str(date_to, max_len=10)
    parts = []
    if board:
        parts.append(f'board/name="{board}"')
    if status:
        parts.append(f'status/name="{status}"')
    if company_name:
        parts.append(f'company/name contains "{company_name}"')
    if assigned_tech:
        parts.append(f'assignedTo/identifier="{assigned_tech}"')
    if priority:
        parts.append(f'priority/name="{priority}"')
    if ticket_type:
        parts.append(f'type/name="{ticket_type}"')
    if date_from:
        parts.append(f"dateEntered>=[{date_from}T00:00:00Z]")
    if date_to:
        parts.append(f"dateEntered<=[{date_to}T23:59:59Z]")
    if summary_contains:
        parts.append(f'summary contains "{summary_contains}"')

    conditions = _build_conditions(parts) or None
    fields = "id,summary,company,board,status,priority,assignedTo,contact,dateEntered,dateResolved,type"

    all_results = cw_paginate("/service/tickets", conditions=conditions, fields=fields)
    total = len(all_results)
    page = all_results[offset: offset + limit]

    if not page:
        return "No tickets found."

    out = [f"Found {total} ticket(s):", "", "| # | Summary | Company | Board | Status | Priority | Tech | Date |"]
    out.append("|---|---------|---------|-------|--------|----------|------|------|")

    for t in page:
        out.append(
            f"| #{t.get('id')} | {_trunc(t.get('summary', ''), 50)} "
            f"| {_n(t, 'company', 'name')} "
            f"| {_n(t, 'board', 'name')} "
            f"| {_n(t, 'status', 'name')} "
            f"| {_n(t, 'priority', 'name')} "
            f"| {_n(t, 'assignedTo', 'identifier')} "
            f"| {_date(t.get('dateEntered'))} |"
        )

    out.append(_pagination_footer(total, limit, offset))
    return "\n".join(out)


@mcp.tool()
def get_open_tickets(
    board: str = "",
    assigned_tech: str = "",
    company_name: str = "",
    limit: int = 25,
    offset: int = 0,
) -> str:
    """
    List currently open (non-closed) ConnectWise tickets.

    Use when: checking live queue state, workload management, open ticket counts.
    Don't use when: searching by keyword — use a vector search tool instead.

    Optionally filter by board, assigned tech, or company.
    """
    parts = ["closedFlag=false"]
    if board:
        parts.append(f'board/name="{board}"')
    if assigned_tech:
        parts.append(f'assignedTo/identifier="{assigned_tech}"')
    if company_name:
        parts.append(f'company/name contains "{company_name}"')

    conditions = _build_conditions(parts)
    fields = "id,summary,company,board,status,priority,assignedTo,contact,dateEntered"

    all_results = cw_paginate("/service/tickets", conditions=conditions, fields=fields)
    total = len(all_results)
    page = all_results[offset: offset + limit]

    if not page:
        return "No open tickets found."

    out = [f"Found {total} open ticket(s):", "", "| # | Summary | Company | Board | Status | Tech | Date |"]
    out.append("|---|---------|---------|-------|--------|------|------|")
    for t in page:
        out.append(
            f"| #{t.get('id')} | {_trunc(t.get('summary', ''), 50)} "
            f"| {_n(t, 'company', 'name')} "
            f"| {_n(t, 'board', 'name')} "
            f"| {_n(t, 'status', 'name')} "
            f"| {_n(t, 'assignedTo', 'identifier')} "
            f"| {_date(t.get('dateEntered'))} |"
        )

    out.append(_pagination_footer(total, limit, offset))
    return "\n".join(out)


@mcp.tool()
def add_ticket_note(ticket_id: int, text: str, internal: bool = True) -> str:
    """
    Add a note to a ConnectWise ticket.

    Use when: logging work done, adding context, or communicating status on a ticket.

    Args:
        ticket_id: The CW ticket ID
        text: Note text content
        internal: True for internal analysis note (default), False for public/external note
    """
    body = {
        "text": text,
        "detailDescriptionFlag": False,
        "internalAnalysisFlag": internal,
        "resolutionFlag": False,
    }
    try:
        result = cw_post(f"/service/tickets/{ticket_id}/notes", body)
        note_id = result.get("id", "?")
        note_type = "internal" if internal else "public"
        return f"Note #{note_id} added to ticket #{ticket_id} ({note_type})."
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("Failed to add note to ticket %s: %s", ticket_id, e, exc_info=True)
        return "Failed to add note. Check server logs for details."


@mcp.tool()
def create_ticket(
    company_id: int,
    summary: str,
    board_name: str,
    priority_name: str = "Medium",
    description: str = "",
    assigned_tech: str = "",
    contact_id: int = 0,
) -> str:
    """
    Create a new ConnectWise service ticket.

    Use when: logging a new issue on behalf of a client or for internal work.
    Don't use when: just adding notes to an existing ticket — use add_ticket_note.

    Args:
        company_id: CW company ID (use get_company to look up)
        summary: Ticket summary/title
        board_name: Service board name (e.g. "Service Board", "Network")
        priority_name: Priority name (default "Medium")
        description: Initial description text (optional)
        assigned_tech: Tech identifier to assign (optional)
        contact_id: Contact ID to link (optional)
    """
    body: dict = {
        "summary": summary,
        "company": {"id": company_id},
        "board": {"name": board_name},
        "priority": {"name": priority_name},
    }
    if description:
        body["initialDescription"] = description
    if assigned_tech:
        body["assignedTo"] = {"identifier": assigned_tech}
    if contact_id:
        body["contact"] = {"id": contact_id}

    try:
        result = cw_post("/service/tickets", body)
        ticket_id = result.get("id", "?")
        return f"Ticket #{ticket_id} created: {summary}\nBoard: {board_name} | Priority: {priority_name}"
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("Failed to create ticket: %s", e, exc_info=True)
        return "Failed to create ticket. Check server logs for details."


@mcp.tool()
def update_ticket_status(ticket_id: int, status_name: str, board_name: str = "") -> str:
    """
    Update the status of a ConnectWise ticket.

    Args:
        ticket_id: The CW ticket ID to update
        status_name: New status name (e.g. "In Progress", "Waiting Customer", "Closed")
        board_name: Board name (optional, only needed when moving boards)
    """
    operations = [{"op": "replace", "path": "/status", "value": {"name": status_name}}]
    if board_name:
        operations.append({"op": "replace", "path": "/board", "value": {"name": board_name}})
    try:
        result = cw_patch(f"/service/tickets/{ticket_id}", operations)
        summary = result.get("summary", "—")
        new_status = (result.get("status") or {}).get("name", status_name)
        return f"Ticket #{ticket_id} updated — Status: {new_status} | Summary: {summary}"
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("Failed to update ticket %s: %s", ticket_id, e, exc_info=True)
        return "Failed to update ticket. Check server logs for details."


@mcp.tool()
def get_boards() -> str:
    """
    List all ConnectWise service boards.

    Use when: you need board names for ticket creation or filtering.
    """
    boards = cw_paginate("/service/boards", fields="id,name,inactiveFlag")
    if not boards:
        return "No boards found."

    active = [b for b in boards if not b.get("inactiveFlag")]
    inactive = [b for b in boards if b.get("inactiveFlag")]

    out = [f"Found {len(boards)} boards ({len(active)} active, {len(inactive)} inactive):", ""]
    out.append("| ID | Name | Active |")
    out.append("|----|------|--------|")
    for b in sorted(boards, key=lambda x: x.get("name", "")):
        active_flag = "No" if b.get("inactiveFlag") else "Yes"
        out.append(f"| {b.get('id')} | {b.get('name', '—')} | {active_flag} |")

    return "\n".join(out)


@mcp.tool()
def get_board_statuses(board_id: int) -> str:
    """
    List all statuses for a specific service board.

    Args:
        board_id: CW board ID (use get_boards to find IDs)
    """
    statuses = cw_paginate(f"/service/boards/{board_id}/statuses", fields="id,name,closedStatus,escalationStatus")
    if not statuses:
        return f"No statuses found for board {board_id}."

    out = [f"Found {len(statuses)} statuses for board {board_id}:", ""]
    out.append("| ID | Name | Closed | Escalation |")
    out.append("|----|------|--------|------------|")
    for s in sorted(statuses, key=lambda x: x.get("name", "")):
        out.append(
            f"| {s.get('id')} | {s.get('name', '—')} "
            f"| {'Yes' if s.get('closedStatus') else 'No'} "
            f"| {s.get('escalationStatus', '—')} |"
        )

    return "\n".join(out)


@mcp.tool()
def get_ticket_time(ticket_id: int) -> str:
    """
    Get all time entries logged against a ticket.

    Returns total hours, per-tech breakdown, and individual entry details.
    """
    entries = cw_paginate(
        "/time/entries",
        conditions=f'chargeToId={ticket_id} and chargeToType="ServiceTicket"',
        fields="id,member,actualHours,billableOption,workType,notes,timeStart",
    )
    if not entries:
        return f"No time entries found for ticket #{ticket_id}."

    total_hours = sum(e.get("actualHours", 0) or 0 for e in entries)
    by_tech: dict[str, float] = {}
    for e in entries:
        tech = (e.get("member") or {}).get("identifier", "Unknown")
        by_tech[tech] = by_tech.get(tech, 0) + (e.get("actualHours", 0) or 0)

    out = [
        f"# Time Entries — Ticket #{ticket_id}",
        "",
        f"**Total Hours:** {total_hours:.2f}",
        "",
        "**By Tech:**",
    ]
    for tech, hrs in sorted(by_tech.items(), key=lambda x: -x[1]):
        out.append(f"- {tech}: {hrs:.2f} hrs")

    out.extend(["", "## Entries", "", "| Date | Tech | Hours | Billable | Notes |"])
    out.append("|------|------|-------|----------|-------|")
    for e in sorted(entries, key=lambda x: x.get("timeStart", "")):
        tech = (e.get("member") or {}).get("identifier", "—")
        out.append(
            f"| {_date(e.get('timeStart'))} "
            f"| {tech} "
            f"| {e.get('actualHours', 0):.2f} "
            f"| {e.get('billableOption', '—')} "
            f"| {_trunc(e.get('notes', ''), 80)} |"
        )

    return "\n".join(out)


@mcp.tool()
def search_cw_kb_articles(query: str = "", board_name: str = "", limit: int = 10) -> str:
    """
    Search ConnectWise internal knowledge base articles.

    Use when: looking for previously documented internal solutions in CW.
    Don't use when: searching vendor documentation — use a dedicated knowledge base tool instead.

    Args:
        query: Text to search in question or answer fields
        board_name: Filter to a specific board's KB (optional)
        limit: Max results to return (default 10)
    """
    parts = []
    if query:
        parts.append(f'question contains "{query}" or answer contains "{query}"')
    if board_name:
        parts.append(f'board/name="{board_name}"')

    conditions = _build_conditions(parts) or None
    articles = cw_paginate(
        "/service/knowledgeBaseArticles",
        conditions=conditions,
        fields="id,question,answer,dateCreated,board",
    )

    if not articles:
        return "No KB articles found."

    page = articles[:limit]
    out = [f"Found {len(articles)} KB article(s) (showing {len(page)}):", ""]
    for a in page:
        board_info = (a.get("board") or {}).get("name", "—")
        out.append(f"**[{a.get('id')}] {_trunc(a.get('question', '—'), 100)}**")
        out.append(f"Board: {board_info} | Created: {_date(a.get('dateCreated'))}")
        if a.get("answer"):
            out.append(f"> {_trunc(a['answer'], 300)}")
        out.append("")

    return "\n".join(out)


@mcp.tool()
def get_ticket_count(board: str = "", status: str = "", company_name: str = "") -> str:
    """
    Get a count of tickets matching the given filters (faster than fetching full list).

    Args:
        board: Filter by board name (optional)
        status: Filter by status name (optional)
        company_name: Filter by company name (optional)
    """
    parts = []
    if board:
        parts.append(f'board/name="{board}"')
    if status:
        parts.append(f'status/name="{status}"')
    if company_name:
        parts.append(f'company/name contains "{company_name}"')

    conditions = _build_conditions(parts) or None
    params: dict = {}
    if conditions:
        params["conditions"] = conditions

    result = cw_get("/service/tickets/count", params if params else None)
    if isinstance(result, dict):
        count = result.get("count", 0)
    elif isinstance(result, int):
        count = result
    else:
        count = 0

    filter_desc = []
    if board:
        filter_desc.append(f"board={board}")
    if status:
        filter_desc.append(f"status={status}")
    if company_name:
        filter_desc.append(f"company={company_name}")

    filter_str = ", ".join(filter_desc) if filter_desc else "all tickets"
    return f"Ticket count ({filter_str}): **{count}**"


# ══════════════════════════════════════════════════════════════════════════════
# OPERATIONS DOMAIN — Companies, contacts, configurations, projects, time
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_company(company_id: int = 0, company_name: str = "") -> str:
    """
    Get a ConnectWise company by ID or name search.

    Use when: looking up a client's CW company record, ID, or contact info.

    Provide either company_id (exact) or company_name (partial match).
    """
    company_name = _safe_str(company_name)
    if company_id:
        company = cw_get(f"/company/companies/{company_id}")
        if not company or isinstance(company, list):
            return f"Company {company_id} not found."
        companies = [company]
    elif company_name:
        companies = cw_paginate(
            "/company/companies",
            conditions=f'name contains "{company_name}"',
            fields="id,name,status,phoneNumber,website,addressLine1,city,state,zip,type,dateCreated",
        )
        if not companies:
            return f"No companies found matching '{company_name}'."
    else:
        return "Provide either company_id or company_name."

    out = [f"Found {len(companies)} company record(s):", ""]
    for c in companies:
        status = _n(c, "status", "name")
        addr_parts = [c.get("addressLine1"), c.get("city"), c.get("state"), c.get("zip")]
        address = ", ".join(p for p in addr_parts if p) or "—"
        out.extend([
            f"**{c.get('name', '—')}** (ID: {c.get('id')})",
            f"Status: {status} | Type: {_n(c, 'type', 'name')}",
            f"Phone: {c.get('phoneNumber', '—')} | Website: {c.get('website', '—')}",
            f"Address: {address}",
            f"Created: {_date(c.get('dateCreated'))}",
            "",
        ])

    return "\n".join(out)


@mcp.tool()
def search_companies(
    status: str = "Active",
    company_type: str = "Client",
    limit: int = 50,
    offset: int = 0,
) -> str:
    """
    List ConnectWise companies with optional status and type filters.

    Defaults to Active Clients. Pass status="" or company_type="" to remove that filter.
    Returns: ID, name, status, phone, website, territory.
    """
    status = _safe_str(status)
    company_type = _safe_str(company_type)
    parts = []
    if status:
        parts.append(f'status/name="{status}"')
    if company_type:
        parts.append(f'type/name="{company_type}"')

    conditions = _build_conditions(parts) or None
    fields = "id,name,status,phoneNumber,website,territory"

    all_results = cw_paginate("/company/companies", conditions=conditions, fields=fields)
    total = len(all_results)
    page = all_results[offset: offset + limit]

    if not page:
        return "No companies found."

    out = [f"Found {total} company record(s):", "", "| ID | Name | Status | Phone | Territory |"]
    out.append("|----|------|--------|-------|-----------|")
    for c in page:
        out.append(
            f"| {c.get('id')} "
            f"| {_trunc(c.get('name', ''), 40)} "
            f"| {_n(c, 'status', 'name')} "
            f"| {c.get('phoneNumber', '—')} "
            f"| {_n(c, 'territory', 'name')} |"
        )

    out.append(_pagination_footer(total, limit, offset))
    return "\n".join(out)


@mcp.tool()
def get_contacts(
    company_id: int = 0,
    company_name: str = "",
    limit: int = 25,
    offset: int = 0,
) -> str:
    """
    Get contacts for a company. Provide either company_id or company_name (partial match).
    Returns: name, title, email, phone, default contact flag.
    """
    company_name = _safe_str(company_name)
    parts = []
    if company_id:
        parts.append(f"company/id={company_id}")
    elif company_name:
        parts.append(f'company/name contains "{company_name}"')
    else:
        return "Provide either company_id or company_name."

    conditions = _build_conditions(parts)
    fields = "id,firstName,lastName,title,email,mobileGuid,company,defaultFlag,communicationItems"

    all_results = cw_paginate("/company/contacts", conditions=conditions, fields=fields)
    total = len(all_results)
    page = all_results[offset: offset + limit]

    if not page:
        return "No contacts found."

    out = [f"Found {total} contact(s):", "", "| ID | Name | Title | Email | Default |"]
    out.append("|----|------|-------|-------|---------|")
    for c in page:
        name = f"{c.get('firstName', '')} {c.get('lastName', '')}".strip() or "—"
        out.append(
            f"| {c.get('id')} "
            f"| {name} "
            f"| {_trunc(c.get('title', '—'), 30)} "
            f"| {c.get('email', '—')} "
            f"| {'Yes' if c.get('defaultFlag') else 'No'} |"
        )

    out.append(_pagination_footer(total, limit, offset))
    return "\n".join(out)


@mcp.tool()
def get_configurations(
    company_id: int = 0,
    company_name: str = "",
    config_type: str = "",
    status: str = "Active",
    limit: int = 50,
    offset: int = 0,
) -> str:
    """
    List client device/asset configurations from ConnectWise (live, not embedded).

    Use when: need current device list for an onsite or audit — live data from CW.
    """
    company_name = _safe_str(company_name)
    config_type = _safe_str(config_type)
    status = _safe_str(status)
    parts = []
    if company_id:
        parts.append(f"company/id={company_id}")
    elif company_name:
        parts.append(f'company/name contains "{company_name}"')
    if config_type:
        parts.append(f'type/name="{config_type}"')
    if status:
        parts.append(f'status/name="{status}"')

    conditions = _build_conditions(parts) or None
    fields = "id,name,type,status,company,ipAddress,macAddress,serialNumber,modelNumber,osType,lastLoginName"

    all_results = cw_paginate("/company/configurations", conditions=conditions, fields=fields)
    total = len(all_results)
    page = all_results[offset: offset + limit]

    if not page:
        return "No configurations found."

    out = [
        f"Found {total} configuration(s):",
        "",
        "| ID | Name | Type | Company | IP | Serial | OS |",
        "|----|------|------|---------|----|--------|----|",
    ]
    for c in page:
        out.append(
            f"| {c.get('id')} "
            f"| {_trunc(c.get('name', ''), 35)} "
            f"| {_n(c, 'type', 'name')} "
            f"| {_n(c, 'company', 'name')} "
            f"| {c.get('ipAddress', '—')} "
            f"| {c.get('serialNumber', '—')} "
            f"| {_trunc(c.get('osType', '—'), 20)} |"
        )

    out.append(_pagination_footer(total, limit, offset))
    return "\n".join(out)


@mcp.tool()
def get_configuration(config_id: int) -> str:
    """
    Get full detail for a single configuration/asset record.

    Args:
        config_id: The CW configuration ID
    """
    config = cw_get(f"/company/configurations/{config_id}")
    if not config or isinstance(config, list):
        return f"Configuration {config_id} not found."

    lines = [
        f"# Configuration #{config.get('id')} — {config.get('name', '—')}",
        "",
        f"**Company:** {_n(config, 'company', 'name')}",
        f"**Type:** {_n(config, 'type', 'name')}",
        f"**Status:** {_n(config, 'status', 'name')}",
        f"**IP Address:** {config.get('ipAddress', '—')}",
        f"**MAC Address:** {config.get('macAddress', '—')}",
        f"**Serial Number:** {config.get('serialNumber', '—')}",
        f"**Model Number:** {config.get('modelNumber', '—')}",
        f"**OS Type:** {config.get('osType', '—')}",
        f"**Last Login:** {config.get('lastLoginName', '—')}",
        f"**Date Added:** {_date(config.get('addedDate') or config.get('dateCreated'))}",
    ]

    questions = config.get("questions") or []
    if questions:
        lines.extend(["", "## Custom Fields"])
        for q in questions:
            lines.append(f"- **{q.get('question', {}).get('question', '?')}:** {q.get('answer', '—')}")

    return "\n".join(lines)


@mcp.tool()
def get_projects(
    company_id: int = 0,
    company_name: str = "",
    status: str = "",
    limit: int = 25,
    offset: int = 0,
) -> str:
    """
    List ConnectWise projects, optionally filtered by company or status.

    Returns: ID, name, status, company, manager, budget, actual hours, % complete.
    """
    parts = []
    if company_id:
        parts.append(f"company/id={company_id}")
    elif company_name:
        parts.append(f'company/name contains "{company_name}"')
    if status:
        parts.append(f'status/name="{status}"')

    conditions = _build_conditions(parts) or None
    fields = "id,name,status,company,manager,budget,actualHours,percentComplete"

    all_results = cw_paginate("/project/projects", conditions=conditions, fields=fields)
    total = len(all_results)
    page = all_results[offset: offset + limit]

    if not page:
        return "No projects found."

    out = [
        f"Found {total} project(s):",
        "",
        "| ID | Name | Status | Company | Manager | Budget | Actual Hrs | % Done |",
        "|----|------|--------|---------|---------|--------|------------|--------|",
    ]
    for p in page:
        budget = f"${float(p.get('budget') or 0):,.2f}" if p.get("budget") else "—"
        actual = f"{p.get('actualHours', 0):.1f}" if p.get("actualHours") is not None else "—"
        pct = f"{p.get('percentComplete', 0)}%" if p.get("percentComplete") is not None else "—"
        out.append(
            f"| {p.get('id')} "
            f"| {_trunc(p.get('name', ''), 35)} "
            f"| {_n(p, 'status', 'name')} "
            f"| {_n(p, 'company', 'name')} "
            f"| {_n(p, 'manager', 'identifier')} "
            f"| {budget} "
            f"| {actual} "
            f"| {pct} |"
        )

    out.append(_pagination_footer(total, limit, offset))
    return "\n".join(out)


@mcp.tool()
def get_project_tickets(project_id: int, status: str = "", limit: int = 50) -> str:
    """
    List tickets associated with a project.

    Args:
        project_id: The CW project ID
        status: Filter by status name (optional)
        limit: Max results to return (default 50)
    """
    parts = [f"project/id={project_id}"]
    if status:
        parts.append(f'status/name="{status}"')

    conditions = _build_conditions(parts)
    fields = "id,summary,status,assignedTo,priority,dateEntered"

    tickets = cw_paginate("/project/tickets", conditions=conditions, fields=fields)
    if not tickets:
        return f"No tickets found for project {project_id}."

    page = tickets[:limit]
    out = [f"Found {len(tickets)} project ticket(s) (showing {len(page)}):", "", "| # | Summary | Status | Tech | Priority | Date |"]
    out.append("|---|---------|--------|------|----------|------|")
    for t in page:
        out.append(
            f"| #{t.get('id')} "
            f"| {_trunc(t.get('summary', ''), 50)} "
            f"| {_n(t, 'status', 'name')} "
            f"| {_n(t, 'assignedTo', 'identifier')} "
            f"| {_n(t, 'priority', 'name')} "
            f"| {_date(t.get('dateEntered'))} |"
        )

    return "\n".join(out)


@mcp.tool()
def get_time_entries(
    tech_identifier: str = "",
    company_name: str = "",
    date_from: str = "",
    date_to: str = "",
    billable_option: str = "",
    limit: int = 50,
    offset: int = 0,
) -> str:
    """
    Get time entries with optional filters (live from CW).

    Use when: you need live/current time entry data from CW.

    Args:
        tech_identifier: Filter by tech's CW identifier/username (optional)
        company_name: Filter by client company name (optional)
        date_from: Start date YYYY-MM-DD (optional)
        date_to: End date YYYY-MM-DD (optional)
        billable_option: "Billable", "DoNotBill", or "NoCharge" (optional)
        limit: Max results (default 50)
    """
    parts = []
    if tech_identifier:
        parts.append(f'member/identifier="{tech_identifier}"')
    if company_name:
        parts.append(f'company/name contains "{company_name}"')
    if date_from:
        parts.append(f"timeStart>=[{date_from}T00:00:00Z]")
    if date_to:
        parts.append(f"timeStart<=[{date_to}T23:59:59Z]")
    if billable_option:
        parts.append(f'billableOption="{billable_option}"')

    conditions = _build_conditions(parts) or None
    fields = "id,member,company,chargeToType,chargeToId,timeStart,actualHours,billableOption,workType,notes"

    all_results = cw_paginate("/time/entries", conditions=conditions, fields=fields, order_by="timeStart desc")
    total = len(all_results)
    total_hours = sum(e.get("actualHours", 0) or 0 for e in all_results)
    page = all_results[offset: offset + limit]

    if not page:
        return "No time entries found."

    out = [
        f"Found {total} time entry/entries — Total: {total_hours:.2f} hrs",
        "",
        "| Date | Tech | Company | Charge To | Hours | Billable | Notes |",
        "|------|------|---------|-----------|-------|----------|-------|",
    ]
    for e in page:
        charge = f"{e.get('chargeToType', '—')} #{e.get('chargeToId', '?')}"
        out.append(
            f"| {_date(e.get('timeStart'))} "
            f"| {_n(e, 'member', 'identifier')} "
            f"| {_n(e, 'company', 'name')} "
            f"| {charge} "
            f"| {e.get('actualHours', 0):.2f} "
            f"| {e.get('billableOption', '—')} "
            f"| {_trunc(e.get('notes', ''), 60)} |"
        )

    out.append(_pagination_footer(total, limit, offset))
    return "\n".join(out)


@mcp.tool()
def log_time(
    charge_to_id: int,
    charge_to_type: str,
    member_id: int,
    hours: float,
    notes: str,
    billable_option: str = "Billable",
    work_type_name: str = "",
    time_start: str = "",
) -> str:
    """
    Log a time entry in ConnectWise.

    Args:
        charge_to_id: ID of ticket or project to charge time against
        charge_to_type: "ServiceTicket", "ProjectTicket", or "ChargeCode"
        member_id: CW member ID of the tech logging time (use get_members to find)
        hours: Actual hours worked (e.g. 1.5)
        notes: Work description/notes
        billable_option: "Billable", "DoNotBill", or "NoCharge" (default: Billable)
        work_type_name: Work type name (optional)
        time_start: ISO datetime string e.g. "2025-03-01T09:00:00Z" (optional, defaults to now)
    """
    body: dict = {
        "chargeToId": charge_to_id,
        "chargeToType": charge_to_type,
        "member": {"id": member_id},
        "actualHours": hours,
        "notes": notes,
        "billableOption": billable_option,
    }
    if work_type_name:
        body["workType"] = {"name": work_type_name}
    if time_start:
        body["timeStart"] = time_start
    else:
        body["timeStart"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        result = cw_post("/time/entries", body)
        entry_id = result.get("id", "?")
        return (
            f"Time entry #{entry_id} logged: {hours:.2f} hrs on {charge_to_type} #{charge_to_id}\n"
            f"Billable: {billable_option} | Notes: {_trunc(notes, 100)}"
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("Failed to log time: %s", e, exc_info=True)
        return "Failed to log time entry. Check server logs for details."


@mcp.tool()
def get_members(include_inactive: bool = False) -> str:
    """
    List ConnectWise team members/technicians.

    Use when: looking up a tech's CW identifier or member ID for other tool calls.

    Args:
        include_inactive: Include disabled/inactive members (default False)
    """
    conditions = None if include_inactive else "inactiveFlag=false"
    fields = "id,identifier,firstName,lastName,title,primaryEmail,systemMember,disableLoginFlag"

    members = cw_paginate("/system/members", conditions=conditions, fields=fields)
    if not members:
        return "No members found."

    out = [f"Found {len(members)} member(s):", "", "| ID | Username | Name | Title | Email |"]
    out.append("|----|----------|------|-------|-------|")
    for m in sorted(members, key=lambda x: x.get("identifier", "")):
        name = f"{m.get('firstName', '')} {m.get('lastName', '')}".strip() or "—"
        out.append(
            f"| {m.get('id')} "
            f"| {m.get('identifier', '—')} "
            f"| {name} "
            f"| {_trunc(m.get('title', '—'), 30)} "
            f"| {m.get('primaryEmail', '—')} |"
        )

    return "\n".join(out)


# ══════════════════════════════════════════════════════════════════════════════
# FINANCE DOMAIN — Invoices, agreements, MRR, opportunities
# Only registered when CW_TIER=leadership
# ══════════════════════════════════════════════════════════════════════════════

if FINANCE_ENABLED:

    @mcp.tool()
    def get_invoices(
        company_id: int = 0,
        company_name: str = "",
        status: str = "",
        date_from: str = "",
        date_to: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """
        List ConnectWise invoices with optional filters (live from CW).

        Use when: checking current invoice/billing status, outstanding balances.

        Args:
            company_id: Filter by company ID (optional)
            company_name: Filter by company name (partial match, optional)
            status: Invoice status e.g. "Open", "Paid", "Closed" (optional)
            date_from: Start date YYYY-MM-DD (optional)
            date_to: End date YYYY-MM-DD (optional)
        """
        company_name = _safe_str(company_name)
        status = _safe_str(status)
        date_from = _safe_str(date_from, max_len=10)
        date_to = _safe_str(date_to, max_len=10)
        parts = []
        if company_id:
            parts.append(f"company/id={company_id}")
        elif company_name:
            parts.append(f'company/name contains "{company_name}"')
        if status:
            parts.append(f'status/name="{status}"')
        if date_from:
            parts.append(f"date>=[{date_from}T00:00:00Z]")
        if date_to:
            parts.append(f"date<=[{date_to}T23:59:59Z]")

        conditions = _build_conditions(parts) or None
        fields = "id,invoiceNumber,company,status,date,dueDate,total,balance,invoiceType"

        all_results = cw_paginate("/finance/invoices", conditions=conditions, fields=fields, order_by="date desc")
        total = len(all_results)
        page = all_results[offset: offset + limit]

        if not page:
            return "No invoices found."

        total_balance = sum(float(i.get("balance") or 0) for i in all_results)
        out = [
            f"Found {total} invoice(s) — Outstanding balance: {_dollar(total_balance)}",
            "",
            "| Invoice # | Company | Status | Date | Due | Total | Balance |",
            "|-----------|---------|--------|------|-----|-------|---------|",
        ]
        for i in page:
            out.append(
                f"| {i.get('invoiceNumber', '—')} "
                f"| {_trunc(_n(i, 'company', 'name'), 30)} "
                f"| {_n(i, 'status', 'name')} "
                f"| {_date(i.get('date'))} "
                f"| {_date(i.get('dueDate'))} "
                f"| {_dollar(i.get('total'))} "
                f"| {_dollar(i.get('balance'))} |"
            )

        out.append(_pagination_footer(total, limit, offset))
        return "\n".join(out)

    @mcp.tool()
    def get_invoice(invoice_id: int) -> str:
        """
        Get full detail for a single invoice, including line items.

        Args:
            invoice_id: The CW invoice ID
        """
        invoice = cw_get(f"/finance/invoices/{invoice_id}")
        if not invoice or isinstance(invoice, list):
            return f"Invoice {invoice_id} not found."

        lines = [
            f"# Invoice #{invoice.get('invoiceNumber', invoice_id)}",
            "",
            f"**Company:** {_n(invoice, 'company', 'name')}",
            f"**Status:** {_n(invoice, 'status', 'name')}",
            f"**Type:** {invoice.get('invoiceType', '—')}",
            f"**Date:** {_date(invoice.get('date'))}",
            f"**Due Date:** {_date(invoice.get('dueDate'))}",
            f"**Total:** {_dollar(invoice.get('total'))}",
            f"**Balance:** {_dollar(invoice.get('balance'))}",
            f"**Paid Date:** {_date(invoice.get('paidDate'))}",
        ]

        line_items = invoice.get("invoiceProductsXref") or invoice.get("products") or []
        if line_items and isinstance(line_items, list):
            lines.extend(["", f"## Line Items ({len(line_items)})", "", "| Description | Qty | Unit Price | Total |"])
            lines.append("|-------------|-----|------------|-------|")
            for item in line_items:
                desc = _trunc(item.get("description") or _n(item, "product", "description") or "—", 50)
                qty = item.get("quantity", "—")
                unit = _dollar(item.get("unitPrice"))
                total_item = _dollar(item.get("total") or (
                    float(item.get("quantity") or 0) * float(item.get("unitPrice") or 0)
                ))
                lines.append(f"| {desc} | {qty} | {unit} | {total_item} |")

        return "\n".join(lines)

    @mcp.tool()
    def get_agreements(
        company_id: int = 0,
        company_name: str = "",
        status: str = "Active",
        agreement_type: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """
        List ConnectWise agreements (managed service contracts) live from CW.

        Use for: current agreement status, billing amounts, renewal flags.

        Defaults to Active agreements.
        """
        parts = []
        if company_id:
            parts.append(f"company/id={company_id}")
        elif company_name:
            parts.append(f'company/name contains "{company_name}"')
        if status:
            parts.append(f'status/name="{status}"')
        if agreement_type:
            parts.append(f'type/name="{agreement_type}"')

        conditions = _build_conditions(parts) or None
        fields = "id,name,type,status,company,startDate,endDate,billAmount,periodType,cancelledFlag"

        all_results = cw_paginate("/finance/agreements", conditions=conditions, fields=fields)
        total = len(all_results)
        page = all_results[offset: offset + limit]

        if not page:
            return "No agreements found."

        total_mrr = sum(float(a.get("billAmount") or 0) for a in all_results)
        out = [
            f"Found {total} agreement(s) — Total bill amount: {_dollar(total_mrr)}/period",
            "",
            "| ID | Name | Type | Company | Status | Start | End | Bill Amt |",
            "|----|------|------|---------|--------|-------|-----|----------|",
        ]
        for a in page:
            cancelled = " (Cancelled)" if a.get("cancelledFlag") else ""
            out.append(
                f"| {a.get('id')} "
                f"| {_trunc(a.get('name', ''), 35)} "
                f"| {_n(a, 'type', 'name')} "
                f"| {_trunc(_n(a, 'company', 'name'), 25)} "
                f"| {_n(a, 'status', 'name')}{cancelled} "
                f"| {_date(a.get('startDate'))} "
                f"| {_date(a.get('endDate'))} "
                f"| {_dollar(a.get('billAmount'))} |"
            )

        out.append(_pagination_footer(total, limit, offset))
        return "\n".join(out)

    @mcp.tool()
    def get_agreement(agreement_id: int) -> str:
        """
        Get full detail for a single agreement (live from CW).

        Args:
            agreement_id: The CW agreement ID
        """
        agreement = cw_get(f"/finance/agreements/{agreement_id}")
        if not agreement or isinstance(agreement, list):
            return f"Agreement {agreement_id} not found."

        lines = [
            f"# Agreement #{agreement.get('id')} — {agreement.get('name', '—')}",
            "",
            f"**Company:** {_n(agreement, 'company', 'name')}",
            f"**Type:** {_n(agreement, 'type', 'name')}",
            f"**Status:** {_n(agreement, 'status', 'name')}",
            f"**Period Type:** {agreement.get('periodType', '—')}",
            f"**Bill Amount:** {_dollar(agreement.get('billAmount'))}",
            f"**Start Date:** {_date(agreement.get('startDate'))}",
            f"**End Date:** {_date(agreement.get('endDate'))}",
            f"**Cancelled:** {'Yes' if agreement.get('cancelledFlag') else 'No'}",
            f"**SLA:** {_n(agreement, 'sla', 'name')}",
        ]

        return "\n".join(lines)

    @mcp.tool()
    def get_agreement_additions(agreement_id: int, include_cancelled: bool = False) -> str:
        """
        List line items (additions) on an agreement (live from CW).

        Args:
            agreement_id: The CW agreement ID
            include_cancelled: Include cancelled line items (default False)
        """
        conditions = None if include_cancelled else "cancelledDate=null"
        fields = "id,product,quantity,unitPrice,unitCost,billCustomer,effectiveDate,cancelledDate,description"

        additions = cw_paginate(
            f"/finance/agreements/{agreement_id}/additions",
            conditions=conditions,
            fields=fields,
        )

        if not additions:
            return f"No additions found for agreement {agreement_id}."

        billable = [a for a in additions if a.get("billCustomer")]
        total_value = sum(
            float(a.get("quantity") or 0) * float(a.get("unitPrice") or 0)
            for a in billable
        )

        out = [
            f"Agreement #{agreement_id} — {len(additions)} addition(s), billable total: {_dollar(total_value)}/period",
            "",
            "| Product | Qty | Unit Price | Bill? | Effective | Cancelled |",
            "|---------|-----|------------|-------|-----------|-----------|",
        ]
        for a in additions:
            product_id = _n(a, "product", "identifier") or _trunc(a.get("description", "—"), 30)
            out.append(
                f"| {_trunc(product_id, 35)} "
                f"| {a.get('quantity', '—')} "
                f"| {_dollar(a.get('unitPrice'))} "
                f"| {'Yes' if a.get('billCustomer') else 'No'} "
                f"| {_date(a.get('effectiveDate'))} "
                f"| {_date(a.get('cancelledDate'))} |"
            )

        return "\n".join(out)

    @mcp.tool()
    def get_client_mrr(company_name: str) -> str:
        """
        Calculate live MRR (Monthly Recurring Revenue) for a client from CW.

        Fetches all active agreements and additions, computes MRR from
        billable non-cancelled non-junk line items.

        Use for: pricing conversations, renewal planning, commission calculations.

        Args:
            company_name: Client company name (partial match)
        """
        agreements = cw_paginate(
            "/finance/agreements",
            conditions=f'company/name contains "{company_name}" and status/name="Active"',
            fields="id,name,type,billAmount,periodType,cancelledFlag",
        )

        if not agreements:
            return f"No active agreements found for '{company_name}'."

        out = [f"# MRR — {company_name}", ""]
        grand_total = 0.0

        for agreement in agreements:
            if agreement.get("cancelledFlag"):
                continue

            agr_id = agreement.get("id")
            agr_name = agreement.get("name", "—")
            agr_type = _n(agreement, "type", "name")

            additions = cw_paginate(
                f"/finance/agreements/{agr_id}/additions",
                conditions="cancelledDate=null",
                fields="id,product,quantity,unitPrice,billCustomer,description",
            )

            mrr_additions = [a for a in additions if a.get("billCustomer") and _is_mrr(a)]
            agr_mrr = sum(
                float(a.get("quantity") or 0) * float(a.get("unitPrice") or 0)
                for a in mrr_additions
            )
            grand_total += agr_mrr

            out.append(f"## {agr_name} ({agr_type})")
            out.append(f"Agreement MRR: {_dollar(agr_mrr)}")

            if mrr_additions:
                out.append("")
                out.append("| Product | Qty | Unit Price | Line Total |")
                out.append("|---------|-----|------------|------------|")
                for a in mrr_additions:
                    product_id = _n(a, "product", "identifier") or _trunc(a.get("description", "—"), 30)
                    qty = float(a.get("quantity") or 0)
                    unit = float(a.get("unitPrice") or 0)
                    line_total = qty * unit
                    out.append(f"| {_trunc(product_id, 35)} | {qty:.0f} | {_dollar(unit)} | {_dollar(line_total)} |")

            out.append("")

        out.extend([
            "---",
            f"**Total MRR for {company_name}: {_dollar(grand_total)}/month**",
            f"**ARR Estimate: {_dollar(grand_total * 12)}/year**",
        ])

        return "\n".join(out)

    @mcp.tool()
    def get_opportunities(
        company_id: int = 0,
        company_name: str = "",
        status: str = "",
        sales_rep: str = "",
        date_from: str = "",
        date_to: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """
        List sales opportunities from ConnectWise (live).

        Use for: pipeline visibility, deal status, revenue forecasting.
        """
        parts = []
        if company_id:
            parts.append(f"company/id={company_id}")
        elif company_name:
            parts.append(f'company/name contains "{company_name}"')
        if status:
            parts.append(f'status/name="{status}"')
        if sales_rep:
            parts.append(f'salesRep/identifier="{sales_rep}"')
        if date_from:
            parts.append(f"closedDate>=[{date_from}T00:00:00Z]")
        if date_to:
            parts.append(f"closedDate<=[{date_to}T23:59:59Z]")

        conditions = _build_conditions(parts) or None
        fields = "id,name,company,status,stage,probability,expectedRevenue,salesRep,closedDate"

        all_results = cw_paginate("/sales/opportunities", conditions=conditions, fields=fields, order_by="closedDate desc")
        total = len(all_results)
        page = all_results[offset: offset + limit]

        if not page:
            return "No opportunities found."

        total_pipeline = sum(float(o.get("expectedRevenue") or 0) for o in all_results)
        out = [
            f"Found {total} opportunity/ies — Pipeline: {_dollar(total_pipeline)}",
            "",
            "| ID | Name | Company | Status | Stage | Prob% | Revenue | Rep | Close |",
            "|----|------|---------|--------|-------|-------|---------|-----|-------|",
        ]
        for o in page:
            out.append(
                f"| {o.get('id')} "
                f"| {_trunc(o.get('name', ''), 35)} "
                f"| {_trunc(_n(o, 'company', 'name'), 25)} "
                f"| {_n(o, 'status', 'name')} "
                f"| {_n(o, 'stage', 'name')} "
                f"| {o.get('probability', '—')}% "
                f"| {_dollar(o.get('expectedRevenue'))} "
                f"| {_n(o, 'salesRep', 'identifier')} "
                f"| {_date(o.get('closedDate'))} |"
            )

        out.append(_pagination_footer(total, limit, offset))
        return "\n".join(out)

    @mcp.tool()
    def create_opportunity(
        company_id: int,
        name: str,
        expected_revenue: float,
        close_date: str,
        probability: int = 50,
        sales_rep_identifier: str = "",
        notes: str = "",
    ) -> str:
        """
        Create a new sales opportunity in ConnectWise.

        Args:
            company_id: CW company ID to link the opportunity to
            name: Opportunity name/title
            expected_revenue: Expected revenue amount (dollars)
            close_date: Expected close date YYYY-MM-DD
            probability: Probability % (0-100, default 50)
            sales_rep_identifier: Sales rep's CW identifier (optional)
            notes: Additional notes (optional)
        """
        body: dict = {
            "name": name,
            "company": {"id": company_id},
            "expectedRevenue": expected_revenue,
            "closedDate": f"{close_date}T00:00:00Z",
            "probability": probability,
        }
        if sales_rep_identifier:
            body["salesRep"] = {"identifier": sales_rep_identifier}
        if notes:
            body["notes"] = notes

        try:
            result = cw_post("/sales/opportunities", body)
            opp_id = result.get("id", "?")
            return (
                f"Opportunity #{opp_id} created: {name}\n"
                f"Revenue: {_dollar(expected_revenue)} | Probability: {probability}% | Close: {close_date}"
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).error("Failed to create opportunity: %s", e, exc_info=True)
            return "Failed to create opportunity. Check server logs for details."

    @mcp.tool()
    def get_agreement_count_by_type() -> str:
        """
        Get a summary of active agreements grouped by type, with counts and total bill amounts.
        Use for MRR/ARR portfolio overview.
        """
        agreements = cw_paginate(
            "/finance/agreements",
            conditions='status/name="Active"',
            fields="id,type,billAmount,cancelledFlag",
        )

        if not agreements:
            return "No active agreements found."

        by_type: dict[str, dict] = {}
        for a in agreements:
            if a.get("cancelledFlag"):
                continue
            type_name = _n(a, "type", "name")
            if type_name not in by_type:
                by_type[type_name] = {"count": 0, "total": 0.0}
            by_type[type_name]["count"] += 1
            by_type[type_name]["total"] += float(a.get("billAmount") or 0)

        total_count = sum(v["count"] for v in by_type.values())
        total_mrr = sum(v["total"] for v in by_type.values())

        out = [
            "# Active Agreements by Type",
            f"Total: {total_count} agreements — {_dollar(total_mrr)}/period",
            "",
            "| Agreement Type | Count | Bill Amount/Period |",
            "|---------------|-------|-------------------|",
        ]
        for type_name, data in sorted(by_type.items(), key=lambda x: -x[1]["total"]):
            out.append(f"| {type_name} | {data['count']} | {_dollar(data['total'])} |")

        out.extend([
            "",
            f"**Grand Total: {_dollar(total_mrr)}/month | {_dollar(total_mrr * 12)}/year (ARR)**",
        ])

        return "\n".join(out)

    @mcp.tool()
    def get_aging_invoices(days_overdue: int = 30, company_name: str = "") -> str:
        """
        List overdue invoices with outstanding balances.

        Use for: collections, AR reviews, identifying clients with unpaid balances.

        Args:
            days_overdue: Minimum days past due to include (default 30)
            company_name: Filter to a specific company (optional)
        """
        parts = ["balance>0"]
        if company_name:
            parts.append(f'company/name contains "{company_name}"')

        conditions = _build_conditions(parts)
        fields = "id,invoiceNumber,company,status,date,dueDate,total,balance,invoiceType"

        all_invoices = cw_paginate("/finance/invoices", conditions=conditions, fields=fields)
        if not all_invoices:
            return "No open invoices with outstanding balances found."

        today = date.today()
        aging = []
        for inv in all_invoices:
            due_str = inv.get("dueDate")
            if not due_str:
                continue
            try:
                due_dt = datetime.strptime(due_str[:10], "%Y-%m-%d").date()
                days_past = (today - due_dt).days
                if days_past >= days_overdue:
                    aging.append({**inv, "_days_past": days_past})
            except ValueError:
                continue

        if not aging:
            return f"No invoices found that are {days_overdue}+ days overdue."

        aging.sort(key=lambda x: -x["_days_past"])
        total_balance = sum(float(i.get("balance") or 0) for i in aging)

        out = [
            f"# Aging Invoices — {days_overdue}+ days overdue",
            f"Found {len(aging)} invoice(s) — Total outstanding: {_dollar(total_balance)}",
            "",
            "| Invoice # | Company | Due Date | Days Past Due | Total | Balance |",
            "|-----------|---------|----------|---------------|-------|---------|",
        ]
        for inv in aging:
            out.append(
                f"| {inv.get('invoiceNumber', '—')} "
                f"| {_trunc(_n(inv, 'company', 'name'), 30)} "
                f"| {_date(inv.get('dueDate'))} "
                f"| **{inv['_days_past']}** "
                f"| {_dollar(inv.get('total'))} "
                f"| {_dollar(inv.get('balance'))} |"
            )

        return "\n".join(out)


# ══════════════════════════════════════════════════════════════════════════════
# Health check
# ══════════════════════════════════════════════════════════════════════════════

@mcp.custom_route("/health", methods=["GET"])
async def _health(request: StarletteRequest) -> JSONResponse:
    tier_info = f"tier={CW_TIER}, finance={'enabled' if FINANCE_ENABLED else 'disabled'}"
    return JSONResponse({"status": "healthy", "service": "cw-live", "config": tier_info})


if __name__ == "__main__":
    port = int(os.getenv("CW_LIVE_MCP_PORT", "8085"))
    mcp.run(transport="http", host="0.0.0.0", port=port)
