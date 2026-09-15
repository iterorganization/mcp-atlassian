"""Confluence FastMCP server instance and tool definitions."""

import json
import logging
import re
from typing import Annotated

from fastmcp import Context, FastMCP
from pydantic import BeforeValidator, Field

from mcp_atlassian.exceptions import MCPAtlassianAuthenticationError
from mcp_atlassian.servers.dependencies import get_confluence_fetcher
from mcp_atlassian.utils.decorators import (
    check_write_access,
)

logger = logging.getLogger(__name__)

confluence_mcp = FastMCP(
    name="Confluence MCP Service",
    instructions="Provides tools for interacting with Atlassian Confluence.",
)


def _format_search_query(
    query: str, *, use_site_search: bool = True
) -> str:
    """Convert natural language query to CQL, handling logical operators.

    Args:
        query: The search query string
        use_site_search: If True, use siteSearch; if False, use text

    Returns:
        A valid CQL query string

    Examples:
        "project documentation" -> 'siteSearch ~ "project documentation"'
        "telework OR remote work" ->
            'siteSearch ~ "telework" OR siteSearch ~ "remote work"'
        "devops AND kubernetes" ->
            'siteSearch ~ "devops" AND siteSearch ~ "kubernetes"'
        "text ~ \"foo\" OR type=page" ->
            "text ~ \"foo\" OR type=page" (already CQL)
    """
    # Check if query already contains CQL field operators
    has_field_operators = any(
        op in query for op in ["=", "~", ">", "<", "currentUser()"]
    )

    if has_field_operators:
        # Already valid CQL, return as-is
        logger.debug(f"Query appears to be valid CQL already: {query}")
        return query

    # Define the field operator to use
    field_operator = "siteSearch" if use_site_search else "text"

    # Check if query contains CQL logical operators (uppercase with spaces)
    # Pattern matches " AND " or " OR " with word boundaries
    logical_operator_pattern = r"\s+(AND|OR)\s+"

    if re.search(logical_operator_pattern, query):
        # Split by logical operators while preserving the operators
        parts = re.split(logical_operator_pattern, query)

        # Reconstruct the query with proper CQL syntax
        # parts will be like: ["telework", "OR", "remote work"]
        cql_parts = []
        for part in parts:
            part = part.strip()
            if part in ["AND", "OR"]:
                # It's a logical operator, keep it as-is
                cql_parts.append(part)
            elif part:
                # It's a search term, wrap it with the field operator
                cql_parts.append(f'{field_operator} ~ "{part}"')

        result = " ".join(cql_parts)
        logger.info(
            f"Converted natural language with operators to CQL: "
            f"{query} -> {result}"
        )
        return result

    # No logical operators, wrap the entire query
    result = f'{field_operator} ~ "{query}"'
    logger.debug(f"Converted simple query to CQL: {query} -> {result}")
    return result


