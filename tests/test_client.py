import asyncio
import inspect
import requests as requests_lib
import pytest
from unittest.mock import MagicMock, Mock, patch

from textual.widgets import Button
from client.main import ClowderClientApp
from client.api_client import ClowderAPIClient


async def _settle():
    """Yield to the event loop briefly, avoiding _wait_for_screen hangs."""
    await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_api_client():
    """Create a mock API client for testing."""
    client = Mock(spec=ClowderAPIClient)
    # Set default return values
    client.fetch_templates.return_value = []
    client.fetch_running_pipelines.return_value = []
    client.fetch_recent_pipelines.return_value = []
    client.fetch_template_details.return_value = {}
    return client


def _make_resp(json_data):
    """Build a MagicMock that quacks like a requests.Response."""
    resp = MagicMock()
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


@pytest.fixture
def mock_templates_resp():
    return _make_resp(["build-and-test", "deploy-staging"])


@pytest.fixture
def mock_running_resp():
    return _make_resp([
        {
            "id": "aaaa-bbbb",
            "name": "build-and-test",
            "description": "CI pipeline",
            "status": "running",
            "stages": [
                {
                    "name": "Build",
                    "jobs": [
                        {"name": "compile", "status": "completed", "log": "OK", "retries": 0},
                        {"name": "lint", "status": "running", "log": None, "retries": 0},
                    ],
                }
            ],
        },
    ])


@pytest.fixture
def mock_running_resp_updated():
    """Same structure as mock_running_resp but lint is now completed."""
    return _make_resp([
        {
            "id": "aaaa-bbbb",
            "name": "build-and-test",
            "description": "CI pipeline",
            "status": "completed",
            "stages": [
                {
                    "name": "Build",
                    "jobs": [
                        {"name": "compile", "status": "completed", "log": "OK", "retries": 0},
                        {"name": "lint", "status": "completed", "log": "All good", "retries": 0},
                    ],
                }
            ],
        },
    ])


@pytest.fixture
def mock_running_resp_two_pipelines():
    """Two pipelines — triggers structural change (full rebuild)."""
    return _make_resp([
        {
            "id": "aaaa-bbbb",
            "name": "build-and-test",
            "description": "CI pipeline",
            "status": "running",
            "stages": [
                {
                    "name": "Build",
                    "jobs": [
                        {"name": "compile", "status": "completed", "log": "OK", "retries": 0},
                        {"name": "lint", "status": "running", "log": None, "retries": 0},
                    ],
                }
            ],
        },
        {
            "id": "cccc-dddd",
            "name": "deploy-staging",
            "description": "Deploy pipeline",
            "status": "running",
            "stages": [
                {
                    "name": "Deploy",
                    "jobs": [
                        {"name": "push", "status": "running", "log": None, "retries": 0},
                    ],
                }
            ],
        },
    ])


@pytest.fixture
def mock_empty_running_resp():
    return _make_resp([])


def _route_get(templates_resp, running_resp):
    """Return a side_effect callable that routes by URL."""
    def _get(url, **kwargs):
        if "templates" in url:
            return templates_resp
        if "running" in url:
            return running_resp
        return _make_resp([])
    return _get


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_detail_text(app):
    """Return the text content of the first Static in #detail_panel."""
    panel = app.query_one("#detail_panel")
    statics = panel.query("Static")
    if statics:
        return str(statics[0].render())
    return ""


def _get_detail_button_ids(app):
    """Return list of button IDs in the detail panel."""
    panel = app.query_one("#detail_panel")
    return [b.id for b in panel.query("Button")]


# ---------------------------------------------------------------------------
# Existing tests (updated: NodeSelected → NodeHighlighted)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_app_has_nav_tree_and_detail_panel(mock_api_client):
    """App should mount with #nav_tree and #detail_panel."""
    mock_api_client.fetch_templates.return_value = []
    mock_api_client.fetch_running_pipelines.return_value = []

    app = ClowderClientApp(api_client=mock_api_client)
    async with app.run_test() as pilot:
        await pilot.pause()

        tree = app.query_one("#nav_tree")
        panel = app.query_one("#detail_panel")
        assert tree is not None
        assert panel is not None


