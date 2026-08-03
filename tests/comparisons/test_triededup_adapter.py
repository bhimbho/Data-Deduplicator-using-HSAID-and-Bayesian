import hashlib
import unittest

from comparisons.external import triededup_adapter


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class TrieDedupAdapterTest(unittest.TestCase):
    def test_trie_dedup_matches_expected_unique_count(self):
        payloads = [b"a", b"b", b"a", b"c", b"b", b"d"]
        hashes = [_digest(p) for p in payloads]

        result = triededup_adapter.dedup_trie(hashes)

        self.assertEqual(result["unique_count"], len(set(hashes)))
        self.assertEqual(result["duplicate_count"], len(hashes) - len(set(hashes)))

    def test_pairwise_dedup_matches_expected_unique_count(self):
        payloads = [b"a", b"b", b"a", b"c", b"b", b"d"]
        hashes = [_digest(p) for p in payloads]

        result = triededup_adapter.dedup_pairwise(hashes)

        self.assertEqual(result["unique_count"], len(set(hashes)))
        self.assertEqual(result["duplicate_count"], len(hashes) - len(set(hashes)))

    def test_trie_and_pairwise_agree(self):
        payloads = [f"payload-{i % 7}".encode() for i in range(50)]
        hashes = [_digest(p) for p in payloads]

        trie_result = triededup_adapter.dedup_trie(hashes)
        pairwise_result = triededup_adapter.dedup_pairwise(hashes)

        self.assertEqual(trie_result["unique_count"], pairwise_result["unique_count"])

    def test_no_duplicates_when_all_hashes_distinct(self):
        hashes = [_digest(str(i).encode()) for i in range(20)]

        result = triededup_adapter.dedup_trie(hashes)

        self.assertEqual(result["unique_count"], 20)
        self.assertEqual(result["duplicate_count"], 0)


if __name__ == "__main__":
    unittest.main()
