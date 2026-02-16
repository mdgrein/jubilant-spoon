import asyncio
import json
import requests
from datetime import datetime
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Tree, Static, Button, Label
from textual.containers import Container, Horizontal

from client.api_client import ClowderAPIClient

SERVER_URL = "http://localhost:8000"
REQUEST_TIMEOUT = 3

STATUS_ICONS = {
    "pending": "[ ]",
    "running": "[*]",
    "completed": "[+]",
    "failed": "[X]",
    "retrying": "[R]",
}


def _status_icon(status: str) -> str:
    return STATUS_ICONS.get(status, "[ ]")


def _format_timestamp(iso_timestamp: str) -> str:
    """Format ISO timestamp for display. Returns HH:MM if today, MM-DD HH:MM otherwise."""
    if not iso_timestamp:
        return ""
    try:
        dt = datetime.fromisoformat(iso_timestamp.replace('Z', '+00:00'))
        now = datetime.now(dt.tzinfo)

        # If same day, show time only
        if dt.date() == now.date():
            return dt.strftime("%H:%M")
        # Otherwise show month-day and time
        return dt.strftime("%m-%d %H:%M")
    except:
        return ""


def _aggregate_status(statuses):
    if not statuses:
        return "pending"
    if any(s == "failed" for s in statuses):
        return "failed"
    if any(s == "retrying" for s in statuses):
        return "retrying"
    if any(s == "running" for s in statuses):
        return "running"
    if all(s == "completed" for s in statuses):
        return "completed"
    return "pending"


def _node_key(data):
    """Create a stable hashable key from node data for cursor tracking."""
    if not isinstance(data, dict):
        return None
    t = data.get("type", "")
    if t in ("root", "templates_header", "running_header"):
        return (t,)
    if t == "template":
        return ("template", data.get("name"))
    if t == "pipeline":
        return ("pipeline", data.get("id"))
    if t == "stage":
        return ("stage", data.get("pipeline_id"), data.get("name"))
    if t == "job":
        return ("job", data.get("name"))
    return (t,)