@pytest.mark.asyncio
async def test_nav_tree_shows_templates_and_running(mock_api_client, mock_running_resp):
    """Tree should contain 'Templates' and 'Running' child nodes."""
    mock_api_client.fetch_templates.return_value = ["build-and-test", "deploy-staging"]
    mock_api_client.fetch_running_pipelines.return_value = mock_running_resp.json()

    app = ClowderClientApp(api_client=mock_api_client)
    async with app.run_test() as pilot:
        app._refresh_tree()
        await pilot.pause()

        tree = app.query_one("#nav_tree")
        child_labels = [str(c.label) for c in tree.root.children]
        assert "Templates" in child_labels
        assert "Running" in child_labels


@pytest.mark.asyncio
async def test_templates_listed_as_leaves(mock_api_client):
    """Each template name should appear as a leaf under Templates."""
    mock_api_client.fetch_templates.return_value = ["build-and-test", "deploy-staging"]
    mock_api_client.fetch_running_pipelines.return_value = []

    app = ClowderClientApp(api_client=mock_api_client)
    async with app.run_test() as pilot:
        app._refresh_tree()
        await pilot.pause()

        tree = app.query_one("#nav_tree")
        templates_node = tree.root.children[0]  # first child is Templates
        leaf_labels = [str(c.label) for c in templates_node.children]
        assert "build-and-test" in leaf_labels
        assert "deploy-staging" in leaf_labels


@pytest.mark.asyncio
async def test_selecting_template_shows_start_button(mock_api_client):
    """Highlighting a template node should render a 'Start Pipeline' button in detail panel."""
    mock_api_client.fetch_templates.return_value = ["build-and-test", "deploy-staging"]
    mock_api_client.fetch_running_pipelines.return_value = []

    app = ClowderClientApp(api_client=mock_api_client)
    async with app.run_test() as pilot:
        app._refresh_tree()
        await pilot.pause()

        tree = app.query_one("#nav_tree")
        template_node = tree.root.children[0].children[0]  # first template leaf

        # Simulate highlight
        from textual.widgets import Tree as TreeWidget
        app.on_tree_node_highlighted(TreeWidget.NodeHighlighted(template_node))
        await pilot.pause()

        panel = app.query_one("#detail_panel")
        buttons = panel.query("Button")
        assert any(b.id == "start_pipeline" for b in buttons)


@pytest.mark.asyncio
async def test_starting_pipeline_via_button(mock_api_client):
    """Clicking 'Start Pipeline' should POST to the server and notify."""
    mock_api_client.fetch_templates.return_value = ["build-and-test", "deploy-staging"]
    mock_api_client.fetch_running_pipelines.return_value = []
    mock_api_client.start_pipeline.return_value = {"name": "build-and-test", "id": "new-id"}

    app = ClowderClientApp(api_client=mock_api_client)
    async with app.run_test() as pilot:
        app._refresh_tree()
        await pilot.pause()

        # Highlight the first template
        tree = app.query_one("#nav_tree")
        template_node = tree.root.children[0].children[0]
        from textual.widgets import Tree as TreeWidget
        app.on_tree_node_highlighted(TreeWidget.NodeHighlighted(template_node))
        await pilot.pause()

        # Click the start button
        btn = app.query_one("#start_pipeline")
        await pilot.click(f"#{btn.id}")
        await pilot.pause()
        mock_api_client.start_pipeline.assert_called_once()
        assert mock_api_client.start_pipeline.call_args[0][0] == "build-and-test"