@confluence_mcp.tool(
    tags={"confluence", "read", "librarian"},
    annotations={"title": "Search Content", "readOnlyHint": True},
)
async def search(
    ctx: Context,
    query: Annotated[
        str,
        Field(
            description=(
                "Search query - can be either a simple text (e.g. 'project documentation') or a CQL query string. "
                "Simple queries use 'siteSearch' by default, to mimic the WebUI search, with an automatic fallback "
                "to 'text' search if not supported. Examples of CQL:\n"
                "- Basic search: 'type=page AND space=DEV'\n"
                "- Personal space search: 'space=\"~username\"' (note: personal space keys starting with ~ must be quoted)\n"
                "- Search by title: 'title~\"Meeting Notes\"'\n"
                "- Use siteSearch: 'siteSearch ~ \"important concept\"'\n"
                "- Use text search: 'text ~ \"important concept\"'\n"
                "- Recent content: 'created >= \"2023-01-01\"'\n"
                "- Content with specific label: 'label=documentation'\n"
                "- Recently modified content: 'lastModified > startOfMonth(\"-1M\")'\n"
                "- Content modified this year: 'creator = currentUser() AND lastModified > startOfYear()'\n"
                "- Content you contributed to recently: 'contributor = currentUser() AND lastModified > startOfWeek()'\n"
                "- Content watched by user: 'watcher = \"user@domain.com\" AND type = page'\n"
                '- Exact phrase in content: \'text ~ "\\"Urgent Review Required\\"" AND label = "pending-approval"\'\n'
                '- Title wildcards: \'title ~ "Minutes*" AND (space = "HR" OR space = "Marketing")\'\n'
                'Note: Special identifiers need proper quoting in CQL: personal space keys (e.g., "~username"), '
                "reserved words, numeric IDs, and identifiers with special characters."
            )
        ),
    ],
    limit: Annotated[
        int,
        Field(
            description="Maximum number of results to return. MUST be between 1-50 (default: 10).",
            default=10,
            ge=1,
            le=50,
        ),
    ] = 10,
    spaces_filter: Annotated[
        str | None,
        Field(
            description="Comma-separated space keys to filter by (optional, default: None). Overrides CONFLUENCE_SPACES_FILTER env var. Use empty string to disable filtering.",
            default=None,
        ),
    ] = None,
) -> str:
    """Search Confluence content using simple terms or CQL.

    Args:
        ctx: The FastMCP context.
        query: Search query - can be simple text or a CQL query string.
        limit: Maximum number of results (1-50).
        spaces_filter: Comma-separated list of space keys to filter by.

    Returns:
        JSON string representing a list of simplified Confluence page objects.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)

    # New code to tackles issues where query contains AND, OR, ... without being a proper CQL.
    # All queries are formatted no matter what:
    # - Natural language: "project documentation" -> 'siteSearch ~ "project documentation"'
    # - Non-CQL logic (agents format like this a LOT, making the former way return 0 results):
    #     "telework OR remote work" -> siteSearch ~ "telework" OR siteSearch ~ "remote work"'
    # - Already CQL: 'siteSearch ~ "devops" AND siteSearch ~ "kubernetes"'
    #     -> 'siteSearch ~ "devops" AND siteSearch ~ "kubernetes"' (unchanged)
    original_query = query
    try:
        # Try with siteSearch first
        query = _format_search_query(original_query, use_site_search=True)
        pages = confluence_fetcher.search(
            query, limit=limit, spaces_filter=spaces_filter
        )
    except Exception as e:
        # If siteSearch fails, fall back to text search
        logger.warning(
            f"siteSearch failed with query '{query}' (error: {e}), "
            f"falling back to text search."
        )
        query = _format_search_query(original_query, use_site_search=False)
        logger.info(f"Retrying with text search: {query}")
        pages = confluence_fetcher.search(
            query, limit=limit, spaces_filter=spaces_filter
        )
    search_results = [page.to_simplified_dict() for page in pages]
    return json.dumps(search_results, indent=2, ensure_ascii=False)

    # Previous code - Will not be used - Left untouched for tracking purposes
    # Check if the query is a simple search term or already a CQL query
    if query and not any(
        x in query for x in ["=", "~", ">", "<", " AND ", " OR ", "currentUser()"]
    ):
        original_query = query
        try:
            query = f'siteSearch ~ "{original_query}"'
            logger.info(
                f"Converting simple search term to CQL using siteSearch: {query}"
            )
            pages = confluence_fetcher.search(
                query, limit=limit, spaces_filter=spaces_filter
            )
        except Exception as e:
            logger.warning(f"siteSearch failed ('{e}'), falling back to text search.")
            query = f'text ~ "{original_query}"'
            logger.info(f"Falling back to text search with CQL: {query}")
            pages = confluence_fetcher.search(
                query, limit=limit, spaces_filter=spaces_filter
            )
    else:
        pages = confluence_fetcher.search(
            query, limit=limit, spaces_filter=spaces_filter
        )
    search_results = [page.to_simplified_dict() for page in pages]
    return json.dumps(search_results, indent=2, ensure_ascii=False)


@confluence_mcp.tool(
    tags={"confluence", "read", "librarian"},
    annotations={"title": "Get Page", "readOnlyHint": True},
)
async def get_page(
    ctx: Context,
    page_id: Annotated[
        str | int | None,
        Field(
            description="Confluence page ID (optional, default: None). Numeric ID from page URL. Provide this OR both 'title' and 'space_key'. If provided, title and space_key are ignored.",
            default=None,
        ),
    ] = None,
    title: Annotated[
        str | None,
        Field(
            description="Exact page title (optional, default: None). Use with 'space_key' if 'page_id' is not known.",
            default=None,
        ),
    ] = None,
    space_key: Annotated[
        str | None,
        Field(
            description="Space key where page resides (optional, default: None). Required if using 'title'. Examples: 'DEV', 'TEAM'",
            default=None,
        ),
    ] = None,
    include_metadata: Annotated[
        bool,
        Field(
            description="Include page metadata like creation date, last update, version, and labels (default: True).",
            default=True,
        ),
    ] = True,
    convert_to_markdown: Annotated[
        bool,
        Field(
            description="Convert page to markdown (default: True). False keeps raw HTML. CAUTION: HTML significantly increases token usage.",
            default=True,
        ),
    ] = True,
    body_format: Annotated[
        str,
        Field(
            description="Body format to retrieve from Confluence API (default: 'export_view'). Options: 'storage' (raw XHTML), 'view' (rendered), 'export_view' (most compatible, recommended).",
            default="export_view",
        ),
    ] = "export_view",
) -> str:
    """Get content of a specific Confluence page by its ID, or by its title and space key.

    Args:
        ctx: The FastMCP context.
        page_id: Confluence page ID. If provided, 'title' and 'space_key' are ignored.
        title: The exact title of the page. Must be used with 'space_key'.
        space_key: The key of the space. Must be used with 'title'.
        include_metadata: Whether to include page metadata.
        convert_to_markdown: Convert content to markdown (true) or keep raw HTML (false).
        body_format: Body format to retrieve ('storage', 'view', or 'export_view').

    Returns:
        JSON string representing the page content and/or metadata, or an error if not found or parameters are invalid.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)
    page_object = None

    if page_id:
        if title or space_key:
            logger.warning(
                "page_id was provided; title and space_key parameters will be ignored."
            )
        try:
            page_id_str = str(page_id)
            page_object = confluence_fetcher.get_page_content(
                page_id_str,
                convert_to_markdown=convert_to_markdown,
                body_format=body_format
            )
        except Exception as e:
            logger.error(f"Error fetching page by ID '{page_id}': {e}")
            return json.dumps(
                {"error": f"Failed to retrieve page by ID '{page_id}': {e}"},
                indent=2,
                ensure_ascii=False,
            )
    elif title and space_key:
        page_object = confluence_fetcher.get_page_by_title(
            space_key,
            title,
            convert_to_markdown=convert_to_markdown,
            body_format=body_format
        )
        if not page_object:
            return json.dumps(
                {
                    "error": f"Page with title '{title}' not found in space '{space_key}'."
                },
                indent=2,
                ensure_ascii=False,
            )
    else:
        raise ValueError(
            "Either 'page_id' OR both 'title' and 'space_key' must be provided."
        )

    if not page_object:
        return json.dumps(
            {"error": "Page not found with the provided identifiers."},
            indent=2,
            ensure_ascii=False,
        )

    if include_metadata:
        result = {"metadata": page_object.to_simplified_dict()}
    else:
        result = {"content": {"value": page_object.content}}

    return json.dumps(result, indent=2, ensure_ascii=False)


