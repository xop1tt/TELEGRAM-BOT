"""Supervisor: keep-alive, сигналы, зависший heartbeat; ресурсы процесса."""

import io
import threading
import unittest
import urllib.error
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from flamenco_bot import supervisor
from flamenco_bot.__main__ import run
from flamenco_bot.runtime import runtime_resources


class Response:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class PingTests(unittest.TestCase):
    def test_returns_http_status(self):
        with patch("urllib.request.urlopen", return_value=Response(200)):
            self.assertEqual(supervisor.ping("https://x.example/api/health"), 200)

    def test_http_error_still_reports_its_status(self):
        error = urllib.error.HTTPError("u", 503, "down", {}, io.BytesIO(b""))
        with patch("urllib.request.urlopen", side_effect=error):
            self.assertEqual(supervisor.ping("https://x.example"), 503)

    def test_network_failure_is_none_not_an_exception(self):
        for failure in (urllib.error.URLError("dns"), TimeoutError(), OSError("x")):
            with patch("urllib.request.urlopen", side_effect=failure):
                self.assertIsNone(supervisor.ping("https://x.example"))


class KeepaliveLoopTests(unittest.TestCase):
    def test_pings_until_stopped_and_warns_about_failures(self):
        stop = threading.Event()
        calls = []

        def request(url):
            calls.append(url)
            if len(calls) == 3:
                stop.set()
            return 200 if len(calls) == 1 else 502

        with self.assertLogs("bot.supervisor", level="WARNING") as logs:
            supervisor.run_keepalive("https://x.example/h", 0.001, stop, request)
        self.assertEqual(calls, ["https://x.example/h"] * 3)
        self.assertEqual(len(logs.records), 2)
        self.assertIn("502", logs.output[0])

    def test_already_stopped_never_pings(self):
        stop = threading.Event()
        stop.set()
        request = MagicMock()
        supervisor.run_keepalive("https://x.example", 60, stop, request)
        request.assert_not_called()