class ClowderClientApp(App):
    """A Textual app for the Clowder client — single-pane layout."""

    CSS = """
    #nav_tree {
        width: 2fr;
        max-width: 80;
        border-right: solid $accent;
    }
    #detail_panel {
        width: 4fr;
        padding: 1 2;
        overflow-y: auto;
    }
    """

    BINDINGS = [
        ("d", "toggle_dark", "Toggle dark mode"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, api_client: ClowderAPIClient = None):
        """
        Initialize the app.

        Args:
            api_client: Optional API client for server communication.
                       If None, creates a default client.
        """
        super().__init__()
        self.api_client = api_client or ClowderAPIClient()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Footer()
        yield Horizontal(
            Tree("Clowder", id="nav_tree"),
            Container(
                Static("Welcome to Clowder.\n\nSelect an item in the navigation tree."),
                id="detail_panel",
            ),
        )

    def on_mount(self) -> None:
        self._refreshing = False
        self._last_snapshot = None
        self._last_structure = None
        self._template_cache = {}  # Cache template details

        # Start background template loading (non-blocking)
        asyncio.create_task(self._async_fetch_templates())

        self._refresh_tree()
        self._poll_timer = self.set_interval(2, self._poll_refresh)

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    async def _async_fetch_templates(self):
        """Fetch all template details asynchronously without blocking UI."""
        # Run in thread pool to avoid blocking the event loop
        await asyncio.to_thread(self._fetch_and_cache_templates)

    def _fetch_and_cache_templates(self):
        """Fetch all template details once at startup and cache them."""
        try:
            # Get list of template IDs
            template_ids = self.api_client.fetch_templates()

            # Fetch all templates in parallel using threads
            import concurrent.futures

            def fetch_template(template_id):
                try:
                    return (template_id, self.api_client.fetch_template_details(template_id))
                except requests.exceptions.RequestException:
                    return (template_id, None)

            # Fetch up to 10 templates in parallel
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                results = executor.map(fetch_template, template_ids)
                for template_id, template_data in results:
                    if template_data:
                        self._template_cache[template_id] = template_data

        except requests.exceptions.RequestException:
            pass  # Server not available, cache will be empty

    def _fetch_data(self):
        """Fetch templates and pipelines from server. Returns (templates, running_pipelines, recent_pipelines)."""
        try:
            templates = self.api_client.fetch_templates()
        except requests.exceptions.RequestException:
            templates = []

        try:
            running_pipelines = self.api_client.fetch_running_pipelines()
        except requests.exceptions.RequestException:
            running_pipelines = []

        try:
            recent_pipelines = self.api_client.fetch_recent_pipelines(limit=10)
        except requests.exceptions.RequestException:
            recent_pipelines = []

        return (templates, running_pipelines, recent_pipelines)

    # ------------------------------------------------------------------
    # Tree refresh
    # ------------------------------------------------------------------

    def _refresh_tree(self) -> None:
        """Sync wrapper: fetch data + apply. Used for initial mount and tests."""
        templates, running_pipelines, recent_pipelines = self._fetch_data()
        self._apply_refresh(templates, running_pipelines, recent_pipelines)

    async def _poll_refresh(self) -> None:
        """Async timer callback — runs HTTP off the main thread."""
        if self._refreshing:
            return
        self._refreshing = True
        try:
            templates, running_pipelines, recent_pipelines = await asyncio.to_thread(self._fetch_data)
            self._apply_refresh(templates, running_pipelines, recent_pipelines)
        finally:
            self._refreshing = False

    @staticmethod
    def _structure_key(templates, running_pipelines, recent_pipelines):
        """Return a hashable tuple of tree shape, ignoring volatile data."""
        parts = []
        parts.append(tuple(templates))
        for pl in running_pipelines:
            stages = []
            for stage in pl.get("stages", []):
                jobs = tuple(j["name"] for j in stage.get("jobs", []))
                stages.append((stage["name"], jobs))
            parts.append(("running", pl.get("id"), pl.get("name"), tuple(stages)))
        for pl in recent_pipelines:
            stages = []
            for stage in pl.get("stages", []):
                jobs = tuple(j["name"] for j in stage.get("jobs", []))
                stages.append((stage["name"], jobs))
            parts.append(("recent", pl.get("id"), pl.get("name"), tuple(stages)))
        return tuple(parts)

    def _apply_refresh(self, templates, running_pipelines, recent_pipelines) -> None:
        """Decide: no-op, in-place update, or full rebuild."""
        # Content snapshot — if nothing changed at all, no-op
        snapshot = json.dumps({"t": templates, "r": running_pipelines, "rc": recent_pipelines}, sort_keys=True, default=str)
        if snapshot == self._last_snapshot:
            return
        self._last_snapshot = snapshot

        structure = self._structure_key(templates, running_pipelines, recent_pipelines)
        if self._last_structure is not None and structure == self._last_structure:
            # Structure same — update labels/data in place
            self._update_tree_content(templates, running_pipelines, recent_pipelines)
        else:
            # Structure changed — full rebuild
            self._rebuild_tree(templates, running_pipelines, recent_pipelines)
        self._last_structure = structure

    def _rebuild_tree(self, templates, running_pipelines, recent_pipelines) -> None:
        """Full clear + rebuild of the tree."""
        tree: Tree = self.query_one("#nav_tree", Tree)

        # Remember cursor so we can restore after rebuild
        selected_key = None
        if tree.cursor_node and tree.cursor_node.data:
            selected_key = _node_key(tree.cursor_node.data)

        tree.clear()
        tree.root.data = {"type": "root"}

        # --- Templates ---
        templates_node = tree.root.add("Templates", expand=True)
        templates_node.data = {"type": "templates_header"}
        for name in templates:
            leaf = templates_node.add_leaf(name)
            leaf.data = {"type": "template", "name": name}

        # --- Running ---
        running_node = tree.root.add("Running", expand=True)
        running_node.data = {"type": "running_header"}
        for pl in running_pipelines:
            pl_icon = _status_icon(pl.get("status", "pending"))
            pl_node = running_node.add(f"{pl_icon} {pl['name']}", expand=True)
            pl_node.data = {"type": "pipeline", "id": pl["id"], "name": pl["name"],
                            "description": pl.get("description", ""), "status": pl.get("status", "")}
            for stage in pl.get("stages", []):
                stage_statuses = [j["status"] for j in stage.get("jobs", [])]
                stage_icon = _status_icon(_aggregate_status(stage_statuses))
                stage_node = pl_node.add(f"{stage_icon} {stage['name']}", expand=True)
                stage_node.data = {"type": "stage", "pipeline_id": pl["id"],
                                   "name": stage["name"], "jobs": stage.get("jobs", [])}
                for job in stage.get("jobs", []):
                    job_icon = _status_icon(job["status"])
                    retries = job.get("retries", 0)
                    retry_label = f" (retry #{retries})" if retries > 0 else ""
                    job_leaf = stage_node.add_leaf(f"{job_icon} {job['name']}{retry_label}")
                    job_leaf.data = {"type": "job", "name": job["name"],
                                     "status": job["status"],
                                     "log": job.get("log"),
                                     "retries": retries}

        # --- Recent ---
        recent_node = tree.root.add("Recent", expand=False)
        recent_node.data = {"type": "recent_header"}
        for pl in recent_pipelines:
            pl_icon = _status_icon(pl.get("status", "completed"))
            timestamp = _format_timestamp(pl.get("completed_at", ""))
            timestamp_prefix = f"[{timestamp}] " if timestamp else ""
            pl_node = recent_node.add(f"{timestamp_prefix}{pl_icon} {pl['name']}", expand=False)
            pl_node.data = {"type": "pipeline", "id": pl["id"], "name": pl["name"],
                            "description": pl.get("description", ""), "status": pl.get("status", "")}
            for stage in pl.get("stages", []):
                stage_statuses = [j["status"] for j in stage.get("jobs", [])]
                stage_icon = _status_icon(_aggregate_status(stage_statuses))
                stage_node = pl_node.add(f"{stage_icon} {stage['name']}", expand=False)
                stage_node.data = {"type": "stage", "pipeline_id": pl["id"],
                                   "name": stage["name"], "jobs": stage.get("jobs", [])}
                for job in stage.get("jobs", []):
                    job_icon = _status_icon(job["status"])
                    retries = job.get("retries", 0)
                    retry_label = f" (retry #{retries})" if retries > 0 else ""
                    job_leaf = stage_node.add_leaf(f"{job_icon} {job['name']}{retry_label}")
                    job_leaf.data = {"type": "job", "name": job["name"],
                                     "status": job["status"],
                                     "log": job.get("log"),
                                     "retries": retries}

        tree.root.expand()

        # Restore cursor position by stable key
        if selected_key is not None:
            for node in self._walk_tree(tree.root):
                if node.data and _node_key(node.data) == selected_key:
                    tree.select_node(node)
                    break

    def _update_tree_content(self, templates, running_pipelines, recent_pipelines) -> None:
        """Walk existing nodes and update labels + data in place (no structural change)."""
        tree: Tree = self.query_one("#nav_tree", Tree)
        running_node = tree.root.children[1]  # Running header
        recent_node = tree.root.children[2] if len(tree.root.children) > 2 else None  # Recent header

        for pl_idx, pl in enumerate(running_pipelines):
            pl_node = running_node.children[pl_idx]
            pl_icon = _status_icon(pl.get("status", "pending"))
            pl_node.set_label(f"{pl_icon} {pl['name']}")
            pl_node.data = {"type": "pipeline", "id": pl["id"], "name": pl["name"],
                            "description": pl.get("description", ""), "status": pl.get("status", "")}

            for stage_idx, stage in enumerate(pl.get("stages", [])):
                stage_node = pl_node.children[stage_idx]
                stage_statuses = [j["status"] for j in stage.get("jobs", [])]
                stage_icon = _status_icon(_aggregate_status(stage_statuses))
                stage_node.set_label(f"{stage_icon} {stage['name']}")
                stage_node.data = {"type": "stage", "pipeline_id": pl["id"],
                                   "name": stage["name"], "jobs": stage.get("jobs", [])}

                for job_idx, job in enumerate(stage.get("jobs", [])):
                    job_node = stage_node.children[job_idx]
                    job_icon = _status_icon(job["status"])
                    retries = job.get("retries", 0)
                    retry_label = f" (retry #{retries})" if retries > 0 else ""
                    job_node.set_label(f"{job_icon} {job['name']}{retry_label}")
                    job_node.data = {"type": "job", "name": job["name"],
                                     "status": job["status"],
                                     "log": job.get("log"),
                                     "retries": retries}

        # Update recent pipelines
        if recent_node:
            for pl_idx, pl in enumerate(recent_pipelines):
                if pl_idx >= len(recent_node.children):
                    break
                pl_node = recent_node.children[pl_idx]
                pl_icon = _status_icon(pl.get("status", "completed"))
                timestamp = _format_timestamp(pl.get("completed_at", ""))
                timestamp_prefix = f"[{timestamp}] " if timestamp else ""
                pl_node.set_label(f"{timestamp_prefix}{pl_icon} {pl['name']}")
                pl_node.data = {"type": "pipeline", "id": pl["id"], "name": pl["name"],
                                "description": pl.get("description", ""), "status": pl.get("status", "")}

                for stage_idx, stage in enumerate(pl.get("stages", [])):
                    if stage_idx >= len(pl_node.children):
                        break
                    stage_node = pl_node.children[stage_idx]
                    stage_statuses = [j["status"] for j in stage.get("jobs", [])]
                    stage_icon = _status_icon(_aggregate_status(stage_statuses))
                    stage_node.set_label(f"{stage_icon} {stage['name']}")
                    stage_node.data = {"type": "stage", "pipeline_id": pl["id"],
                                       "name": stage["name"], "jobs": stage.get("jobs", [])}

                    for job_idx, job in enumerate(stage.get("jobs", [])):
                        if job_idx >= len(stage_node.children):
                            break
                        job_node = stage_node.children[job_idx]
                        job_icon = _status_icon(job["status"])
                        retries = job.get("retries", 0)
                        retry_label = f" (retry #{retries})" if retries > 0 else ""
                        job_node.set_label(f"{job_icon} {job['name']}{retry_label}")
                        job_node.data = {"type": "job", "name": job["name"],
                                         "status": job["status"],
                                         "log": job.get("log"),
                                         "retries": retries}

        # If tree has focus, refresh the detail panel for the highlighted node
        if tree.has_focus and tree.cursor_node and isinstance(tree.cursor_node.data, dict):
            self.call_later(self._render_detail, tree.cursor_node.data)

    # ------------------------------------------------------------------
    # Tree cursor movement → detail panel
    # ------------------------------------------------------------------

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        data = event.node.data
        if not isinstance(data, dict):
            return
        # Schedule the detail rendering to run async
        self.call_later(self._render_detail, data)

    async def _render_detail(self, data: dict) -> None:
        """Dispatch to the appropriate _show_* method."""
        node_type = data.get("type")
        try:
            if node_type in ("root", "templates_header", "running_header"):
                await self._show_welcome()
            elif node_type == "template":
                await self._show_template(data["name"])
            elif node_type == "pipeline":
                await self._show_pipeline(data)
            elif node_type == "stage":
                await self._show_stage(data)
            elif node_type == "job":
                await self._show_job(data)
        except Exception:
            pass  # panel may be detached during shutdown

    # ------------------------------------------------------------------
    # Detail renderers
    # ------------------------------------------------------------------

    def _clear_detail(self) -> Container | None:
        try:
            panel = self.query_one("#detail_panel", Container)
        except Exception:
            return None
        if not panel.is_attached:
            return None
        panel.remove_children()
        return panel

    async def _show_welcome(self) -> None:
        panel = self._clear_detail()
        if panel is None:
            return
        await asyncio.sleep(0.05)  # Let panel clear settle
        panel.mount(Static("Welcome to Clowder.\n\nSelect an item in the navigation tree."))

    async def _show_template(self, name: str) -> None:
        panel = self._clear_detail()
        if panel is None:
            return
        await asyncio.sleep(0.05)  # Let panel clear settle

        # Get template from cache (no network request!)
        template = self._template_cache.get(name)

        if template:
            # Build template description
            lines = [
                f"[bold]{template['name']}[/bold]",
                f"",
                f"{template['description']}",
                f"",
                f"[bold]Pipeline Structure:[/bold]",
                f""
            ]

            # Show stages and jobs
            for stage_idx, stage in enumerate(template.get("stages", [])):
                is_last_stage = stage_idx == len(template.get("stages", [])) - 1
                stage_prefix = "└─" if is_last_stage else "├─"
                lines.append(f"{stage_prefix} [cyan]{stage['name']}[/cyan]")

                jobs = stage.get("jobs", [])
                for job_idx, job in enumerate(jobs):
                    is_last_job = job_idx == len(jobs) - 1
                    job_prefix = "   └─" if is_last_stage else "│  └─" if is_last_job else "│  ├─"
                    if is_last_stage:
                        job_prefix = "   └─" if is_last_job else "   ├─"

                    agent = job['agent_type']
                    lines.append(f"{job_prefix} [green]{agent}[/green]")

                    # Show dependencies
                    if job.get("dependencies"):
                        for dep in job["dependencies"]:
                            dep_type = dep.get("type", "success")
                            dep_prefix = "   " if is_last_stage else "│  "
                            dep_prefix += "      " if is_last_job else "   │  "
                            arrow = "→" if dep_type == "success" else "⤷"
                            lines.append(f"{dep_prefix}{arrow} depends on [yellow]{dep['depends_on']}[/yellow]")

            lines.append("")
            panel.mount(Static("\n".join(lines), markup=True))
        else:
            # Template not in cache (shouldn't happen)
            panel.mount(Static(f"Template: {name}\n\n(Template details not available)"))

        panel.mount(Button("Start Pipeline", id="start_pipeline"))
        self._selected_template = name

    async def _show_pipeline(self, data: dict) -> None:
        panel = self._clear_detail()
        if panel is None:
            return
        await asyncio.sleep(0.05)  # Let panel clear settle
        icon = _status_icon(data.get("status", ""))
        panel.mount(Static(f"{icon} {data['name']}\n\n"
                           f"Status: {data.get('status', 'unknown')}\n"
                           f"Description: {data.get('description', '')}"))
        panel.mount(Button("Stop Pipeline", id="stop_pipeline"))
        self._selected_pipeline_id = data["id"]

    async def _show_stage(self, data: dict) -> None:
        panel = self._clear_detail()
        if panel is None:
            return
        await asyncio.sleep(0.05)  # Let panel clear settle
        lines = [f"Stage: {data['name']}\n"]
        for job in data.get("jobs", []):
            icon = _status_icon(job["status"])
            retries = job.get("retries", 0)
            retry_label = f" (retry #{retries})" if retries > 0 else ""
            lines.append(f"  {icon} {job['name']}{retry_label} — {job['status']}")
        panel.mount(Static("\n".join(lines)))

    async def _show_job(self, data: dict) -> None:
        panel = self._clear_detail()
        if panel is None:
            return
        await asyncio.sleep(0.05)  # Let panel clear settle
        retries = data.get("retries", 0)
        retry_info = f"\nRetries: {retries}" if retries > 0 else ""
        log_text = data.get("log") or "(no output yet)"
        panel.mount(Static(f"Job: {data['name']}\n"
                           f"Status: {data['status']}"
                           f"{retry_info}\n\n{log_text}"))

    # ------------------------------------------------------------------
    # Button actions
    # ------------------------------------------------------------------

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start_pipeline":
            await self._do_start_pipeline()
        elif event.button.id == "stop_pipeline":
            await self._do_stop_pipeline()

    async def _do_start_pipeline(self) -> None:
        name = getattr(self, "_selected_template", None)
        if not name:
            return

        # Show progress notification
        self.notify(f"Starting pipeline '{name}'...")

        try:
            # TODO: Add UI to collect prompt from user
            pipeline = await asyncio.to_thread(
                self.api_client.start_pipeline,
                name,
                "Test pipeline execution",
                "D:/workspace"
            )
            self.notify(f"Started pipeline '{pipeline['name']}'")
        except requests.exceptions.HTTPError as e:
            if e.response and e.response.status_code == 404:
                self.notify(f"Template '{name}' not found.", severity="error")
            else:
                self.notify(f"Error: {e}", severity="error")
        except requests.exceptions.RequestException as e:
            self.notify(f"Network error: {e}", severity="error")

        # Refresh data and restore focus to tree
        self._last_snapshot = None  # force rebuild
        self._last_structure = None
        templates, running_pipelines, recent_pipelines = await asyncio.to_thread(self._fetch_data)
        self._apply_refresh(templates, running_pipelines, recent_pipelines)

        # Restore focus to tree
        tree = self.query_one("#nav_tree", Tree)
        tree.focus()

    async def _do_stop_pipeline(self) -> None:
        pid = getattr(self, "_selected_pipeline_id", None)
        if not pid:
            return

        # Show progress notification
        self.notify(f"Stopping pipeline...")

        try:
            pipeline = await asyncio.to_thread(
                self.api_client.stop_pipeline, pid
            )
            self.notify(f"Stopped pipeline '{pipeline['name']}'")
        except requests.exceptions.HTTPError as e:
            if e.response and e.response.status_code == 404:
                self.notify(f"Pipeline not found.", severity="error")
            else:
                self.notify(f"Error: {e}", severity="error")
        except requests.exceptions.RequestException as e:
            self.notify(f"Network error: {e}", severity="error")

        # Refresh data and restore focus to tree
        self._last_snapshot = None  # force rebuild
        self._last_structure = None
        templates, running_pipelines, recent_pipelines = await asyncio.to_thread(self._fetch_data)
        self._apply_refresh(templates, running_pipelines, recent_pipelines)

        # Restore focus to tree
        tree = self.query_one("#nav_tree", Tree)
        tree.focus()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _walk_tree(node):
        yield node
        for child in node.children:
            yield from ClowderClientApp._walk_tree(child)


if __name__ == "__main__":
    app = ClowderClientApp()
    app.run()