@pytest.mark.asyncio
async def test_selecting_pipeline_shows_stop_button(mock_api_client, mock_running_resp):
    """Highlighting a running pipeline node should show a 'Stop Pipeline' button."""
    mock_api_client.fetch_templates.return_value = ["build-and-test", "deploy-staging"]
    mock_api_client.fetch_running_pipelines.return_value = mock_running_resp.json()

    app = ClowderClientApp(api_client=mock_api_client)
    async with app.run_test() as pilot:
        app._refresh_tree()
        await pilot.pause()

        tree = app.query_one("#nav_tree")
        running_node = tree.root.children[1]  # Running
        pipeline_node = running_node.children[0]

        from textual.widgets import Tree as TreeWidget
        app.on_tree_node_highlighted(TreeWidget.NodeHighlighted(pipeline_node))
        await pilot.pause()

        panel = app.query_one("#detail_panel")
        buttons = panel.query("Button")
        assert any(b.id == "stop_pipeline" for b in buttons)


@pytest.mark.asyncio
async def test_selecting_stage_shows_jobs_overview(mock_api_client, mock_running_resp):
    """Highlighting a stage node shows its jobs and statuses in the detail panel."""
    mock_api_client.fetch_templates.return_value = ["build-and-test", "deploy-staging"]
    mock_api_client.fetch_running_pipelines.return_value = mock_running_resp.json()

    app = ClowderClientApp(api_client=mock_api_client)
    async with app.run_test() as pilot:
        app._refresh_tree()
        await pilot.pause()

        tree = app.query_one("#nav_tree")
        running_node = tree.root.children[1]
        pipeline_node = running_node.children[0]
        stage_node = pipeline_node.children[0]

        from textual.widgets import Tree as TreeWidget
        app.on_tree_node_highlighted(TreeWidget.NodeHighlighted(stage_node))
        await pilot.pause()

        panel = app.query_one("#detail_panel")
        content = str(panel.query_one("Static").render())
        assert "Stage: Build" in content
        assert "compile" in content
        assert "lint" in content


@pytest.mark.asyncio
async def test_selecting_job_shows_log(mock_api_client, mock_running_resp):
    """Highlighting a job node shows its log output in the detail panel."""
    mock_api_client.fetch_templates.return_value = ["build-and-test", "deploy-staging"]
    mock_api_client.fetch_running_pipelines.return_value = mock_running_resp.json()

    app = ClowderClientApp(api_client=mock_api_client)
    async with app.run_test() as pilot:
        app._refresh_tree()
        await pilot.pause()

        tree = app.query_one("#nav_tree")
        running_node = tree.root.children[1]
        pipeline_node = running_node.children[0]
        stage_node = pipeline_node.children[0]
        job_node = stage_node.children[0]  # compile

        from textual.widgets import Tree as TreeWidget
        app.on_tree_node_highlighted(TreeWidget.NodeHighlighted(job_node))
        await pilot.pause()

        panel = app.query_one("#detail_panel")
        content = str(panel.query_one("Static").render())
        assert "Job: compile" in content
        assert "OK" in content