@confluence_mcp.tool(
    tags={"confluence", "read", "librarian"},
    annotations={"title": "Get Page Children", "readOnlyHint": True},
)
async def get_page_children(
    ctx: Context,
    parent_id: Annotated[
        str,
        Field(
            description="Parent page ID whose children to retrieve."
        ),
    ],
    expand: Annotated[
        str,
        Field(
            description="Fields to expand in response (default: 'version'). Examples: 'version', 'body.storage', 'body.export_view'",
            default="version",
        ),
    ] = "version",
    limit: Annotated[
        int,
        Field(
            description="Maximum number of child pages to return. MUST be between 1-50 (default: 25).",
            default=25,
            ge=1,
            le=50,
        ),
    ] = 25,
    include_content: Annotated[
        bool,
        Field(
            description="Include page content in response (default: False).",
            default=False,
        ),
    ] = False,
    convert_to_markdown: Annotated[
        bool,
        Field(
            description="Convert page content to markdown (default: True). Only relevant if include_content is True.",
            default=True,
        ),
    ] = True,
    start: Annotated[
        int,
        Field(
            description="Starting index for pagination (default: 0). MUST be 0 or greater.", 
            default=0, 
            ge=0),
    ] = 0,
    include_folders: Annotated[
        bool,
        Field(
            description="Whether to include child folders in addition to child pages",
            default=True,
        ),
    ] = True,
    body_format: Annotated[
        str,
        Field(
            description="Body format to retrieve from Confluence API (default: 'storage'). Options: 'storage' (raw XHTML), 'view' (rendered), 'export_view' (export-ready). Only used when body content is expanded.",
            default="storage",
        ),
    ] = "storage",
) -> str:
    """Get child pages and folders of a specific Confluence page.

    Args:
        ctx: The FastMCP context.
        parent_id: The ID of the parent page.
        expand: Fields to expand.
        limit: Maximum number of child items.
        include_content: Whether to include page content.
        convert_to_markdown: Convert content to markdown if include_content is true.
        start: Starting index for pagination.
        include_folders: Whether to include child folders (default: True).
        body_format: Body format to retrieve ('storage', 'view', or 'export_view').

    Returns:
        JSON string representing a list of child page and folder objects.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)
    if include_content and "body" not in expand:
        expand = f"{expand},body.{body_format}" if expand else f"body.{body_format}"

    try:
        pages = confluence_fetcher.get_page_children(
            page_id=parent_id,
            start=start,
            limit=limit,
            expand=expand,
            convert_to_markdown=convert_to_markdown,
            include_folders=include_folders,
            body_format=body_format,
        )
        child_pages = [page.to_simplified_dict() for page in pages]
        result = {
            "parent_id": parent_id,
            "count": len(child_pages),
            "limit_requested": limit,
            "start_requested": start,
            "results": child_pages,
        }
    except Exception as e:
        logger.error(
            f"Error getting/processing children for page ID {parent_id}: {e}",
            exc_info=True,
        )
        result = {"error": f"Failed to get child pages: {e}"}

    return json.dumps(result, indent=2, ensure_ascii=False)


@confluence_mcp.tool(
    tags={"confluence", "read", "librarian"},
    annotations={"title": "Get Comments", "readOnlyHint": True},
)
async def get_comments(
    ctx: Context,
    page_id: Annotated[
        str,
        Field(
            description=(
                "Confluence page ID (numeric ID, can be parsed from URL, "
                "e.g. from 'https://example.atlassian.net/wiki/spaces/TEAM/pages/123456789/Page+Title' "
                "-> '123456789')"
            )
        ),
    ],
) -> str:
    """Get comments for a specific Confluence page.

    Args:
        ctx: The FastMCP context.
        page_id: Confluence page ID.

    Returns:
        JSON string representing a list of comment objects.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)
    comments = confluence_fetcher.get_page_comments(page_id)
    formatted_comments = [comment.to_simplified_dict() for comment in comments]
    return json.dumps(formatted_comments, indent=2, ensure_ascii=False)


