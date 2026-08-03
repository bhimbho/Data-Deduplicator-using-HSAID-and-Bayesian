import shutil
import tempfile
import unittest
from pathlib import Path

from hsaids.cdc_hsaids import CDCConfig
from comparisons.baselines.baseline_hash_dedup import NaiveHashDedup


class NaiveHashDedupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="naive_hash_dedup_test_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_identical_files_are_deduplicated_as_chunks(self):
        a = self.tmp / "a.bin"
        b = self.tmp / "b.bin"
        payload = b"shared payload block\n" * 10000
        a.write_bytes(payload)
        b.write_bytes(payload)

        engine = NaiveHashDedup(
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
        self.assertGreater(stats["dedup_ratio"], 1.0)

    def test_logical_input_bytes_correct_via_insert_chunk_directly(self):
        from hsaids.cdc_hsaids import Chunk

        engine = NaiveHashDedup(store_dir=self.tmp / "store_direct")
        try:
            engine.insert_chunk(Chunk(file_offset=0, size=4, digest="aaaa", data=b"aaaa"))
            engine.insert_chunk(Chunk(file_offset=0, size=4, digest="aaaa", data=b"aaaa"))
            engine.insert_chunk(Chunk(file_offset=0, size=5, digest="bbbb", data=b"bbbbb"))
            stats = engine.statistics()
        finally:
            engine.close()

        self.assertEqual(stats["logical_input_bytes"], 13)
        self.assertEqual(stats["unique_chunk_bytes"], 9)
        self.assertEqual(stats["duplicate_chunks_detected"], 1)

    def test_lookup_latency_percentiles_are_reported(self):
        path = self.tmp / "single.bin"
        path.write_bytes(b"unique-data-" * 10000)

        engine = NaiveHashDedup(
            store_dir=self.tmp / "store_latency",
            cdc_config=CDCConfig(min_size=512, avg_size=1024, max_size=4096),
        )
        try:
            engine.ingest_file(path)
            stats = engine.statistics()
        finally:
            engine.close()

        self.assertGreaterEqual(stats["lookup_latency_p95_ns"], stats["lookup_latency_p50_ns"])
        self.assertGreaterEqual(stats["lookup_latency_p99_ns"], stats["lookup_latency_p95_ns"])


if __name__ == "__main__":
    unittest.main()
