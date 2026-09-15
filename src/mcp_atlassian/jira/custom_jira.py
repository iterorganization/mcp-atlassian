"""Custom Jira client with extended comment functionality."""

from typing import Any

from atlassian import Jira
from atlassian.typehints import T_resp_json

class CustomJira(Jira):
    """Extended Jira client with support for comment properties."""

    def issue_add_comment(
        self,
        issue_key: str,
        comment: str,
        visibility: dict[str, str] | None = None,
        properties: list[dict[str, Any]] | None = None,
    ) -> T_resp_json:
        """
        Add a comment to a Jira issue with optional properties.

        :param issue_key: Issue key (e.g., 'PROJ-123')
        :param comment: Comment text
        :param visibility: Comment visibility settings
        :param properties: List of property objects to attach to comment
        :return: Created comment object
        """
        data: dict[str, Any] = {"body": comment}

        if visibility:
            data["visibility"] = visibility

        if properties:
            data["properties"] = properties

        url = self.resource_url(f"issue/{issue_key}/comment")
        return self.post(url, data=data)

    def issue_edit_comment(
        self,
        issue_key: str,
        comment_id: str,
        comment: str,
        visibility: dict[str, str] | None = None,
        properties: list[dict[str, Any]] | None = None,
    ) -> T_resp_json:
        """
        Edit an existing comment with optional properties.

        :param issue_key: Issue key
        :param comment_id: Comment ID to edit
        :param comment: Updated comment text
        :param visibility: Comment visibility settings
        :param properties: List of property objects
        :return: Updated comment object
        """
        data: dict[str, Any] = {"body": comment}

        if visibility:
            data["visibility"] = visibility

        if properties:
            data["properties"] = properties

        url = self.resource_url(f"issue/{issue_key}/comment/{comment_id}")
        return self.put(url, data=data)