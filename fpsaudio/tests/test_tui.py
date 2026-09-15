"""TUI tests, driven headlessly through Textual's pilot.

These exist because a TUI defect is invisible to every other kind of test. The
first version of :class:`PlanScreen` defined a method called ``_render``, which
shadowed ``textual.widget.Widget._render`` and made the screen render ``None``
— the plan screen crashed the instant it was opened, while every unit test
still passed.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

try:
    import textual  # noqa: F401

    HAVE_TEXTUAL = True
except ImportError:  # pragma: no cover
    HAVE_TEXTUAL = False

from fpsaudio.core.adapters import get_registry

REGISTRY = get_registry()


def have(*names: str) -> bool:
    return all(
        (REGISTRY.get(n) is not None and REGISTRY.get(n).detect().found) for n in names
    )


def static_content(widget) -> str:
    """Read a Static's content without going through the render pipeline."""
    return str(getattr(widget, "_Static__content", ""))


@unittest.skipUnless(HAVE_TEXTUAL, "textual is not installed")
class TestNoTextualInternalsAreShadowed(unittest.TestCase):
    """Screens must not define methods that collide with Textual's own API.

    This is a whole-class-of-bug guard: overriding a private Textual method
    silently breaks rendering rather than raising anything obvious.
    """

    def test_screen_methods_do_not_shadow_widget_internals(self) -> None:
        from textual.screen import Screen
        from textual.widget import Widget

        from fpsaudio.tui import app as tui

        screens = [
            tui.SourceScreen,
            tui.RetimeScreen,
            tui.OutputScreen,
            tui.PlanScreen,
            tui.RunScreen,
        ]
        # Names Textual itself defines and calls internally.
        reserved = {
            name
            for base in (Widget, Screen)
            for name in vars(base)
            if name.startswith("_") and callable(vars(base)[name])
        }

        for screen in screens:
            ours = {
                name
                for name, value in vars(screen).items()
                if callable(value) and not name.startswith("__")
            }
            collisions = ours & reserved
            self.assertEqual(
                collisions,
                set(),
                f"{screen.__name__} overrides Textual internals: {sorted(collisions)}",
            )


