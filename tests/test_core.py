"""不依赖 JEV 的核心逻辑自测：分词、切段、BM25、扩词、截断。

运行：uv run python -m unittest discover -s tests -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mini_sag_wiki.bm25 import BM25Index
from mini_sag_wiki.pipeline import extract_terms, merge_evidence
from mini_sag_wiki.text import chunk_text, focus_snippet, tokenize


class TokenizeTest(unittest.TestCase):
    def test_chinese_becomes_bigrams(self) -> None:
        self.assertIn("回滚", tokenize("回滚窗口"))
        self.assertNotIn("的了", tokenize("回滚窗口的了"))

    def test_english_is_stemmed_and_stopwords_dropped(self) -> None:
        terms = tokenize("The rollbacks returned and the returning pipeline")
        self.assertIn("rollback", terms)
        self.assertIn("return", terms)
        self.assertNotIn("the", terms)
        self.assertNotIn("and", terms)

    def test_numbers_are_kept(self) -> None:
        self.assertIn("30", tokenize("within 30 days"))


class ChunkTest(unittest.TestCase):
    def test_short_paragraphs_are_merged(self) -> None:
        text = "Short one.\n\nShort two.\n\n" + "Long paragraph. " * 20
        chunks = chunk_text(text, min_chars=40, max_chars=200)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertEqual(chunks[0]["para"], 0)
        self.assertGreaterEqual(chunks[0]["end_para"], 1)

    def test_long_paragraph_is_split(self) -> None:
        text = "Sentence number one. " * 30
        chunks = chunk_text(text, min_chars=40, max_chars=120)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk["text"]), 200)


class BM25Test(unittest.TestCase):
    def setUp(self) -> None:
        self.chunks = [
            {"id": "a#0", "doc": "a", "path": "a.md", "para": 0, "end_para": 0,
             "text": "The rollback window is 30 minutes after a release."},
            {"id": "a#1", "doc": "a", "path": "a.md", "para": 1, "end_para": 1,
             "text": "Refunds are granted within 30 days of the charge."},
            {"id": "b#0", "doc": "b", "path": "b.md", "para": 0, "end_para": 0,
             "text": "API keys expire after 365 days and can be rotated."},
        ]
        self.index = BM25Index.build(self.chunks, kb="data", sources=[])

    def test_relevant_chunk_ranks_first(self) -> None:
        hits = self.index.search({"rollback": 1.0}, k=3)
        self.assertEqual(hits[0]["chunk"]["id"], "a#0")
        self.assertAlmostEqual(self.index.idf("nonexistentterm"), 0.0, places=6)

    def test_exclude_and_neighbors(self) -> None:
        self.assertEqual([c["id"] for c in self.index.neighbors("a#0")], ["a#1"])
        self.assertEqual(self.index.search({"rollback": 1.0}, k=3, exclude={"a#0"}), [])

    def test_save_and_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bm25.json"
            self.index.save(path)
            loaded = BM25Index.load(path)
            self.assertEqual(loaded.stats()["chunks"], 3)
            self.assertEqual(loaded.search({"rollback": 1.0}, k=1)[0]["chunk"]["id"], "a#0")

    def test_missing_index_raises(self) -> None:
        from mini_sag_wiki.bm25 import IndexMissing

        with self.assertRaises(IndexMissing):
            BM25Index.load(Path("does-not-exist/bm25.json"))


class ExtractTermsTest(unittest.TestCase):
    def test_rare_terms_preferred_and_exclusions_hold(self) -> None:
        chunks = [
            {"id": "a#0", "doc": "a", "path": "a.md", "para": 0, "end_para": 0,
             "text": "The rollback window is 30 minutes and the pipeline restores an image."},
            {"id": "a#1", "doc": "a", "path": "a.md", "para": 1, "end_para": 1,
             "text": "Refunds are granted within 30 days of the charge."},
        ]
        index = BM25Index.build(chunks, kb="data", sources=[])
        picked = extract_terms(
            chunks[0]["text"],
            index.idf,
            exclude={"rollback"},
            top=5,
            min_idf=0.0,
        )
        self.assertNotIn("rollback", picked)
        self.assertNotIn("30", picked)  # 纯数字不当线索词
        self.assertTrue(picked)


class MergeTest(unittest.TestCase):
    def test_adjacent_chunks_merge_up_to_the_cap(self) -> None:
        def item(para: int, text: str, p: float) -> dict:
            return {
                "id": f"a#{para}", "doc": "a", "source": "a.md", "para": para,
                "end_para": para, "text": text, "p": p, "bm25": 1.0,
                "jev": {"useful": p, "info": p}, "via": "seed:x", "round": 1,
            }

        merged = merge_evidence(
            [
                item(0, "zero", 0.6),
                item(1, "one", 0.9),
                item(2, "two", 0.5),
                item(3, "three", 0.5),
                item(9, "far", 0.7),
            ],
            max_parts=3,
        )
        self.assertEqual(len(merged), 3)
        self.assertEqual(merged[0]["id"], "a#0-2")
        self.assertEqual(merged[0]["p"], 0.9)
        self.assertEqual(merged[0]["via"], "seed:x")
        self.assertEqual(merged[1]["id"], "a#3")
        self.assertEqual(merged[2]["id"], "a#9")


class SnippetTest(unittest.TestCase):
    def test_window_centers_on_the_match(self) -> None:
        text = (
            "Background sentence about canaries and traffic. "
            "Another sentence about latency budgets and alerts. "
            "The rollback window is 30 minutes. "
            "A trailing sentence about approvals and change tickets."
        )
        out = focus_snippet(text, {"rollback", "window"}, 90)
        self.assertIn("rollback window is 30 minutes", out)
        self.assertLess(len(out), len(text))


if __name__ == "__main__":
    unittest.main()