@pytest.mark.asyncio
async def test_auto_refresh(mock_api_client):
    """Tree should poll the server multiple times via set_interval."""
    call_count = 0

    def counting_fetch(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return []

    mock_api_client.fetch_templates.side_effect = counting_fetch
    mock_api_client.fetch_running_pipelines.return_value = []

    app = ClowderClientApp(api_client=mock_api_client)
    async with app.run_test() as pilot:
        await pilot.pause()
        initial = call_count
        await pilot.pause(delay=3)

        assert call_count > initial


@pytest.mark.asyncio
async def test_dark_mode_toggle(mock_api_client):
    """Pressing 'd' should toggle the theme."""
    mock_api_client.fetch_templates.return_value = []
    mock_api_client.fetch_running_pipelines.return_value = []

    app = ClowderClientApp(api_client=mock_api_client)
    async with app.run_test() as pilot:
        await pilot.pause()

        original = app.theme
        await pilot.press("d")
        assert app.theme != original
        await pilot.press("d")
        assert app.theme == original


# ---------------------------------------------------------------------------
# New keyboard-driven tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_navigate_tree_with_arrow_keys(mock_api_client, mock_running_resp):
    """Arrow keys should move through tree nodes in order."""
    mock_api_client.fetch_templates.return_value = ["build-and-test", "deploy-staging"]
    mock_api_client.fetch_running_pipelines.return_value = mock_running_resp.json()

    app = ClowderClientApp(api_client=mock_api_client)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._poll_timer.stop()

        tree = app.query_one("#nav_tree")
        tree.focus()
        await _settle()

        # Start at root (cursor_line 0)
        assert tree.cursor_node.data["type"] == "root"

        # Navigate down through all nodes
        expected_types = [
            "templates_header", "template", "template",
            "running_header", "pipeline", "stage", "job", "job",
        ]
        for expected in expected_types:
            tree.action_cursor_down()
            await _settle()
            assert tree.cursor_node.data["type"] == expected, \
                f"Expected {expected}, got {tree.cursor_node.data['type']}"

        # Navigate back up
        tree.action_cursor_up()
        await _settle()
        assert tree.cursor_node.data["type"] == "job"
        assert tree.cursor_node.data["name"] == "compile"


@pytest.mark.asyncio
async def test_detail_panel_updates_on_cursor_move(mock_api_client, mock_running_resp):
    """Detail panel should update as cursor moves through tree."""
    mock_api_client.fetch_templates.return_value = ["build-and-test", "deploy-staging"]
    mock_api_client.fetch_running_pipelines.return_value = mock_running_resp.json()

    app = ClowderClientApp(api_client=mock_api_client)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._poll_timer.stop()

        tree = app.query_one("#nav_tree")
        tree.focus()
        await _settle()

        # Root → welcome
        text = _get_detail_text(app)
        assert "Welcome to Clowder" in text

        # Down to Templates header → welcome
        tree.action_cursor_down()
        await _settle()
        await _settle()  # Extra wait for async rendering
        text = _get_detail_text(app)
        assert "Welcome to Clowder" in text

        # Down to first template → "Template: build-and-test"
        tree.action_cursor_down()
        await _settle()
        await _settle()  # Extra wait for async rendering
        text = _get_detail_text(app)
        assert "Template: build-and-test" in text
        assert "start_pipeline" in _get_detail_button_ids(app)

        # Down to second template → "Template: deploy-staging"
        tree.action_cursor_down()
        await _settle()
        await _settle()  # Extra wait for async rendering
        text = _get_detail_text(app)
        assert "Template: deploy-staging" in text

        # Down to Running header → welcome
        tree.action_cursor_down()
        await _settle()
        await _settle()  # Extra wait for async rendering
        text = _get_detail_text(app)
        assert "Welcome to Clowder" in text


@pytest.mark.asyncio
async def test_expand_collapse_nodes(mock_api_client, mock_running_resp):
    """Enter should toggle expand/collapse on branch nodes."""
    mock_api_client.fetch_templates.return_value = ["build-and-test", "deploy-staging"]
    mock_api_client.fetch_running_pipelines.return_value = mock_running_resp.json()

    app = ClowderClientApp(api_client=mock_api_client)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._poll_timer.stop()

        tree = app.query_one("#nav_tree")
        tree.focus()
        await pilot.pause()

        # Move to Templates header
        tree.action_cursor_down()
        await pilot.pause()
        assert tree.cursor_node.data["type"] == "templates_header"

        # Collapse Templates with select (Enter equivalent)
        tree.action_select_cursor()
        await pilot.pause()
        assert not tree.cursor_node.is_expanded

        # Down should skip to Running since Templates children are hidden
        tree.action_cursor_down()
        await pilot.pause()
        assert tree.cursor_node.data["type"] == "running_header"

        # Go back up to Templates
        tree.action_cursor_up()
        await pilot.pause()
        assert tree.cursor_node.data["type"] == "templates_header"

        # Expand Templates with select
        tree.action_select_cursor()
        await pilot.pause()
        assert tree.cursor_node.is_expanded

        # Down should enter template children
        tree.action_cursor_down()
        await pilot.pause()
        assert tree.cursor_node.data["type"] == "template"


@pytest.mark.asyncio
async def test_full_tree_navigation_with_running_pipeline(mock_api_client, mock_running_resp):
    """Navigate all 9 positions and verify detail content at each."""
    mock_api_client.fetch_templates.return_value = ["build-and-test", "deploy-staging"]
    mock_api_client.fetch_running_pipelines.return_value = mock_running_resp.json()

    app = ClowderClientApp(api_client=mock_api_client)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._poll_timer.stop()

        tree = app.query_one("#nav_tree")
        tree.focus()
        await _settle()

        # Position 0: root
        assert tree.cursor_node.data["type"] == "root"
        await _settle()  # Wait for initial rendering
        assert "Welcome to Clowder" in _get_detail_text(app)

        # Position 1: templates_header
        tree.action_cursor_down()
        await _settle()
        await _settle()  # Extra wait for async rendering
        assert tree.cursor_node.data["type"] == "templates_header"
        assert "Welcome to Clowder" in _get_detail_text(app)
        assert "start_pipeline" not in _get_detail_button_ids(app)

        # Position 2: template (build-and-test)
        tree.action_cursor_down()
        await _settle()
        await _settle()  # Extra wait for async rendering
        assert tree.cursor_node.data["type"] == "template"
        assert tree.cursor_node.data["name"] == "build-and-test"
        assert "Template: build-and-test" in _get_detail_text(app)
        assert "start_pipeline" in _get_detail_button_ids(app)

        # Position 3: template (deploy-staging)
        tree.action_cursor_down()
        await _settle()
        await _settle()  # Extra wait for async rendering
        assert tree.cursor_node.data["type"] == "template"
        assert tree.cursor_node.data["name"] == "deploy-staging"
        assert "Template: deploy-staging" in _get_detail_text(app)
        assert "start_pipeline" in _get_detail_button_ids(app)

        # Position 4: running_header
        tree.action_cursor_down()
        await _settle()
        await _settle()  # Extra wait for async rendering
        assert tree.cursor_node.data["type"] == "running_header"
        assert "Welcome to Clowder" in _get_detail_text(app)
        assert "stop_pipeline" not in _get_detail_button_ids(app)

        # Position 5: pipeline
        tree.action_cursor_down()
        await _settle()
        await _settle()  # Extra wait for async rendering
        assert tree.cursor_node.data["type"] == "pipeline"
        assert tree.cursor_node.data["id"] == "aaaa-bbbb"
        text = _get_detail_text(app)
        assert "build-and-test" in text
        assert "running" in text
        assert "stop_pipeline" in _get_detail_button_ids(app)

        # Position 6: stage
        tree.action_cursor_down()
        await _settle()
        await _settle()  # Extra wait for async rendering
        assert tree.cursor_node.data["type"] == "stage"
        assert tree.cursor_node.data["name"] == "Build"
        text = _get_detail_text(app)
        assert "Stage: Build" in text
        assert "compile" in text
        assert "lint" in text

        # Position 7: job (compile)
        tree.action_cursor_down()
        await _settle()
        await _settle()  # Extra wait for async rendering
        assert tree.cursor_node.data["type"] == "job"
        assert tree.cursor_node.data["name"] == "compile"
        text = _get_detail_text(app)
        assert "Job: compile" in text
        assert "OK" in text

        # Position 8: job (lint)
        tree.action_cursor_down()
        await _settle()
        await _settle()  # Extra wait for async rendering
        assert tree.cursor_node.data["type"] == "job"
        assert tree.cursor_node.data["name"] == "lint"
        text = _get_detail_text(app)
        assert "Job: lint" in text
        assert "(no output yet)" in text


@pytest.mark.asyncio
async def test_start_pipeline_keyboard_flow(mock_api_client):
    """Navigate to template with keys, Tab to Start button, Enter to activate."""
    mock_api_client.fetch_templates.return_value = ["build-and-test", "deploy-staging"]
    mock_api_client.fetch_running_pipelines.return_value = []
    mock_api_client.start_pipeline.return_value = {"name": "build-and-test", "id": "new-id"}

    app = ClowderClientApp(api_client=mock_api_client)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._poll_timer.stop()

        tree = app.query_one("#nav_tree")
        tree.focus()
        await pilot.pause()

        # Navigate to first template (down, down)
        tree.action_cursor_down()
        tree.action_cursor_down()
        await pilot.pause()
        assert tree.cursor_node.data["type"] == "template"
        assert tree.cursor_node.data["name"] == "build-and-test"

        # Tab to focus the Start button
        await pilot.press("tab")
        await pilot.pause()
        btn = app.query_one("#start_pipeline", Button)
        assert btn.has_focus

        # Press Enter to activate
        await pilot.press("enter")
        await pilot.pause()
        mock_api_client.start_pipeline.assert_called_once()
        assert mock_api_client.start_pipeline.call_args[0][0] == "build-and-test"


@pytest.mark.asyncio
async def test_stop_pipeline_keyboard_flow(mock_api_client, mock_running_resp):
    """Navigate to pipeline with keys, Tab to Stop button, Enter to activate."""
    mock_api_client.fetch_templates.return_value = ["build-and-test", "deploy-staging"]
    mock_api_client.fetch_running_pipelines.return_value = mock_running_resp.json()
    mock_api_client.stop_pipeline.return_value = {"name": "build-and-test", "id": "aaaa-bbbb"}

    app = ClowderClientApp(api_client=mock_api_client)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._poll_timer.stop()

        tree = app.query_one("#nav_tree")
        tree.focus()
        await _settle()

        # Navigate to pipeline: root → templates_header → t1 → t2 → running_header → pipeline
        for _ in range(5):
            tree.action_cursor_down()
            await _settle()

        # Wait for the detail panel to render the pipeline
        await _settle()
        await _settle()
        await _settle()  # Extra wait for async rendering
        assert tree.cursor_node.data["type"] == "pipeline"

        # Verify stop button is shown
        btn = app.query_one("#stop_pipeline", Button)
        assert btn is not None

        # Focus and activate the Stop button
        btn.focus()
        await _settle()
        assert btn.has_focus

        # Press Enter to activate
        btn.press()
        await _settle()
        mock_api_client.stop_pipeline.assert_called_once()
        assert mock_api_client.stop_pipeline.call_args[0][0] == "aaaa-bbbb"


@pytest.mark.asyncio
async def test_in_place_update_changes_labels_not_structure(
    mock_api_client, mock_running_resp, mock_running_resp_updated
):
    """In-place update should reuse node objects but update labels and data."""
    mock_api_client.fetch_templates.return_value = ["build-and-test", "deploy-staging"]
    mock_api_client.fetch_running_pipelines.return_value = mock_running_resp.json()

    app = ClowderClientApp(api_client=mock_api_client)
    async with app.run_test(size=(120, 40)) as pilot:
        app._refresh_tree()
        await pilot.pause()

        tree = app.query_one("#nav_tree")
        running_node = tree.root.children[1]
        pipeline_node = running_node.children[0]
        stage_node = pipeline_node.children[0]
        lint_node = stage_node.children[1]

        # Capture object references
        original_pipeline_node = pipeline_node
        original_lint_node = lint_node

        assert lint_node.data["status"] == "running"
        assert lint_node.data["log"] is None

        # Apply updated data (same structure, different statuses)
        templates = ["build-and-test", "deploy-staging"]
        pipelines_updated = mock_running_resp_updated.json()
        app._last_snapshot = None  # force past no-op check
        app._apply_refresh(templates, pipelines_updated, [])
        await pilot.pause()

        # Same node objects (in-place, not rebuilt)
        assert tree.root.children[1].children[0] is original_pipeline_node
        assert tree.root.children[1].children[0].children[0].children[1] is original_lint_node

        # But data and labels updated
        assert original_lint_node.data["status"] == "completed"
        assert original_lint_node.data["log"] == "All good"
        assert "[+]" in str(original_lint_node.label)


@pytest.mark.asyncio
async def test_structural_change_triggers_full_rebuild(
    mock_api_client, mock_running_resp, mock_running_resp_two_pipelines
):
    """Adding a pipeline should trigger full rebuild with new nodes."""
    mock_api_client.fetch_templates.return_value = ["build-and-test", "deploy-staging"]
    mock_api_client.fetch_running_pipelines.return_value = mock_running_resp.json()

    app = ClowderClientApp(api_client=mock_api_client)
    async with app.run_test(size=(120, 40)) as pilot:
        app._refresh_tree()
        await pilot.pause()

        tree = app.query_one("#nav_tree")
        running_node = tree.root.children[1]
        assert len(running_node.children) == 1

        # Apply data with 2 pipelines (structural change)
        templates = ["build-and-test", "deploy-staging"]
        pipelines_two = mock_running_resp_two_pipelines.json()
        app._last_snapshot = None
        app._last_structure = None  # force structural change detection
        app._apply_refresh(templates, pipelines_two, [])
        await pilot.pause()

        running_node = tree.root.children[1]
        assert len(running_node.children) == 2
        labels = [str(c.label) for c in running_node.children]
        assert any("build-and-test" in l for l in labels)
        assert any("deploy-staging" in l for l in labels)


@pytest.mark.asyncio
async def test_poll_refresh_is_async(mock_api_client):
    """_poll_refresh should be an async method and app should have _refreshing flag."""
    # Return empty lists initially, then raise exception on subsequent calls
    call_count = [0]
    def maybe_fail(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] > 2:  # Let first couple calls succeed
            raise Exception("no server")
        return []

    mock_api_client.fetch_templates.side_effect = maybe_fail
    mock_api_client.fetch_running_pipelines.return_value = []

    app = ClowderClientApp(api_client=mock_api_client)
    assert inspect.iscoroutinefunction(app._poll_refresh)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert hasattr(app, "_refreshing")


@pytest.mark.asyncio
async def test_quit_binding(mock_api_client):
    """Pressing 'q' should exit the app cleanly."""
    mock_api_client.fetch_templates.return_value = []
    mock_api_client.fetch_running_pipelines.return_value = []

    app = ClowderClientApp(api_client=mock_api_client)
    async with app.run_test() as pilot:
        await pilot.pause()

        # Should not crash
        await pilot.press("q")
        await pilot.pause()


@pytest.mark.asyncio
async def test_server_unreachable_shows_empty_tree(mock_api_client):
    """When server is unreachable, tree should have headers but no children."""
    mock_api_client.fetch_templates.side_effect = requests_lib.exceptions.ConnectionError("Connection refused")
    mock_api_client.fetch_running_pipelines.side_effect = requests_lib.exceptions.ConnectionError("Connection refused")
    mock_api_client.fetch_recent_pipelines.side_effect = requests_lib.exceptions.ConnectionError("Connection refused")

    app = ClowderClientApp(api_client=mock_api_client)
    async with app.run_test() as pilot:
        app._last_snapshot = None
        app._last_structure = None
        app._refresh_tree()
        await pilot.pause()

        tree = app.query_one("#nav_tree")
        # Should have Templates, Running, and Recent headers
        assert len(tree.root.children) == 3
        templates_node = tree.root.children[0]
        running_node = tree.root.children[1]
        assert str(templates_node.label) == "Templates"
        assert str(running_node.label) == "Running"
        # But no children under either
        assert len(templates_node.children) == 0
        assert len(running_node.children) == 0
