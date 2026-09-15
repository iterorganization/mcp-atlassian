"""Request logging utilities for HTTP requests and responses."""

import json
import logging
from typing import Any

import requests
from requests import PreparedRequest, Response
from requests.adapters import HTTPAdapter

from mcp_atlassian.utils.logging import get_masked_session_headers, mask_sensitive

logger = logging.getLogger("mcp-atlassian.request-logging")


class LoggingHTTPAdapter(HTTPAdapter):
    """HTTP adapter that logs all requests and responses when protocol logging is enabled."""

    def send(
        self, request: PreparedRequest, **kwargs: Any
    ) -> Response:
        """Send a request and log it if protocol logging is enabled.

        Args:
            request: The prepared request to send
            **kwargs: Additional arguments for the request

        Returns:
            The response object
        """
        # Only log if logger is effectively enabled at INFO level
        # This respects both MCP_LOG_PROTOCOL environment variable and logger configuration
        should_log = logger.isEnabledFor(logging.INFO)

        # Log request
        if should_log:
            self._log_request(request, kwargs)

        # Send the request
        response = super().send(request, **kwargs)
        
        # Log response
        if should_log:
            self._log_response(response, request)

        return response

    def _log_request(self, request: PreparedRequest, kwargs: Any) -> None:
        """Log outgoing HTTP request at WARNING level."""
        # Get headers (masked for sensitive data)
        headers = dict(request.headers)
        masked_headers = get_masked_session_headers(headers)
        
        # Get request body
        body = None
        if request.body:
            try:
                if isinstance(request.body, bytes):
                    body_str = request.body.decode('utf-8', errors='replace')
                else:
                    body_str = str(request.body)
                
                # Try to parse as JSON for pretty printing
                try:
                    body = json.loads(body_str)
                    body = json.dumps(body, indent=2)
                except (json.JSONDecodeError, TypeError):
                    body = body_str
            except Exception as e:
                body = f"<Error reading body: {e}>"
        
        # Log at INFO level
        logger.info("=" * 80)
        logger.info(f"OUTGOING REQUEST: {request.method} {request.url}")
        logger.info("Request Headers:")
        for header_name, header_value in masked_headers.items():
            logger.info(f"  {header_name}: {header_value}")
        
        if body:
            logger.info("Request Body:")
            # Mask sensitive data in body if it's JSON
            try:
                body_dict = json.loads(body) if isinstance(body, str) else body
                masked_body = self._mask_sensitive_in_dict(body_dict)
                logger.info(json.dumps(masked_body, indent=2))
            except (json.JSONDecodeError, TypeError):
                logger.info(body)
        else:
            logger.info("Request Body: <empty>")
        logger.info("=" * 80)

    def _log_response(self, response: Response, request: PreparedRequest) -> None:
        """Log incoming HTTP response at WARNING level."""
        # Get response headers (masked for sensitive data)
        headers = dict(response.headers)
        masked_headers = get_masked_session_headers(headers)
        
        # Get response body
        body = None
        try:
            # Try to get text content
            content_type = response.headers.get('Content-Type', '')
            if 'application/json' in content_type or 'text' in content_type:
                try:
                    body = response.text
                    # Try to parse as JSON for pretty printing
                    try:
                        body_dict = json.loads(body)
                        body = json.dumps(body_dict, indent=2)
                    except (json.JSONDecodeError, TypeError):
                        pass
                except Exception:
                    body = f"<Response body length: {len(response.content)} bytes>"
            else:
                body = f"<Binary content, length: {len(response.content)} bytes>"
        except Exception as e:
            body = f"<Error reading response body: {e}>"
        
        # Log at INFO level
        logger.info("=" * 80)
        logger.info(f"INCOMING RESPONSE: {request.method} {request.url}")
        logger.info(f"Status Code: {response.status_code}")
        logger.info("Response Headers:")
        for header_name, header_value in masked_headers.items():
            logger.info(f"  {header_name}: {header_value}")
        
        if body:
            logger.info("Response Body:")
            # Mask sensitive data in body if it's JSON
            try:
                body_dict = json.loads(body) if isinstance(body, str) else body
                masked_body = self._mask_sensitive_in_dict(body_dict)
                logger.info(json.dumps(masked_body, indent=2))
            except (json.JSONDecodeError, TypeError):
                logger.info(body)
        else:
            logger.info("Response Body: <empty>")
        logger.info("=" * 80)

    def _mask_sensitive_in_dict(self, data: Any) -> Any:
        """Recursively mask sensitive fields in a dictionary."""
        if isinstance(data, dict):
            masked = {}
            sensitive_keys = {
                'password', 'token', 'access_token', 'refresh_token', 
                'api_token', 'api_key', 'secret', 'authorization',
                'authorization_header', 'credentials', 'auth'
            }
            for key, value in data.items():
                if any(sensitive in key.lower() for sensitive in sensitive_keys):
                    masked[key] = mask_sensitive(str(value))
                elif isinstance(value, (dict, list)):
                    masked[key] = self._mask_sensitive_in_dict(value)
                else:
                    masked[key] = value
            return masked
        elif isinstance(data, list):
            return [self._mask_sensitive_in_dict(item) for item in data]
        else:
            return data


def wrap_session_with_logging(session: requests.Session) -> requests.Session:
    """Wrap a requests.Session with logging adapter.

    Args:
        session: The session to wrap

    Returns:
        The same session object (modified in place)
    """
    # Mount the logging adapter for both http and https
    adapter = LoggingHTTPAdapter()
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