@unittest.skipUnless(HAVE_TEXTUAL, "textual is not installed")
class TestWizardFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from fpsaudio.core.dsp import synth

        cls.dir = Path(tempfile.mkdtemp(prefix="fpsaudio_tui_"))
        cls.inputs = cls.dir / "in"
        cls.inputs.mkdir()
        synth.write(
            cls.inputs / "stereo.wav",
            synth.sine(0.5, 48000, 997.0, channels=2),
            48000,
            subtype="PCM_24",
        )

    def _drive(self, coro):
        return asyncio.run(coro)

    def test_every_screen_renders_and_the_plan_is_shown_before_running(self) -> None:
        from fpsaudio.tui.app import FpsAudioApp, OutputScreen, PlanScreen, RetimeScreen

        inputs = self.inputs
        out = self.dir / "out"

        async def run() -> dict:
            app = FpsAudioApp()
            found: dict = {}
            async with app.run_test(size=(120, 44)) as pilot:
                await pilot.pause()
                app.screen.query_one("#path").value = str(inputs)
                await pilot.press("enter")
                for _ in range(80):
                    await pilot.pause(0.05)
                    if app.screen.query_one("#streams").row_count >= 1:
                        break
                found["rows"] = app.screen.query_one("#streams").row_count

                await pilot.press("a")
                await pilot.pause()
                found["selected"] = len(app.state.selected)

                await pilot.press("n")
                await pilot.pause()
                found["screen2"] = type(app.screen).__name__
                found["ratio_panel"] = static_content(
                    app.screen.query_one("#ratio-detail")
                )

                await pilot.press("n")
                await pilot.pause()
                found["screen3"] = type(app.screen).__name__

                app.state.output_dir = out
                app.state.codec = "flac"
                await pilot.press("n")
                await pilot.pause(0.3)
                found["screen4"] = type(app.screen).__name__
                found["plan"] = static_content(app.screen.query_one("#plan-body"))
                found["status"] = static_content(app.screen.query_one("#plan-status"))
            return found

        result = self._drive(run())

        self.assertGreaterEqual(result["rows"], 1)
        self.assertGreaterEqual(result["selected"], 1)
        self.assertEqual(result["screen2"], RetimeScreen.__name__)
        # The exact ratio must be on screen, not a rounded percentage.
        self.assertIn("1001/960", result["ratio_panel"])
        self.assertEqual(result["screen3"], OutputScreen.__name__)
        self.assertEqual(result["screen4"], PlanScreen.__name__)

        plan = result["plan"]
        self.assertIn("Steps:", plan)
        self.assertIn("Retime", plan)
        self.assertIn("Verify:", plan)
        self.assertIn("job(s) ready", result["status"])

    def test_stretch_method_warns_that_it_is_the_wrong_operation(self) -> None:
        """The TUI must say so, not just the docs."""
        from fpsaudio.core.contracts import RetimeMethod
        from fpsaudio.tui.app import FpsAudioApp, RetimeScreen

        async def run() -> str:
            app = FpsAudioApp()
            async with app.run_test(size=(120, 44)) as pilot:
                await pilot.pause()
                app.state.method = RetimeMethod.STRETCH
                await app.push_screen(RetimeScreen())
                await pilot.pause()
                app.screen.update_ratio_panel()
                await pilot.pause()
                return static_content(app.screen.query_one("#ratio-detail"))

        panel = self._drive(run())
        self.assertIn("NOT what happens", panel)

    def test_stepping_back_does_not_reset_choices(self) -> None:
        """A Select fires Changed on mount.

        With hardcoded initial values that overwrote the session state, walking
        back from Output to Retime silently reset the preset and method to the
        defaults — the user's choice vanished with no indication.
        """
        from fpsaudio.core.contracts import RetimeMethod
        from fpsaudio.tui.app import FpsAudioApp, RetimeScreen

        async def run() -> tuple[str, str]:
            app = FpsAudioApp()
            async with app.run_test(size=(120, 44)) as pilot:
                await pilot.pause()
                app.state.preset_key = "25_to_24"
                app.state.method = RetimeMethod.REDECLARE
                app.state.codec = "wavpack"

                await app.push_screen(RetimeScreen())
                await pilot.pause()
                # Mounting the screen must not have overwritten the session.
                return app.state.preset_key, app.state.method.value

        preset, method = self._drive(run())
        self.assertEqual(preset, "25_to_24")
        self.assertEqual(method, "redeclare")

    @unittest.skipUnless(have("ffmpeg", "flac"), "ffmpeg and flac are required")
    def test_the_run_screen_completes_a_real_job(self) -> None:
        from fpsaudio.tui.app import FpsAudioApp, RunScreen

        inputs = self.inputs
        out = self.dir / "out_run"

        async def run() -> set:
            app = FpsAudioApp()
            async with app.run_test(size=(120, 44)) as pilot:
                await pilot.pause()
                app.screen.query_one("#path").value = str(inputs)
                await pilot.press("enter")
                for _ in range(80):
                    await pilot.pause(0.05)
                    if app.screen.query_one("#streams").row_count >= 1:
                        break
                await pilot.press("a")
                await pilot.pause()
                await pilot.press("n")
                await pilot.pause()
                await pilot.press("n")
                await pilot.pause()
                app.state.output_dir = out
                app.state.codec = "flac"
                await pilot.press("n")
                await pilot.pause(0.3)
                await pilot.press("r")
                await pilot.pause(0.3)
                self.assertIsInstance(app.screen, RunScreen)

                for _ in range(200):
                    await pilot.pause(0.05)
                    scheduler = getattr(app.screen, "_scheduler", None)
                    if scheduler and scheduler.records and all(
                        r.state.value in ("done", "failed", "refused", "skipped")
                        for r in scheduler.records
                    ):
                        return {r.state.value for r in scheduler.records}
                return set()

        states = self._drive(run())
        self.assertEqual(states, {"done"})
        self.assertTrue(any(out.glob("*.flac")))


if __name__ == "__main__":
    unittest.main()
