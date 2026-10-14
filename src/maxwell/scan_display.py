import os
import shutil
import sys
import threading
import time

from maxwell.cli_output import color


class ScanDisplay:
    def __init__(self, stream=None):
        self.lock = threading.RLock()
        self.stream = sys.stdout if stream is None else stream
        self.interactive = self.stream.isatty() and os.environ.get("TERM") != "dumb"
        self.refresh_interval = 0.12 if self.interactive else 15
        self.stage_fraction = None
        self.repository = None
        self.stages = []
        self.statuses = {}
        self.stage = None
        self.completed = 0
        self.total = None
        self.phase = ""
        self.agent = ""
        self.started = time.monotonic()
        self.rendered_lines = 0
        self.last_wait = None

    def __call__(self, event):
        with self.lock:
            self.update(event)

    def update(self, event):
        state = event["state"]
        if state == "plan":
            if self.repository:
                self.finish()
            self.stage = None
            self.stage_fraction = None
            self.repository = event["repository"]
            self.stages = event["stages"]
            self.statuses = {}
            self.started = time.monotonic()
            self.last_wait = None
            return
        if state == "stage":
            self.stage = event["stage"]
            self.stage_fraction = None
            self.statuses[self.stage] = event["status"]
            self.total = event.get("total")
            self.completed = event.get("completed", 0)
            self.phase = event.get("detail", "")
            self.agent = ""
            if self.interactive or event["status"] == "running":
                self.render()
            return
        if state == "batch":
            self.total = event["total"]
            self.completed = event["completed"]
        elif state == "progress":
            self.phase = event["phase"]
            if "stage_fraction" in event:
                self.stage_fraction = event["stage_fraction"]
            if (
                any(word in self.phase.lower() for word in ("retry", "waiting for github"))
                and self.phase != self.last_wait
            ):
                self.last_wait = self.phase
                self.clear()
                self.write(f"{self.repository}: {self.phase}")
            if not self.interactive:
                return
        elif state == "running":
            self.agent = "/".join(filter(None, [event.get("model"), event.get("effort")]))
        elif state == "publishing":
            self.phase = "publishing to GitHub"
        else:
            return
        if self.interactive:
            self.render()

    def write(self, text):
        print(text, file=self.stream, flush=True)

    def progress(self):
        done = sum(status != "running" for status in self.statuses.values())
        fraction = (
            self.stage_fraction
            if self.stage_fraction is not None
            else self.completed / self.total
            if self.total
            else 0
        )
        return min(
            1,
            (done + (fraction if self.statuses.get(self.stage) == "running" else 0))
            / max(1, len(self.stages)),
        )

    def clear(self):
        if self.rendered_lines:
            self.stream.write(f"\033[{self.rendered_lines}A\r\033[0J")
            self.rendered_lines = 0

    def panel(self, elapsed):
        width = max(4, min(100, shutil.get_terminal_size((100, 24)).columns - 1))
        content_width = width - 4

        def styled(text, code):
            return color(text, code, self.stream)

        def clipped(text, available):
            text = text.replace("\n", " ").replace("\r", " ")
            if len(text) <= available:
                return text
            return text[: max(0, available - 1)] + ("…" if available else "")

        def row(parts):
            available = content_width
            values = []
            for text, code in parts:
                piece = clipped(text, available)
                values.append(styled(piece, code))
                available -= len(piece)
                if not available:
                    break
            return (
                styled("│", "2") + " " + "".join(values) + " " * available + " " + styled("│", "2")
            )

        title = clipped(f" Maxwell · {self.repository} ", width - 2)
        top = (
            styled("╭", "2")
            + styled(title, "1;36")
            + styled("─" * (width - 2 - len(title)) + "╮", "2")
        )
        stages = []
        for index, stage in enumerate(self.stages):
            if index:
                stages.append((" › ", "2"))
            status = self.statuses.get(stage)
            label = (
                {"measure": "Meas", "hunt": "Hunt", "review": "Rev", "audit": "Aud"}.get(
                    stage, stage.capitalize()
                )
                if content_width < 45
                else stage.capitalize()
            )
            if status == "completed":
                label, code = f"✓ {label}", "32"
            elif status == "failed":
                label, code = f"✗ {label}", "1;31"
            elif status == "skipped":
                label, code = f"– {label}", "2"
            elif status == "running":
                code = {"measure": "1;33", "hunt": "1;36", "review": "1;35", "audit": "1;32"}.get(
                    stage, "1;36"
                )
                if stage in ("review", "audit") and self.total:
                    label += f" {self.completed}/{self.total}"
            else:
                code = "2"
            stages.append((label, code))
        percentage = int(100 * self.progress())
        percentage_label = f" {percentage:3d}%"
        bar_width = max(0, content_width - len(percentage_label))
        filled = int(bar_width * self.progress())
        active = self.statuses.get(self.stage) == "running"
        if active and filled < bar_width:
            position = filled + int((time.monotonic() - self.started) * 8) % (bar_width - filled)
            bar = [
                ("━" * filled, "32"),
                ("━" * (position - filled), "90"),
                ("━", "1;36"),
                ("━" * (bar_width - position - 1), "90"),
                (percentage_label, "2"),
            ]
        else:
            bar = [
                ("━" * filled, "32"),
                ("━" * (bar_width - filled), "90"),
                (percentage_label, "2"),
            ]
        failed = any(status == "failed" for status in self.statuses.values())
        active = self.statuses.get(self.stage) == "running"
        if active or self.stage is None:
            activity = " · ".join(filter(None, [self.agent, self.phase]))
        elif failed:
            activity = "Finished with errors"
        elif len(self.statuses) == len(self.stages):
            activity = "Completed"
        else:
            activity = f"{self.stage.capitalize()} complete"
        activity = activity or "Preparing"
        if active:
            spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int((time.monotonic() - self.started) * 8) % 10]
            activity = f"{spinner} {activity}"
        available = max(0, content_width - len(elapsed) - 2)
        activity = clipped(activity, available)
        detail = [
            (activity, "33" if failed else "2"),
            (" " * max(0, content_width - len(activity) - len(elapsed)), "2"),
            (elapsed, "2"),
        ]
        return [top, row(stages), row(bar), row(detail), styled("╰" + "─" * (width - 2) + "╯", "2")]

    def render(self):
        elapsed = int(time.monotonic() - self.started)
        minutes, seconds = divmod(elapsed, 60)
        elapsed = f"{minutes}:{seconds:02d}"
        if self.interactive:
            self.clear()
            lines = self.panel(elapsed)
            self.write("\n".join(lines))
            self.rendered_lines = len(lines)
        else:
            filled = int(12 * self.progress())
            bar = "#" * filled + "-" * (12 - filled)
            labels = []
            for stage in self.stages:
                status = self.statuses.get(stage)
                marker = {"completed": "+", "failed": "!", "skipped": "-", "running": ">"}.get(
                    status, "·"
                )
                count = (
                    f" {self.completed}/{self.total}"
                    if stage in ("review", "audit")
                    and stage == self.stage
                    and self.total is not None
                    else ""
                )
                labels.append(f"{stage.capitalize()}{marker}{count}")
            self.write(f"{self.repository} [{bar}] {'  '.join(labels)}  {elapsed}")

    def finish(self):
        with self.lock:
            self.finalize()

    def finalize(self):
        if self.repository:
            if self.statuses.get(self.stage) == "running":
                self.statuses[self.stage] = "failed"
            self.phase = ""
            self.agent = ""
            self.render()
            self.rendered_lines = 0