class KeepaliveIntervalTests(unittest.TestCase):
    def test_default_zero_and_bounds(self):
        self.assertEqual(
            supervisor.keepalive_interval({}),
            supervisor.DEFAULT_KEEPALIVE_INTERVAL_SECONDS,
        )
        self.assertEqual(
            supervisor.keepalive_interval({"KEEPALIVE_INTERVAL_SECONDS": "0"}), 0
        )
        for good in ("60", "840"):
            self.assertEqual(
                supervisor.keepalive_interval({"KEEPALIVE_INTERVAL_SECONDS": good}),
                int(good),
            )

    def test_values_that_would_let_render_sleep_are_rejected(self):
        for bad in ("59", "841", "-1", "abc", "1.5"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                supervisor.keepalive_interval({"KEEPALIVE_INTERVAL_SECONDS": bad})

    def test_url_is_absent_without_render_or_explicit_setting(self):
        self.assertIsNone(supervisor.keepalive_url({}))
        self.assertIsNone(supervisor.keepalive_url({"RENDER_EXTERNAL_URL": "  "}))


class SuperviseTests(unittest.TestCase):
    def test_stale_heartbeat_after_grace_restarts_the_container(self):
        sleeper = [__import__("sys").executable, "-c", "import time; time.sleep(60)"]
        with TemporaryDirectory() as directory:
            heartbeat = Path(directory) / "beat"
            ticks = iter(range(0, 10_000, 100))
            with self.assertLogs("bot.supervisor", level="ERROR") as logs:
                code = supervisor.supervise(
                    {"bot": sleeper},
                    {},
                    heartbeat,
                    threading.Event(),
                    poll_seconds=0.01,
                    startup_grace_seconds=250,
                    clock=lambda: next(ticks),
                )
        self.assertEqual(code, 1)
        self.assertIn("heartbeat is stale", logs.output[0])

    def test_old_heartbeat_file_from_previous_run_is_removed(self):
        with TemporaryDirectory() as directory:
            heartbeat = Path(directory) / "beat"
            heartbeat.write_text("old")
            stop = threading.Event()
            stop.set()
            supervisor.supervise(
                {"bot": [__import__("sys").executable, "-c", "pass"]},
                {},
                heartbeat,
                stop,
            )
            self.assertFalse(heartbeat.exists())

    def test_stop_signal_gives_exit_code_zero(self):
        stop = threading.Event()
        stop.set()
        code = supervisor.supervise(
            {
                "bot": [
                    __import__("sys").executable,
                    "-c",
                    "import time; time.sleep(60)",
                ]
            },
            {},
            None,
            stop,
        )
        self.assertEqual(code, 0)

    def test_stuck_child_is_killed_after_timeout(self):
        process = MagicMock()
        process.poll.return_value = None
        process.wait.side_effect = [
            __import__("subprocess").TimeoutExpired("cmd", 0),
            None,
        ]
        with self.assertLogs("bot.supervisor", level="WARNING"):
            supervisor.stop_processes({"bot": process}, timeout_seconds=0)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()


class MainTests(unittest.TestCase):
    def _main(self, env, supervise_result=0):
        with (
            patch.dict("os.environ", env, clear=True),
            patch.object(
                supervisor, "supervise", return_value=supervise_result
            ) as supervise,
            patch("signal.signal") as register,
            patch("threading.Thread") as thread,
        ):
            code = supervisor.main()
        return code, supervise, register, thread

    def test_starts_both_processes_and_handles_termination_signals(self):
        code, supervise, register, thread = self._main({"PORT": "10000"})
        self.assertEqual(code, 0)
        self.assertEqual(
            {call.args[0] for call in register.call_args_list},
            {__import__("signal").SIGTERM, __import__("signal").SIGINT},
        )
        thread.assert_not_called()  # без RENDER_EXTERNAL_URL keep-alive выключен
        commands, env, heartbeat, _ = supervise.call_args.args
        self.assertEqual(set(commands), {"bot", "api"})
        self.assertEqual(env["API_PORT"], "10000")
        self.assertEqual(str(heartbeat), supervisor.DEFAULT_HEARTBEAT_FILE)

    def test_keepalive_thread_starts_on_render(self):
        _, _, _, thread = self._main(
            {"RENDER_EXTERNAL_URL": "https://studio.onrender.com"}
        )
        thread.assert_called_once()
        self.assertEqual(
            thread.call_args.kwargs["args"][:2],
            (
                "https://studio.onrender.com/api/health",
                supervisor.DEFAULT_KEEPALIVE_INTERVAL_SECONDS,
            ),
        )
        thread.return_value.start.assert_called_once()

    def test_failed_child_propagates_a_nonzero_exit_code(self):
        code, *_ = self._main({}, supervise_result=1)
        self.assertEqual(code, 1)

    def test_invalid_interval_stops_startup(self):
        with (
            patch.dict("os.environ", {"KEEPALIVE_INTERVAL_SECONDS": "5"}, clear=True),
            self.assertRaises(ValueError),
        ):
            supervisor.main()

    def test_signal_handler_sets_the_stop_event(self):
        captured = {}

        def register(signum, handler):
            captured[signum] = handler

        stop_events = []

        def fake_supervise(commands, env, heartbeat, stop):
            stop_events.append(stop)
            return 0

        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(supervisor, "supervise", fake_supervise),
            patch("signal.signal", register),
        ):
            supervisor.main()
        handler = captured[__import__("signal").SIGTERM]
        stop_events[0].clear()
        handler(15, None)
        self.assertTrue(stop_events[0].is_set())


class EntryPointTests(unittest.TestCase):
    def test_module_entry_point_runs_the_async_main(self):
        with (
            patch("flamenco_bot.__main__.main") as main,
            patch("asyncio.run") as run_loop,
        ):
            run()
        main.assert_called_once_with()
        run_loop.assert_called_once()


class ProcessResourcesTests(unittest.TestCase):
    def _usage(self, platform, rss):
        usage = MagicMock(ru_maxrss=rss, ru_utime=1.5, ru_stime=0.5)
        with (
            patch("resource.getrusage", return_value=usage),
            patch.object(runtime_resources.sys, "platform", platform),
        ):
            return runtime_resources.get_process_resources()

    def test_linux_reports_kilobytes_as_megabytes(self):
        memory, cpu = self._usage("linux", 51200)
        self.assertEqual(memory, 50.0)
        self.assertEqual(cpu, 2.0)

    def test_macos_reports_bytes_as_megabytes(self):
        memory, _ = self._usage("darwin", 52428800)
        self.assertEqual(memory, 50.0)

    def test_real_process_has_positive_usage(self):
        memory, cpu = runtime_resources.get_process_resources()
        self.assertGreater(memory, 0)
        self.assertGreaterEqual(cpu, 0)


if __name__ == "__main__":
    unittest.main()
