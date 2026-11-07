"""Gateway routing (pqv/gateway.py): explorer at /, each wallet under /<name>/, the first also at /wallet and for
bare /api/ paths, /<name> redirected to /<name>/ so the page's relative URLs resolve."""
import unittest

from pqv import gateway as g


class Route(unittest.TestCase):
    def setUp(self):
        g.UP.clear()
        g.UP.update(explorer=8300, wallets={"alice": 8201, "bob": 8202, "carol": 8203}, first="alice")

    def test_explorer(self):
        self.assertEqual(g.route("/"), (8300, "/", None))
        self.assertEqual(g.route("/api/tx/0xabc"), (8300, "/api/tx/0xabc", None))
        self.assertEqual(g.route("/api/search?q=alice"), (8300, "/api/search?q=alice", None))

    def test_named_wallets(self):
        self.assertEqual(g.route("/bob/"), (8202, "/", None))
        self.assertEqual(g.route("/bob/api/state"), (8202, "/api/state", None))
        self.assertEqual(g.route("/carol/api/jobs/12ab"), (8203, "/api/jobs/12ab", None))
        self.assertEqual(g.route("/carol/icons/icon-192.png"), (8203, "/icons/icon-192.png", None))
        self.assertEqual(g.route("/bob"), (None, None, "/bob/"))

    def test_first_wallet_aliases(self):
        self.assertEqual(g.route("/wallet/"), (8201, "/", None))
        self.assertEqual(g.route("/wallet/api/send"), (8201, "/api/send", None))
        self.assertEqual(g.route("/wallet"), (None, None, "/wallet/"))
        self.assertEqual(g.route("/api/state"), (8201, "/api/state", None))  # phone app on the bare domain

    def test_misc(self):
        self.assertEqual(g.route("/healthz")[0], "health")
        self.assertEqual(g.route("/sw.js"), (None, None, None))
        self.assertEqual(g.route("/dave/api/state"), (8201, "/dave/api/state", None))  # unknown name: not a wallet


if __name__ == "__main__":
    unittest.main()
