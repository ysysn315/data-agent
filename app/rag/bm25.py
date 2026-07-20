from rank_bm25 import BM25Okapi
from typing import List, Dict
import jieba


class BM25Retriever:
    def __init__(self):
        self.bm25 = None
        self.documents = []

    def index(self, documents: List[Dict]):
        self.documents = documents
        tokenized = [list(jieba.cut(doc["content"]))
                     for doc in documents]
        self.bm25 = BM25Okapi(tokenized)

    def search(self, query: str, top_k: int = 10) -> List[Dict]:
        tokenized_query = list(jieba.cut(query))
        scores = self.bm25.get_scores(tokenized_query)
        top_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]
        return [
            {
                **self.documents[i], "bm25_score": scores[i], "bm25_rank": rank + 1
            }
            for rank, i in enumerate(top_indices)
        ]
