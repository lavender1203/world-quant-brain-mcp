"""Regression tests for the scoped performance-comparison endpoint."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import requests

from main import BrainApiClient


class PerformanceComparisonEndpointTests(unittest.IsolatedAsyncioTestCase):
    """Ensure each ownership mode builds the endpoint accepted by BRAIN."""

    async def asyncSetUp(self) -> None:
        # Bypass __init__: route construction does not need Redis, credentials,
        # or an HTTP session, and unit tests must not touch those services.
        self.client = object.__new__(BrainApiClient)
        self.client.base_url = "https://api.worldquantbrain.com"
        self.client.ensure_authenticated = AsyncMock()
        self.client._request_json_with_retries = AsyncMock(return_value={"ok": True})
        self.archive_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.archive_directory.cleanup)
        self.archive_environment = patch.dict(
            os.environ,
            {"BRAIN_PERFORMANCE_COMPARISON_ARCHIVE": self.archive_directory.name},
        )
        self.archive_environment.start()
        self.addCleanup(self.archive_environment.stop)

    async def assert_scope(self, expected_scope: str, **scope_arguments: str) -> None:
        """Call the client and assert that it sends one request to the expected scope."""
        result = await self.client.performance_comparison("alpha123", **scope_arguments)

        self.assertEqual(result["ok"], True)
        self.assertEqual(result["_archive"]["saved"], True)
        self.assertTrue(Path(result["_archive"]["path"]).is_file())
        self.client._request_json_with_retries.assert_awaited_once_with(
            "GET",
            (
                f"{self.client.base_url}/{expected_scope}/alphas/alpha123/"
                "before-and-after-performance"
            ),
            op_name="performance_comparison",
        )

    async def test_uses_authenticated_user_scope_by_default(self) -> None:
        await self.assert_scope("users/self")

    async def test_uses_team_scope_when_team_is_given(self) -> None:
        await self.assert_scope("teams/team456", team_id="team456")

    async def test_competition_scope_takes_precedence(self) -> None:
        await self.assert_scope(
            "competitions/PAC2026",
            competition="PAC2026",
            team_id="team456",
        )

    async def test_reports_submitted_alpha_as_unavailable(self) -> None:
        """Turn the platform's OS-only 400 into an actionable result."""
        failed_response = requests.Response()
        failed_response.status_code = 400
        error = requests.HTTPError(response=failed_response)
        self.client._request_json_with_retries.side_effect = error

        details_response = requests.Response()
        details_response.status_code = 200
        details_response._content = (
            b'{"stage":"OS","status":"ACTIVE",'
            b'"dateSubmitted":"2026-09-01T00:45:41-04:00"}'
        )
        self.client._request = AsyncMock(return_value=details_response)

        result = await self.client.performance_comparison("submitted123")

        self.assertEqual(result["available"], False)
        self.assertEqual(
            result["reason"], "performance_comparison_is_pre_submission_only"
        )
        self.assertEqual(result["stage"], "OS")


if __name__ == "__main__":
    unittest.main()