@confluence_mcp.tool(
    tags={"confluence", "read"},
    annotations={"title": "Get Labels", "readOnlyHint": True},
)
async def get_labels(
    ctx: Context,
    page_id: Annotated[
        str,
        Field(
            description="Confluence page ID (numeric ID from URL). Example: '123456789'"
        ),
    ],
) -> str:
    """Get labels for a specific Confluence page.

    Args:
        ctx: The FastMCP context.
        page_id: Confluence page ID.

    Returns:
        JSON string representing a list of label objects.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)
    labels = confluence_fetcher.get_page_labels(page_id)
    formatted_labels = [label.to_simplified_dict() for label in labels]
    return json.dumps(formatted_labels, indent=2, ensure_ascii=False)


@confluence_mcp.tool(
    tags={"confluence", "write"},
    annotations={"title": "Add Label", "destructiveHint": True},
)
@check_write_access
async def add_label(
    ctx: Context,
    page_id: Annotated[str, Field(description="The ID of the page to update")],
    name: Annotated[str, Field(description="The name of the label to add")],
) -> str:
    """Add label to an existing Confluence page.

    Args:
        ctx: The FastMCP context.
        page_id: The ID of the page to update.
        name: The name of the label.

    Returns:
        JSON string representing the updated list of label objects for the page.

    Raises:
        ValueError: If in read-only mode or Confluence client is unavailable.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)
    labels = confluence_fetcher.add_page_label(page_id, name)
    formatted_labels = [label.to_simplified_dict() for label in labels]
    return json.dumps(formatted_labels, indent=2, ensure_ascii=False)


