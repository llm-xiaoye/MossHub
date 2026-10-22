"""Compute ordering changes must preserve every child identity and duplicate mapping."""
import unittest
from unittest.mock import patch
from raghub.indexing.vector_cache import document_vectors


class BatchClient:
    def __init__(self, wrong_order=False):
        self.calls = []
        self.wrong_order = wrong_order
    def embed(self, items, *, owner_id, operation_id, fence):
        self.calls.append(items)
        response = [{"item_id": i["item_id"], "vector": i["text"]} for i in items]
        if self.wrong_order:
            response.reverse()
        return {"operation": {"operation_id": operation_id}, "items": response}


class DocumentEmbeddingBatchTest(unittest.TestCase):
    def children(self):
        sizes = [512, 20, 129, 390, 512, 325, 371, 124, 30, 1024]
        children = [{"chunk_id": str(i), "text": "text-" + str(i), "text_sha256": str(i),
                     "token_count": count, "input_kind": "document_extended" if count > 512 else "document_standard"}
                    for i, count in enumerate(sizes)]
        children.append({**children[2], "chunk_id": "duplicate"})
        return children

    def run_batches(self, client):
        def key(profile, kind, text):
            return kind + ":" + text
        def cached(db, settings, profile, child):
            return key(profile, child["input_kind"], child["text"]), None
        def persist(db, settings, profile, child, values, fence):
            return {"vector": values}
        with patch("raghub.indexing.vector_cache.vector_key", side_effect=key), \
             patch("raghub.indexing.vector_cache.cached_vector", side_effect=cached), \
             patch("raghub.indexing.vector_cache.persist_vector", side_effect=persist):
            return document_vectors(None, None, {}, self.children(), client, "owner",
                                    lambda: None, lambda stage, details: None)

    def test_small_stable_length_batches_keep_all_child_and_duplicate_bindings(self):
        client = BatchClient()
        vectors, stats = self.run_batches(client)
        self.assertTrue(all(1 <= len(batch) <= 4 for batch in client.calls))
        expected = [str(i) for i in sorted(range(9), key=lambda i: self.children()[i]["token_count"])] + ["9"]
        observed = [i["item_id"] for batch in client.calls for i in batch]
        self.assertEqual(observed, expected)
        self.assertEqual(len(observed), len(set(observed)))
        self.assertEqual(set(vectors), {c["chunk_id"] for c in self.children()})
        for child in self.children():
            self.assertEqual(vectors[child["chunk_id"]]["vector"], child["text"])
        self.assertEqual(stats["within_request_duplicate_child_count"], 1)
        self.assertEqual(stats["computed_input_count"], 10)

    def test_changed_model_item_order_is_rejected(self):
        client = BatchClient(wrong_order=True)
        with self.assertRaises(ValueError):
            self.run_batches(client)
        self.assertEqual(len(client.calls), 1)


if __name__ == "__main__":
    unittest.main()

