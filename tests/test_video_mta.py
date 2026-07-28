"""MTA-апартамент должен переживать остановку записи.

Без вечного потока в MTA второй сеанс захвата в том же процессе падает с
access violation внутри windows_capture.pyd и уносит приложение целиком
(см. _keep_mta_alive).
"""
from __future__ import annotations

import threading
import unittest

from app.recorder import video


def _keepers() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "mta-keeper"]


class MtaKeeperTests(unittest.TestCase):
    def test_keeper_is_started_once_and_stays_alive(self) -> None:
        video._keep_mta_alive()
        video._keep_mta_alive()  # повторный вызов не плодит потоки

        keepers = _keepers()
        self.assertEqual(len(keepers), 1)
        self.assertTrue(keepers[0].is_alive())
        self.assertTrue(keepers[0].daemon)

    def test_recorder_start_arms_the_keeper(self) -> None:
        """Держатель обязан подниматься до первого сеанса захвата."""
        import inspect

        self.assertIn("_keep_mta_alive()",
                      inspect.getsource(video.VideoRecorder.start))


if __name__ == "__main__":
    unittest.main()