@confluence_mcp.tool(
    tags={"confluence", "write"},
    annotations={"title": "Create Page", "destructiveHint": True},
)
@check_write_access
async def create_page(
    ctx: Context,
    space_key: Annotated[
        str,
        Field(
            description="Space key to create page in. Usually short uppercase code. Examples: 'DEV', 'TEAM', 'DOC'"
        ),
    ],
    title: Annotated[str, Field(description="Page title.")],
    content: Annotated[
        str,
        Field(
            description="Page content. Format depends on content_format parameter. Can be Markdown (default), wiki markup, or storage format."
        ),
    ],
    parent_id: Annotated[
        str | None,
        Field(
            description="Parent page ID (optional, default: None). If provided, page will be created as child of specified page.",
            default=None,
        ),
        BeforeValidator(lambda x: str(x) if x is not None else None),
    ] = None,
    content_format: Annotated[
        str,
        Field(
            description="(Optional) The format of the content parameter. Options: 'markdown' (default), 'wiki', or 'storage'. Wiki format uses Confluence wiki markup syntax",
            default="markdown",
        ),
    ] = "markdown",
    enable_heading_anchors: Annotated[
        bool,
        Field(
            description="(Optional) Whether to enable automatic heading anchor generation. Only applies when content_format is 'markdown'",
            default=False,
        ),
    ] = False,
) -> str:
    """Create a new Confluence page.

    Args:
        ctx: The FastMCP context.
        space_key: The key of the space.
        title: The title of the page.
        content: The content of the page (format depends on content_format).
        parent_id: Optional parent page ID.
        content_format: The format of the content ('markdown', 'wiki', or 'storage').
        enable_heading_anchors: Whether to enable heading anchors (markdown only).

    Returns:
        JSON string representing the created page object.

    Raises:
        ValueError: If in read-only mode, Confluence client is unavailable, or invalid content_format.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)

    # Validate content_format
    if content_format not in ["markdown", "wiki", "storage"]:
        raise ValueError(
            f"Invalid content_format: {content_format}. Must be 'markdown', 'wiki', or 'storage'"
        )

    # Determine parameters based on content format
    if content_format == "markdown":
        is_markdown = True
        content_representation = None  # Will be converted to storage
    else:
        is_markdown = False
        content_representation = content_format  # Pass 'wiki' or 'storage' directly

    page = confluence_fetcher.create_page(
        space_key=space_key,
        title=title,
        body=content,
        parent_id=parent_id,
        is_markdown=is_markdown,
        enable_heading_anchors=enable_heading_anchors
        if content_format == "markdown"
        else False,
        content_representation=content_representation,
    )
    result = page.to_simplified_dict()
    return json.dumps(
        {"message": "Page created successfully", "page": result},
        indent=2,
        ensure_ascii=False,
    )


@confluence_mcp.tool(
    tags={"confluence", "write"},
    annotations={"title": "Update Page", "destructiveHint": True},
)
@check_write_access
async def update_page(
    ctx: Context,
    page_id: Annotated[str, Field(description="The ID of the page to update")],
    title: Annotated[str, Field(description="The new title of the page")],
    content: Annotated[
        str,
        Field(
            description="The new content of the page. Format depends on content_format parameter"
        ),
    ],
    is_minor_edit: Annotated[
        bool, Field(description="Mark as minor edit (default: False).", default=False)
    ] = False,
    version_comment: Annotated[
        str | None, Field(description="Version comment (optional, default: None).", default=None)
    ] = None,
    parent_id: Annotated[
        str | None,
        Field(description="New parent page ID (optional, default: None).", default=None),
        BeforeValidator(lambda x: str(x) if x is not None else None),
    ] = None,
    content_format: Annotated[
        str,
        Field(
            description="Content format (default: 'markdown'). Valid values: 'markdown', 'wiki', 'storage'. Wiki uses Confluence wiki markup syntax.",
            default="markdown",
        ),
    ] = "markdown",
    enable_heading_anchors: Annotated[
        bool,
        Field(
            description="Enable automatic heading anchor generation (default: False). Only applies when content_format is 'markdown'.",
            default=False,
        ),
    ] = False,
) -> str:
    """Update an existing Confluence page.

    Args:
        ctx: The FastMCP context.
        page_id: The ID of the page to update.
        title: The new title of the page.
        content: The new content of the page (format depends on content_format).
        is_minor_edit: Whether this is a minor edit.
        version_comment: Optional comment for this version.
        parent_id: Optional new parent page ID.
        content_format: The format of the content ('markdown', 'wiki', or 'storage').
        enable_heading_anchors: Whether to enable heading anchors (markdown only).

    Returns:
        JSON string representing the updated page object.

    Raises:
        ValueError: If Confluence client is not configured, available, or invalid content_format.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)

    # Validate content_format
    if content_format not in ["markdown", "wiki", "storage"]:
        raise ValueError(
            f"Invalid content_format: {content_format}. Must be 'markdown', 'wiki', or 'storage'"
        )

    # Determine parameters based on content format
    if content_format == "markdown":
        is_markdown = True
        content_representation = None  # Will be converted to storage
    else:
        is_markdown = False
        content_representation = content_format  # Pass 'wiki' or 'storage' directly

    updated_page = confluence_fetcher.update_page(
        page_id=page_id,
        title=title,
        body=content,
        is_minor_edit=is_minor_edit,
        version_comment=version_comment,
        is_markdown=is_markdown,
        parent_id=parent_id,
        enable_heading_anchors=enable_heading_anchors
        if content_format == "markdown"
        else False,
        content_representation=content_representation,
    )
    page_data = updated_page.to_simplified_dict()
    return json.dumps(
        {"message": "Page updated successfully", "page": page_data},
        indent=2,
        ensure_ascii=False,
    )


