"""Keyboard-driven Textual TUI.

Presentation only — **zero business logic**.  Every screen builds the same
:class:`~fpsaudio.core.contracts.JobSpec` the CLI builds and hands it to the
same planner and scheduler.  If a decision is being made here that the CLI
cannot make, that is a bug.

Five screens, in the order the work actually happens:

1. **Source**   — pick files, see every stream and its Atmos findings
2. **Retime**   — preset or explicit rates, method, and the exact ratio
3. **Output**   — codec, container, template, overwrite policy
4. **Plan**     — "what will happen", shown *before* anything runs (§7)
5. **Run**      — queue, per-job progress, verification results

The plan screen is deliberately not skippable: the single largest failure of
both legacy programs was doing something destructive without saying so first.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

from ..core.config import CONTAINERS, encodable_codecs
from ..core.contracts import (
    AtmosPolicy,
    AudioStream,
    DitherMode,
    JobSpec,
    MediaFile,
    OutputSpec,
    ProgressEvent,
    Refusal,
    RetimeMethod,
    RetimeSpec,
    VerifySpec,
)
from ..core.jobs import JobState, QueueStore, Scheduler, auto_workers
from ..core.plan import build_plan
from ..core.probe import discover_files, probe_file
from ..core.ratio import LEGACY_PRESET_KEYS, get_preset, iter_presets

try:
    from textual import on, work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.reactive import reactive
    from textual.screen import Screen
    from textual.widgets import (
        Button,
        DataTable,
        Footer,
        Header,
        Input,
        Label,
        ProgressBar,
        RichLog,
        Select,
        Static,
    )
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "The TUI needs textual. Install it with: pip install textual"
    ) from exc


__all__ = ["FpsAudioApp", "run"]


class SessionState:
    """Everything the wizard has collected so far.  Plain data, no behaviour."""

    def __init__(self) -> None:
        self.inputs: list[Path] = []
        self.media: dict[Path, MediaFile] = {}
        self.selected: list[tuple[Path, AudioStream]] = []
        self.preset_key: str = "23.976_to_25"
        self.method: RetimeMethod = RetimeMethod.RESAMPLE
        self.codec: str = "auto"
        self.container: str = "auto"
        self.bitrate: str = ""
        self.template: str = "{stem}__a{index}__{profile}"
        self.overwrite: str = "skip"
        self.dither: DitherMode = DitherMode.TPDF
        self.output_dir: Path = Path.cwd() / "retimed"
        self.atmos_policy: AtmosPolicy = AtmosPolicy.REFUSE
        self.accepted: list[str] = []
        self.null_test: bool = False
        self.loudness: bool = False

    def specs(self) -> list[JobSpec]:
        ratio = get_preset(self.preset_key)
        return [
            JobSpec(
                source=path,
                stream_index=stream.stream_index,
                profile_key=ratio.key,
                retime=RetimeSpec(
                    src_fps=ratio.src_fps, dst_fps=ratio.dst_fps, method=self.method
                ),
                output=OutputSpec(
                    directory=self.output_dir,
                    template=self.template,
                    container=self.container,
                    codec=self.codec,
                    bitrate=self.bitrate or None,
                    dither=self.dither,
                    overwrite=self.overwrite,
                ),
                verify=VerifySpec(null_test=self.null_test, loudness=self.loudness),
                atmos_policy=self.atmos_policy,
                accepted=tuple(self.accepted),
            )
            for path, stream in self.selected
        ]


# --------------------------------------------------------------------------- #
# 1. Source
# --------------------------------------------------------------------------- #

class SourceScreen(Screen):
    BINDINGS = [
        Binding("space", "toggle", "Select/deselect stream"),
        Binding("a", "select_all", "Select all"),
        Binding("n", "next", "Next"),
        Binding("p", "focus_path", "Add another path"),
        Binding("q", "app.quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Vertical(
            Label("Source files", classes="title"),
            Input(placeholder="Path to a file or folder, then Enter", id="path"),
            Label("", id="scan-status"),
            DataTable(id="streams", cursor_type="row"),
            Static("", id="stream-detail", classes="detail"),
            id="body",
        )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#streams", DataTable)
        table.add_columns("", "file", "stream", "codec", "ch", "rate", "objects")
        self.query_one("#path", Input).focus()

    @on(Input.Submitted, "#path")
    def action_add_path(self) -> None:
        field = self.query_one("#path", Input)
        raw = field.value.strip().strip('"')
        if not raw:
            return
        field.value = ""
        # Hand focus to the table. Without this the Input keeps focus and the
        # single-key bindings below (space / a / n) are swallowed as text, so
        # the user types "ann" into the path box instead of selecting streams.
        self.query_one("#streams", DataTable).focus()
        self.scan(Path(raw).expanduser())

    def action_focus_path(self) -> None:
        self.query_one("#path", Input).focus()

    @work(thread=True, exclusive=False)
    def scan(self, root: Path) -> None:
        status = self.query_one("#scan-status", Label)
        self.app.call_from_thread(status.update, f"Scanning {root} ...")
        try:
            files = discover_files(root, recursive=True)
        except Refusal as refusal:
            self.app.call_from_thread(status.update, f"[red]{refusal.message}[/red]")
            return

        state: SessionState = self.app.state
        found = unidentified = 0
        for path in files:
            media = probe_file(path)
            state.media[path] = media
            if not media.identified or not media.audio:
                unidentified += 1
                self.app.call_from_thread(
                    self._add_unidentified, path, "; ".join(media.problems)
                )
                continue
            for stream in media.audio:
                found += 1
                self.app.call_from_thread(self._add_stream, path, stream)

        message = f"{found} stream(s) found in {len(files)} file(s)"
        if unidentified:
            # B-5: never silently produce zero jobs.
            message += f"; [yellow]{unidentified} file(s) could not be identified[/yellow]"
        self.app.call_from_thread(status.update, message)

    def _add_stream(self, path: Path, stream: AudioStream) -> None:
        table = self.query_one("#streams", DataTable)
        objects = ""
        if stream.atmos.present:
            objects = f"{stream.atmos.kind} ({stream.atmos.certainty})"
        elif stream.atmos.certainty == "unknown":
            objects = "UNKNOWN"
        table.add_row(
            " ",
            path.name,
            f"#{stream.stream_index}",
            stream.codec + (f"/{stream.profile}" if stream.profile else ""),
            str(stream.channels or "?"),
            str(stream.sample_rate or "?"),
            objects,
            key=f"{path}::{stream.stream_index}",
        )

    def _add_unidentified(self, path: Path, reason: str) -> None:
        table = self.query_one("#streams", DataTable)
        table.add_row("!", path.name, "-", "unidentified", "-", "-", reason[:30])

    def action_toggle(self) -> None:
        table = self.query_one("#streams", DataTable)
        if table.cursor_row < 0:
            return
        row_key = list(table.rows)[table.cursor_row]
        key = str(row_key.value)
        if "::" not in key:
            return
        path_text, index_text = key.rsplit("::", 1)
        path, index = Path(path_text), int(index_text)
        state: SessionState = self.app.state
        entry = next(
            (e for e in state.selected if e[0] == path and e[1].stream_index == index),
            None,
        )
        if entry:
            state.selected.remove(entry)
            table.update_cell(row_key, list(table.columns)[0], " ")
        else:
            state.selected.append((path, state.media[path].stream(index)))
            table.update_cell(row_key, list(table.columns)[0], "X")

    def action_select_all(self) -> None:
        state: SessionState = self.app.state
        state.selected.clear()
        table = self.query_one("#streams", DataTable)
        first_column = list(table.columns)[0]
        for row_key in table.rows:
            key = str(row_key.value)
            if "::" not in key:
                continue
            path_text, index_text = key.rsplit("::", 1)
            path = Path(path_text)
            state.selected.append((path, state.media[path].stream(int(index_text))))
            table.update_cell(row_key, first_column, "X")

    def action_next(self) -> None:
        state: SessionState = self.app.state
        if not state.selected:
            self.query_one("#scan-status", Label).update(
                "[yellow]Select at least one stream with Space (or A for all).[/yellow]"
            )
            return
        self.app.push_screen(RetimeScreen())


# --------------------------------------------------------------------------- #
# 2. Retime
# --------------------------------------------------------------------------- #

class RetimeScreen(Screen):
    BINDINGS = [
        Binding("n", "next", "Next"),
        Binding("escape", "app.pop_screen", "Back"),
        Binding("q", "app.quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        # Seed every control from the session, not from a literal. A Select
        # fires Changed on mount, so a hardcoded initial value would overwrite
        # the user's choice every time they step back into this screen.
        state: SessionState = self.app.state
        yield Header(show_clock=False)
        yield Vertical(
            Label("Retime", classes="title"),
            Select(
                [(ratio.label, key) for key, ratio in iter_presets()],
                value=state.preset_key,
                id="preset",
            ),
            Select(
                [
                    ("Resample — pitch moves with speed (correct, default)", "resample"),
                    ("Redeclare — bit-exact where the rate allows", "redeclare"),
                    ("Stretch — pitch preserved (opt-in, NOT a speed change)", "stretch"),
                ],
                value=state.method.value,
                id="method",
            ),
            Static("", id="ratio-detail", classes="detail"),
            id="body",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.update_ratio_panel()

    @on(Select.Changed)
    def _changed(self, event: Select.Changed) -> None:
        state: SessionState = self.app.state
        if event.select.id == "preset" and event.value is not None:
            state.preset_key = str(event.value)
        elif event.select.id == "method" and event.value is not None:
            state.method = RetimeMethod(str(event.value))
        self.update_ratio_panel()

    def update_ratio_panel(self) -> None:
        state: SessionState = self.app.state
        ratio = get_preset(state.preset_key)
        lines = [
            f"Exact speed ratio    [b]{ratio.speed_str}[/b]   "
            f"({ratio.percent_approx():+.6f}%)",
            f"Duration multiplier  {ratio.duration_scale.numerator}/"
            f"{ratio.duration_scale.denominator}",
            "",
        ]
        rates = {s.sample_rate for _, s in state.selected if s.sample_rate}
        for rate in sorted(r for r in rates if r):
            new_rate = ratio.redeclared_rate(rate)
            if new_rate.denominator == 1:
                lines.append(
                    f"[green]At {rate} Hz a bit-exact retime IS possible "
                    f"(redeclare as {int(new_rate)} Hz).[/green]"
                )
            else:
                lines.append(
                    f"At {rate} Hz a bit-exact retime is not possible "
                    f"({rate} x {ratio.speed_str} = {new_rate})."
                )
        if state.method is RetimeMethod.STRETCH:
            lines += [
                "",
                "[yellow]Stretch keeps the original pitch. That is NOT what happens "
                "when film runs at a different speed — a real speed change moves "
                "pitch with rate. Both legacy converters did this by default.[/yellow]",
            ]
        self.query_one("#ratio-detail", Static).update("\n".join(lines))

    def action_next(self) -> None:
        self.app.push_screen(OutputScreen())


# --------------------------------------------------------------------------- #
# 3. Output
# --------------------------------------------------------------------------- #

class OutputScreen(Screen):
    BINDINGS = [
        Binding("n", "next", "Next"),
        Binding("escape", "app.pop_screen", "Back"),
        Binding("q", "app.quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        # Seeded from the session for the same reason as RetimeScreen: stepping
        # back into this screen must not silently reset what was chosen.
        state: SessionState = self.app.state
        yield Header(show_clock=False)
        yield VerticalScroll(
            Label("Output", classes="title"),
            Label("Directory"),
            Input(value=str(state.output_dir), id="outdir"),
            Label("Codec"),
            Select(
                [("auto — keep the source family", "auto")]
                + [(codec, codec) for codec in encodable_codecs()],
                value=state.codec,
                id="codec",
            ),
            Label("Container"),
            Select(
                [("auto", "auto")] + [(name, name) for name in sorted(CONTAINERS)],
                value=state.container,
                id="container",
            ),
            Label("Bitrate (lossy only, e.g. 256k)"),
            Input(
                value=state.bitrate,
                placeholder="leave blank for the encoder default",
                id="bitrate",
            ),
            Label("Filename template"),
            Input(value=state.template, id="template"),
            Label("If the output exists"),
            Select(
                [("skip", "skip"), ("rename alongside", "rename"), ("overwrite", "overwrite")],
                value=state.overwrite,
                id="overwrite",
            ),
            Label("Dolby Atmos sources"),
            Select(
                [
                    ("refuse — stop rather than lose objects", "refuse"),
                    ("hand-off — retime and prepare for Dolby Media Encoder", "handoff"),
                    ("flatten — discard objects (needs confirmation)", "flatten"),
                ],
                value=state.atmos_policy.value,
                id="atmos",
            ),
            id="body",
        )
        yield Footer()

    @on(Select.Changed)
    @on(Input.Changed)
    def _changed(self, event: Any) -> None:
        state: SessionState = self.app.state
        widget_id = event.control.id
        value = event.value
        if value is None:
            return
        if widget_id == "codec":
            state.codec = str(value)
        elif widget_id == "container":
            state.container = str(value)
        elif widget_id == "bitrate":
            state.bitrate = str(value).strip()
        elif widget_id == "template":
            state.template = str(value) or "{stem}__a{index}__{profile}"
        elif widget_id == "overwrite":
            state.overwrite = str(value)
        elif widget_id == "outdir":
            state.output_dir = Path(str(value)).expanduser()
        elif widget_id == "atmos":
            state.atmos_policy = AtmosPolicy(str(value))

    def action_next(self) -> None:
        self.app.push_screen(PlanScreen())


# --------------------------------------------------------------------------- #
# 4. Plan — "what will happen", before anything happens
# --------------------------------------------------------------------------- #

class PlanScreen(Screen):
    BINDINGS = [
        Binding("r", "run", "Run"),
        Binding("a", "accept", "Accept the listed loss"),
        Binding("escape", "app.pop_screen", "Back"),
        Binding("q", "app.quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Vertical(
            Label("What will happen", classes="title"),
            VerticalScroll(Static("", id="plan-body"), id="plan-scroll"),
            Label("", id="plan-status"),
            id="body",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_plan()

    def refresh_plan(self) -> None:
        state: SessionState = self.app.state
        chunks: list[str] = []
        self._tokens: list[str] = []
        runnable = 0

        for spec in state.specs():
            media = state.media[spec.source]
            try:
                plan = build_plan(spec, media, claim_output=False)
            except Refusal as refusal:
                chunks.append(f"[red]{_escape(refusal.render())}[/red]")
                if refusal.override_token:
                    self._tokens.append(refusal.override_token)
                chunks.append("")
                continue
            runnable += 1
            chunks.append(_escape(plan.describe()))
            chunks.append("")
            chunks.append("[dim]Commands that would run:[/dim]")
            chunks.append(f"[dim]{_escape(plan.render_commands())}[/dim]")
            chunks.append("")

        self.query_one("#plan-body", Static).update("\n".join(chunks))
        status = f"{runnable} job(s) ready"
        if self._tokens:
            unique = sorted(set(self._tokens))
            status += (
                f"; [yellow]{len(unique)} refusal(s). Press A to accept: "
                f"{', '.join(unique)}[/yellow]"
            )
        self.query_one("#plan-status", Label).update(status)

    def action_accept(self) -> None:
        """Accept every refusal token currently listed.

        This is the typed-confirmation gate: the loss has just been described in
        full on this screen, and accepting is a separate, deliberate keypress.
        """
        state: SessionState = self.app.state
        for token in getattr(self, "_tokens", ()):
            if token not in state.accepted:
                state.accepted.append(token)
        self.refresh_plan()

    def action_run(self) -> None:
        self.app.push_screen(RunScreen())


def _escape(text: str) -> str:
    return text.replace("[", r"\[")


# --------------------------------------------------------------------------- #
# 5. Run
# --------------------------------------------------------------------------- #

class RunScreen(Screen):
    BINDINGS = [
        Binding("s", "stop", "Stop"),
        Binding("escape", "app.pop_screen", "Back"),
        Binding("q", "app.quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Vertical(
            Label("Running", classes="title"),
            ProgressBar(total=100, id="overall", show_eta=False),
            DataTable(id="queue", cursor_type="row"),
            RichLog(id="log", markup=True, wrap=True),
            id="body",
        )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#queue", DataTable)
        table.add_columns("job", "file", "stream", "state", "progress", "detail")
        self._rows: dict[str, Any] = {}
        state: SessionState = self.app.state
        for spec in state.specs():
            key = table.add_row(
                spec.short_id, spec.source.name, f"#{spec.stream_index}",
                "pending", "0%", "",
            )
            self._rows[spec.job_id] = key
        self.execute()

    @work(thread=True, exclusive=True)
    def execute(self) -> None:
        state: SessionState = self.app.state
        specs = state.specs()
        store = QueueStore(state.output_dir / ".fpsaudio")
        scheduler = Scheduler(
            store=store,
            file_workers=auto_workers("files"),
            encoder_workers=auto_workers("encoders"),
            on_event=lambda kind, payload: self.app.call_from_thread(
                self.on_scheduler_event, kind, payload
            ),
        )
        scheduler.add(specs)
        self._scheduler = scheduler

        def probe(path: Path) -> MediaFile:
            return state.media.get(path) or probe_file(path)

        records = scheduler.run(probe=probe, resume=True)
        self.app.call_from_thread(self.on_batch_finished, records)

    def on_scheduler_event(self, kind: str, payload: dict[str, Any]) -> None:
        table = self.query_one("#queue", DataTable)
        log = self.query_one("#log", RichLog)
        if kind == "batch_start":
            log.write(
                f"Starting {payload['total']} job(s): {payload['workers']} files in "
                f"parallel, {payload['encoder_slots']} encoder slots."
            )
        elif kind == "progress":
            key = self._rows.get(payload["job_id"])
            if key is not None:
                columns = list(table.columns)
                table.update_cell(key, columns[4], f"{payload['percent']:.0f}%")
                table.update_cell(key, columns[5], payload["message"][:40])
        elif kind == "job":
            key = self._rows.get(payload["job_id"])
            if key is not None:
                table.update_cell(key, list(table.columns)[3], payload["state"])
            if payload.get("error"):
                log.write(f"[red]{_escape(payload['error'])}[/red]")
        elif kind == "batch_progress":
            total = max(1, payload["total"])
            self.query_one("#overall", ProgressBar).update(
                progress=payload["completed"] / total * 100
            )

    def on_batch_finished(self, records: list[Any]) -> None:
        log = self.query_one("#log", RichLog)
        self.query_one("#overall", ProgressBar).update(progress=100)
        for record in records:
            if record.state is JobState.DONE:
                log.write(f"[green]done[/green] {record.output}")
            elif record.state is JobState.REFUSED:
                log.write(f"[magenta]refused[/magenta] {_escape(record.error or '')}")
            elif record.state is JobState.FAILED:
                log.write(f"[red]failed[/red] {_escape(record.error or '')}")
            for warning in record.warnings:
                log.write(f"[yellow]! {_escape(warning)}[/yellow]")
            for check in (record.verification or {}).get("checks", ()):
                if check.get("skipped"):
                    log.write(f"  [dim]~ {check['name']}: {check['skip_reason']}[/dim]")
                elif check.get("ok"):
                    log.write(f"  [green]PASS[/green] {check['name']}: {check['detail']}")
                else:
                    log.write(f"  [red]FAIL[/red] {check['name']}: {check['detail']}")

    def action_stop(self) -> None:
        scheduler = getattr(self, "_scheduler", None)
        if scheduler is not None:
            scheduler.stop()
            self.query_one("#log", RichLog).write(
                "[yellow]Stopping after the running jobs finish. "
                "Progress is saved; re-running resumes.[/yellow]"
            )


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

class FpsAudioApp(App):
    TITLE = "fpsaudio"
    SUB_TITLE = "exact audio frame-rate retiming"
    CSS = """
    .title { text-style: bold; padding: 1 0 0 1; }
    .detail { padding: 1 1; color: $text-muted; }
    #body { padding: 0 1; height: 1fr; }
    DataTable { height: 1fr; }
    RichLog { height: 12; border: solid $panel; }
    #plan-scroll { height: 1fr; border: solid $panel; }
    """
    BINDINGS = [Binding("q", "quit", "Quit")]

    def __init__(self) -> None:
        super().__init__()
        self.state = SessionState()

    def on_mount(self) -> None:
        self.push_screen(SourceScreen())


def run() -> None:
    FpsAudioApp().run()
