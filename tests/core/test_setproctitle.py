import unittest

from hydrangea.util.setproctitle import setproctitle


class TestSetProcTitle(unittest.TestCase):
    def test_does_not_crash(self):
        setproctitle("hydrangea test title")