@confluence_mcp.tool(
    tags={"confluence", "write"},
    annotations={"title": "Delete Page", "destructiveHint": True},
)
@check_write_access
async def delete_page(
    ctx: Context,
    page_id: Annotated[str, Field(description="The ID of the page to delete")],
) -> str:
    """Delete an existing Confluence page.

    Args:
        ctx: The FastMCP context.
        page_id: The ID of the page to delete.

    Returns:
        JSON string indicating success or failure.

    Raises:
        ValueError: If Confluence client is not configured or available.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)
    try:
        result = confluence_fetcher.delete_page(page_id=page_id)
        if result:
            response = {
                "success": True,
                "message": f"Page {page_id} deleted successfully",
            }
        else:
            response = {
                "success": False,
                "message": f"Unable to delete page {page_id}. API request completed but deletion unsuccessful.",
            }
    except Exception as e:
        logger.error(f"Error deleting Confluence page {page_id}: {str(e)}")
        response = {
            "success": False,
            "message": f"Error deleting page {page_id}",
            "error": str(e),
        }

    return json.dumps(response, indent=2, ensure_ascii=False)


@confluence_mcp.tool(
    tags={"confluence", "write"},
    annotations={"title": "Add Comment", "destructiveHint": True},
)
@check_write_access
async def add_comment(
    ctx: Context,
    page_id: Annotated[
        str, Field(description="The ID of the page to add a comment to")
    ],
    content: Annotated[
        str, Field(description="The comment content in Markdown format")
    ],
) -> str:
    """Add a comment to a Confluence page.

    Args:
        ctx: The FastMCP context.
        page_id: The ID of the page to add a comment to.
        content: The comment content in Markdown format.

    Returns:
        JSON string representing the created comment.

    Raises:
        ValueError: If in read-only mode or Confluence client is unavailable.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)
    try:
        comment = confluence_fetcher.add_comment(page_id=page_id, content=content)
        if comment:
            comment_data = comment.to_simplified_dict()
            response = {
                "success": True,
                "message": "Comment added successfully",
                "comment": comment_data,
            }
        else:
            response = {
                "success": False,
                "message": f"Unable to add comment to page {page_id}. API request completed but comment creation unsuccessful.",
            }
    except Exception as e:
        logger.error(f"Error adding comment to Confluence page {page_id}: {str(e)}")
        response = {
            "success": False,
            "message": f"Error adding comment to page {page_id}",
            "error": str(e),
        }

    return json.dumps(response, indent=2, ensure_ascii=False)


