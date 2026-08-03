import shutil
import tempfile
import unittest
from pathlib import Path

from hsaids.cdc_hsaids import CDCConfig, ChunkLevelHSAIDS, iter_cdc_chunks


class ChunkLevelHSAIDSTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cdc_hsaids_test_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cdc_produces_multiple_variable_chunks(self):
        path = self.tmp / "sample.bin"
        path.write_bytes((b"abcdef0123456789" * 4096) + (b"tail" * 128))
        chunks = list(iter_cdc_chunks(path, CDCConfig(min_size=512, avg_size=1024, max_size=4096)))

        self.assertGreater(len(chunks), 1)
        self.assertEqual(sum(chunk.size for chunk in chunks), path.stat().st_size)
        self.assertNotEqual(len({chunk.size for chunk in chunks}), 1)

    def test_identical_files_are_deduplicated_as_chunks(self):
        a = self.tmp / "a.bin"
        b = self.tmp / "b.bin"
        payload = b"shared payload block\n" * 10000
        a.write_bytes(payload)
        b.write_bytes(payload)

        engine = ChunkLevelHSAIDS(
            store_dir=self.tmp / "store",
            cdc_config=CDCConfig(min_size=512, avg_size=1024, max_size=4096),
        )
        try:
            first = engine.ingest_file(a)
            second = engine.ingest_file(b)
            stats = engine.statistics()
        finally:
            engine.close()

        self.assertGreater(first["chunk_count"], 1)
        self.assertEqual(first["chunk_count"], second["chunk_count"])
        self.assertGreater(stats["duplicate_chunks_detected"], 0)
        self.assertGreater(stats["duplicate_chunk_references"], 0)
        self.assertGreater(stats["dedup_ratio"], 1.0)

    def test_shifted_files_reuse_some_content_defined_chunks(self):
        base = b"0123456789abcdef" * 4096
        shared = b"common-region-" * 20000
        a = self.tmp / "a.bin"
        b = self.tmp / "b.bin"
        a.write_bytes(base + shared + base)
        b.write_bytes(b"prefix-shift" + base + shared + base)

        engine = ChunkLevelHSAIDS(
            store_dir=self.tmp / "store_shifted",
            cdc_config=CDCConfig(min_size=512, avg_size=1024, max_size=4096),
        )
        try:
            engine.ingest_file(a)
            engine.ingest_file(b)
            stats = engine.statistics()
        finally:
            engine.close()

        self.assertGreater(stats["total_chunks_processed"], 2)
        self.assertGreater(stats["duplicate_chunks_detected"], 0)
        self.assertGreater(stats["avg_chunk_size"], 0)
        self.assertGreaterEqual(stats["cold_index_waf"], 0)

    def test_deleting_recipe_allows_gc_to_reclaim_unique_chunks(self):
        path = self.tmp / "single.bin"
        path.write_bytes(b"unique-data-" * 10000)

        engine = ChunkLevelHSAIDS(
            store_dir=self.tmp / "store_gc",
            cdc_config=CDCConfig(min_size=512, avg_size=1024, max_size=4096),
        )
        try:
            summary = engine.ingest_file(path)
            engine.delete_file_recipe(summary["file_id"])
            gc_stats = engine.garbage_collect()
        finally:
            engine.close()

        self.assertGreater(gc_stats["reclaimed_chunks"], 0)
        self.assertGreater(gc_stats["reclaimed_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
