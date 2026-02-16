"""
HTTP API client for Clowder server.
Handles all network communication, separated from UI logic.
"""

import requests
from typing import Optional


class ClowderAPIClient:
    """Handles all HTTP communication with Clowder server."""

    def __init__(self, base_url: str = "http://localhost:8000", timeout: int = 3):
        """
        Initialize API client.

        Args:
            base_url: Base URL of the Clowder server
            timeout: Request timeout in seconds
        """
        self.base_url = base_url
        self.timeout = timeout

    def fetch_templates(self) -> list[str]:
        """
        Fetch list of template IDs from server.

        Returns:
            List of template ID strings

        Raises:
            requests.exceptions.RequestException: On network error
        """
        response = requests.get(
            f"{self.base_url}/pipelines/templates",
            timeout=self.timeout
        )
        response.raise_for_status()
        return response.json()

    def fetch_template_details(self, template_id: str) -> dict:
        """
        Fetch full template details.

        Args:
            template_id: Template ID to fetch

        Returns:
            Template dict with stages and jobs

        Raises:
            requests.exceptions.RequestException: On network error
            requests.exceptions.HTTPError: If template not found (404)
        """
        response = requests.get(
            f"{self.base_url}/pipelines/templates/{template_id}",
            timeout=self.timeout
        )
        response.raise_for_status()
        return response.json()

    def start_pipeline(self, template_id: str, prompt: str, workspace_path: str) -> dict:
        """
        Start a new pipeline.

        Args:
            template_id: Template to instantiate
            prompt: User's original prompt
            workspace_path: Workspace directory path

        Returns:
            Pipeline info dict

        Raises:
            requests.exceptions.RequestException: On network error
            requests.exceptions.HTTPError: If template not found (404)
        """
        response = requests.post(
            f"{self.base_url}/pipelines/{template_id}/start",
            json={"prompt": prompt, "workspace_path": workspace_path},
            timeout=self.timeout
        )
        response.raise_for_status()
        return response.json()

    def stop_pipeline(self, pipeline_id: str) -> dict:
        """
        Stop a running pipeline.

        Args:
            pipeline_id: Pipeline to stop

        Returns:
            Pipeline info dict with updated status

        Raises:
            requests.exceptions.RequestException: On network error
        """
        response = requests.post(
            f"{self.base_url}/pipelines/{pipeline_id}/stop",
            timeout=self.timeout
        )
        response.raise_for_status()
        return response.json()

    def fetch_running_pipelines(self) -> list[dict]:
        """
        Fetch currently running pipelines.

        Returns:
            List of pipeline dicts with nested stages and jobs

        Raises:
            requests.exceptions.RequestException: On network error
        """
        response = requests.get(
            f"{self.base_url}/pipelines/running",
            timeout=self.timeout
        )
        response.raise_for_status()
        return response.json()

    def fetch_recent_pipelines(self, limit: int = 10) -> list[dict]:
        """
        Fetch recently completed/failed pipelines.

        Args:
            limit: Maximum number of recent pipelines to return

        Returns:
            List of pipeline dicts with nested stages and jobs

        Raises:
            requests.exceptions.RequestException: On network error
        """
        response = requests.get(
            f"{self.base_url}/pipelines/recent",
            params={"limit": limit},
            timeout=self.timeout
        )
        response.raise_for_status()
        return response.json()

    def ping(self) -> bool:
        """
        Check if server is reachable.

        Returns:
            True if server responds to ping

        Raises:
            requests.exceptions.RequestException: On network error
        """
        response = requests.get(
            f"{self.base_url}/ping",
            timeout=self.timeout
        )
        response.raise_for_status()
        return response.json().get("pong", False)