@confluence_mcp.tool(
    tags={"confluence", "read"},
    annotations={"title": "Search User", "readOnlyHint": True},
)
async def search_user(
    ctx: Context,
    query: Annotated[
        str,
        Field(
            description=(
                "Search query - a CQL query string for user search. "
                "Examples of CQL:\n"
                "- Basic user lookup by full name: 'user.fullname ~ \"First Last\"'\n"
                'Note: Special identifiers need proper quoting in CQL: personal space keys (e.g., "~username"), '
                "reserved words, numeric IDs, and identifiers with special characters."
            )
        ),
    ],
    limit: Annotated[
        int,
        Field(
            description="Maximum number of results (1-50)",
            default=10,
            ge=1,
            le=50,
        ),
    ] = 10,
) -> str:
    """Search Confluence users using CQL.

    Args:
        ctx: The FastMCP context.
        query: Search query - a CQL query string for user search.
        limit: Maximum number of results (1-50).

    Returns:
        JSON string representing a list of simplified Confluence user search result objects.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)

    # If the query doesn't look like CQL, wrap it as a user fullname search
    if query and not any(
        x in query for x in ["=", "~", ">", "<", " AND ", " OR ", "user."]
    ):
        # Simple search term - search by fullname
        query = f'user.fullname ~ "{query}"'
        logger.info(f"Converting simple search term to user CQL: {query}")

    try:
        user_results = confluence_fetcher.search_user(query, limit=limit)
        search_results = [user.to_simplified_dict() for user in user_results]
        return json.dumps(search_results, indent=2, ensure_ascii=False)
    except MCPAtlassianAuthenticationError as e:
        logger.error(f"Authentication error during user search: {e}", exc_info=False)
        return json.dumps(
            {
                "error": "Authentication failed. Please check your credentials.",
                "details": str(e),
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(f"Error searching users: {str(e)}")
        return json.dumps(
            {
                "error": f"An unexpected error occurred while searching for users: {str(e)}"
            },
            indent=2,
            ensure_ascii=False,
        )


@confluence_mcp.tool(
    tags={"confluence", "read", "analytics"},
    annotations={"title": "Get Page Views", "readOnlyHint": True},
)
async def confluence_get_page_views(
    ctx: Context,
    page_id: Annotated[
        str,
        Field(
            description=(
                "Confluence page ID (numeric ID, can be found in the page URL). "
                "For example, in 'https://example.atlassian.net/wiki/spaces/TEAM/pages/123456789/Page+Title', "
                "the page ID is '123456789'."
            )
        ),
    ],
    include_title: Annotated[
        bool,
        Field(description="Whether to fetch and include the page title"),
    ] = True,
) -> str:
    """Get view statistics for a Confluence page.

    Note: This tool is only available for Confluence Cloud. Server/Data Center
    instances do not support the Analytics API.

    Args:
        ctx: The FastMCP context.
        page_id: The Confluence page ID.
        include_title: Whether to include the page title in the response.

    Returns:
        JSON string with page view statistics including total views and last viewed date.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)
    try:
        result = confluence_fetcher.get_page_views(
            page_id=page_id,
            include_title=include_title,
        )
        return json.dumps(result.to_simplified_dict(), indent=2, ensure_ascii=False)
    except MCPAtlassianAuthenticationError as e:
        logger.error(f"Authentication error getting page views: {e}")
        return json.dumps(
            {
                "error": "Authentication failed. Please check your credentials.",
                "details": str(e),
            },
            indent=2,
            ensure_ascii=False,
        )
    except ValueError as e:
        logger.error(f"Error getting page views for {page_id}: {e}")
        return json.dumps(
            {"error": str(e), "page_id": page_id},
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(f"Unexpected error getting page views for {page_id}: {e}")
        return json.dumps(
            {"error": f"Failed to get page views: {e}", "page_id": page_id},
            indent=2,
            ensure_ascii=False,
        )
